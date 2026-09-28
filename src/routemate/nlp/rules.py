from __future__ import annotations

import re

from ..ds.fuzzy import normalize
from .query import TripQuery
from .stations import Mention, StationIndex

ORIGIN_CUES = {"from", "at", "near", "leaving", "starting", "start", "currently", "am", "im", "between", "frm"}
DEST_CUES = {"to", "reach", "reaching", "towards", "till", "until", "get", "going", "go", "into", "upto", "destination", "for", "arrive", "and"}
AVOID_CUES = {"avoid", "avoiding", "skip", "skipping", "bypass", "except", "not", "without", "via_not"}
CUE_WINDOW = 3

_CHANGE_WORDS = r"(chang\w*|transfer\w*|interchang\w*|switch\w*)"
_FEWER = r"(few|fewer|fewest|less|least|min|minimum|minimal|no|not|without|avoid|dont|don t|do not|zero|never|hate)"
MODE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("fewest_transfers", re.compile(rf"\b{_FEWER}\b(\s+\w+){{0,5}}\s+{_CHANGE_WORDS}|\bdirect\b|\bsingle train\b|\bone train\b|\bsame train\b")),
    ("cheapest", re.compile(r"\b(cheap|cheaper|cheapest|budget|low fare|lowest fare|least fare|save money|economical|affordable|less money)\b")),
    ("fewest_stops", re.compile(r"\b(fewest|fewer|less|least|minimum)\s+(stops|stations|halts)\b")),
    ("shortest_distance", re.compile(r"\b(shortest|least|minimum)\s+(distance|km|kilometers|route|path)\b")),
    ("fastest", re.compile(r"\b(fast|faster|fastest|quick|quicker|quickest|asap|hurry|urgent|earliest|soonest|running late)\b")),
]

_CLOCK = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})\b")
_TIME = re.compile(r"\b(?P<h>\d{1,2})(?:h(?P<m>\d{2}))?\s*(?P<ap>a\s?m|p\s?m|o clock|oclock|hrs|hours)?\b")
_RELATIVE = re.compile(r"\bin\s+(?P<n>\d{1,3})\s*(?P<u>min|mins|minute|minutes|hr|hrs|hour|hours)\b")
ARRIVE_CUES = {"before", "by", "until", "till", "reach", "arrive", "latest", "within"}
DEPART_CUES = {"at", "after", "leaving", "leave", "depart", "departing", "start", "starting", "around", "from"}
PM_WORDS = {"pm", "evening", "night", "tonight", "afternoon"}
AM_WORDS = {"am", "morning"}


def _cue_role(tokens: list[str], start: int) -> str | None:
    for back in range(1, CUE_WINDOW + 1):
        i = start - back
        if i < 0:
            break
        tok = tokens[i]
        if tok in AVOID_CUES:
            return "avoid"
        if tok in DEST_CUES:
            if tok == "and" and "between" not in tokens[:start]:
                continue
            return "destination"
        if tok in ORIGIN_CUES:
            return "origin"
    return None


def assign_roles(tokens: list[str], mentions: list[Mention]) -> tuple[str | None, str | None, list[str]]:
    origin = destination = None
    avoid: list[str] = []
    unassigned: list[Mention] = []
    for m in mentions:
        role = _cue_role(tokens, m.start)
        if role == "avoid":
            avoid.append(m.station)
        elif role == "origin" and origin is None:
            origin = m.station
        elif role == "destination" and destination is None:
            destination = m.station
        else:
            unassigned.append(m)
    for m in unassigned:
        if m.station in (origin, destination):
            continue
        if origin is None and (destination is None or m.start < _pos(mentions, destination)):
            origin = m.station
        elif destination is None:
            destination = m.station
    return origin, destination, avoid


def _pos(mentions: list[Mention], station: str) -> int:
    return next(m.start for m in mentions if m.station == station)


def detect_mode(text: str) -> str | None:
    for mode, pattern in MODE_PATTERNS:
        if pattern.search(text):
            return mode
    return None


def _resolve_hour(h: int, m: int, ampm: str | None, words: set[str], now: float) -> float | None:
    if ampm and ampm.replace(" ", "") in ("am", "pm"):
        ap = ampm.replace(" ", "")
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
        return h * 60 + m
    if h > 24 or m > 59:
        return None
    if h > 12 or h == 0:
        return h * 60 + m
    if words & PM_WORDS:
        return (h % 12 + 12) * 60 + m
    if words & AM_WORDS:
        return (h % 12) * 60 + m
    candidates = [c for c in ((h % 12) * 60 + m, (h % 12 + 12) * 60 + m) if 5 * 60 + 30 <= c <= 24 * 60]
    if not candidates:
        return None
    upcoming = [c for c in candidates if c >= now - 30]
    return min(upcoming) if upcoming else min(candidates)


def parse_times(tokens: list[str], blocked: set[int], now: float) -> tuple[float | None, float | None]:
    """Find 'before 10', 'by 9:30 am', 'at 6pm', 'in 20 minutes' outside station-name tokens."""
    words = set(tokens)
    text = " ".join(t if i not in blocked else "_" for i, t in enumerate(tokens))
    rel = _RELATIVE.search(text)
    if rel:
        n = int(rel["n"]) * (60 if rel["u"].startswith("h") else 1)
        return now + n, None
    offsets = _token_offsets(text)
    for match in _TIME.finditer(text):
        idx = _token_index(offsets, match.start())
        prev = tokens[max(0, idx - 4) : idx]
        has_cue = bool(set(prev) & (ARRIVE_CUES | DEPART_CUES))
        if not (has_cue or match["m"] or match["ap"]):
            continue
        minute = _resolve_hour(int(match["h"]), int(match["m"] or 0), match["ap"], words, now)
        if minute is None:
            continue
        if set(prev) & ARRIVE_CUES:
            return None, minute
        return minute, None
    return None, None


def _token_offsets(text: str) -> list[int]:
    offs, pos = [], 0
    for tok in text.split(" "):
        offs.append(pos)
        pos += len(tok) + 1
    return offs


def _token_index(offsets: list[int], char_pos: int) -> int:
    idx = 0
    for i, off in enumerate(offsets):
        if off <= char_pos:
            idx = i
    return idx


class RuleParser:
    """Offline natural-language trip parser: station spotting + cue words + regex time/mode rules."""

    def __init__(self, index: StationIndex) -> None:
        self.index = index

    def parse(self, text: str, now: float) -> TripQuery:
        text = _CLOCK.sub(r"\1h\2", text)
        norm = normalize(text)
        tokens = norm.split()
        mentions = self.index.find_mentions(text)
        origin, destination, avoid = assign_roles(tokens, mentions)
        blocked = {i for m in mentions for i in range(m.start, m.end)}
        depart_at, arrive_by = parse_times(tokens, blocked, now)
        q = TripQuery(
            origin=origin,
            destination=destination,
            mode=detect_mode(norm),
            depart_at=depart_at,
            arrive_by=arrive_by,
            avoid=avoid,
            parser="rules",
        )
        if len(mentions) > 2 + len(avoid):
            q.notes.append("More than two stations mentioned; used the first as start and the next as destination.")
        return q
