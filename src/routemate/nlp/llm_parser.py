from __future__ import annotations

from ..network.model import Network, fmt_hhmm, parse_hhmm
from .gemini import GeminiClient
from .query import TripQuery
from .stations import StationIndex

MODES = ["", "fastest", "fewest_transfers", "fewest_stops", "shortest_distance", "cheapest"]

SCHEMA = {
    "type": "object",
    "properties": {
        "origin": {"type": "string", "description": "Exact station name from the list, or empty"},
        "destination": {"type": "string", "description": "Exact station name from the list, or empty"},
        "mode": {"type": "string", "enum": MODES},
        "depart_at": {"type": "string", "description": "24h HH:MM, or empty"},
        "arrive_by": {"type": "string", "description": "24h HH:MM, or empty"},
        "avoid": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string", "description": "ISO 639-1 code of the user's language"},
    },
    "required": ["origin", "destination", "mode", "depart_at", "arrive_by", "avoid", "language"],
}


class LLMParser:
    """Gemini reads the sentence; the StationIndex re-validates every station it names.

    The model never gets the final say on a station id or a time: anything it returns that does
    not resolve against the real network is dropped, and the offline parser fills the gap.
    """

    def __init__(self, client: GeminiClient, index: StationIndex, network: Network) -> None:
        self.client = client
        self.index = index
        self.network = network
        names = ", ".join(sorted(s.name for s in network.stations.values()))
        lines = "; ".join(
            f"{l.name}: {network.stations[l.stations[0]].name} to {network.stations[l.stations[-1]].name}"
            for l in network.lines.values()
        )
        self.system = (
            f"You convert trip requests for the {network.system} in {network.city} into JSON.\n"
            f"Stations (always answer with these exact names): {names}.\n"
            f"Lines: {lines}.\n"
            "Rules:\n"
            "- origin/destination: station names from the list; map a well-known landmark or area to its metro station "
            "only when it is unambiguous (e.g. 'Inorbit Mall' -> Durgam Cheruvu, 'Koti' -> Sultan Bazaar). Empty if not stated.\n"
            "- For a follow-up like 'and back?' or 'return trip', swap the previous origin and destination.\n"
            "- mode: fastest | fewest_transfers (user dislikes changing trains) | fewest_stops | shortest_distance | cheapest; "
            "empty when no preference is expressed.\n"
            "- 'before/by/reach by X' is arrive_by; 'at/after/leaving X' is depart_at. Resolve relative times using the current time. "
            "Assume the nearest upcoming time when am/pm is missing.\n"
            "- avoid: stations the user explicitly wants to avoid.\n"
            "- The user may write in English, Telugu, Hindi or a mix, in any script."
        )

    def parse(self, text: str, now: float, previous: TripQuery | None = None) -> TripQuery:
        prompt = f"Current local time: {fmt_hhmm(now)}\n"
        if previous and (previous.origin or previous.destination):
            st = self.network.stations
            prompt += (
                "Previous trip: "
                f"{st[previous.origin].name if previous.origin else '?'} -> "
                f"{st[previous.destination].name if previous.destination else '?'}\n"
            )
        prompt += f"Request: {text}"
        data = self.client.generate_json(system=self.system, prompt=prompt, schema=SCHEMA)

        q = TripQuery(parser="gemini", language=(data.get("language") or "en")[:5])
        for role in ("origin", "destination"):
            value = (data.get(role) or "").strip()
            if value:
                sid = self.index.resolve(value)
                if sid:
                    setattr(q, role, sid)
                else:
                    q.notes.append(f"AI suggested an unknown station '{value}'; ignored.")
        mode = data.get("mode") or ""
        q.mode = mode if mode in MODES and mode else None
        for key, attr in (("depart_at", "depart_at"), ("arrive_by", "arrive_by")):
            value = (data.get(key) or "").strip()
            if value:
                try:
                    setattr(q, attr, parse_hhmm(value))
                except ValueError:
                    q.notes.append(f"AI returned an unreadable time '{value}'; ignored.")
        if q.depart_at is not None and q.arrive_by is not None:
            q.depart_at = None
        q.avoid = [sid for sid in (self.index.resolve(a) for a in data.get("avoid") or []) if sid]
        return q


def combine(primary: TripQuery, fallback: TripQuery) -> TripQuery:
    """Fields the LLM left empty are filled from the offline parse of the same sentence."""
    has_time = primary.depart_at is not None or primary.arrive_by is not None
    return TripQuery(
        origin=primary.origin or fallback.origin,
        destination=primary.destination or fallback.destination,
        mode=primary.mode or fallback.mode,
        depart_at=primary.depart_at if has_time else fallback.depart_at,
        arrive_by=primary.arrive_by if has_time else fallback.arrive_by,
        avoid=primary.avoid or fallback.avoid,
        language=primary.language,
        parser=primary.parser,
        notes=primary.notes + fallback.notes,
    )
