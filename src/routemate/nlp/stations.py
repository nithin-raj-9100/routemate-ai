from __future__ import annotations

from dataclasses import dataclass

from ..ds.fuzzy import normalize, similarity
from ..ds.trie import Trie
from ..network.model import Network

SINGLE_WORD_MIN_SIM = 0.84
PHRASE_MIN_SIM = 0.8
RESOLVE_MIN_SIM = 0.72


@dataclass(frozen=True)
class Mention:
    station: str
    start: int
    end: int
    score: float
    surface: str


class StationIndex:
    """Everything that turns human text into station ids: exact names, aliases, prefixes, typos.

    A trie over full names, aliases and each word of a name answers autocomplete ("hit" ->
    Hitech City, "city" -> Hitech City). Anything the trie cannot place falls back to
    Damerau-Levenshtein similarity against every known name, so "Ameerpeth" still lands.
    """

    def __init__(self, network: Network) -> None:
        self.network = network
        self.trie = Trie()
        self.names: dict[str, str] = {}
        self.compact: dict[str, str] = {}
        for sid, st in network.stations.items():
            weight = 3.0 if network.is_interchange(sid) else 2.0 if self._is_terminal(sid) else 1.0
            forms = {normalize(st.name), normalize(sid.replace("_", " "))} | {normalize(a) for a in st.aliases}
            for form in forms:
                self.names[form] = sid
                self.compact[form.replace(" ", "")] = sid
                self.trie.insert(form, sid, weight + 0.5)
                for word in form.split():
                    if len(word) > 2:
                        self.trie.insert(word, sid, weight)

    def _is_terminal(self, sid: str) -> bool:
        return any(sid in line.terminals for line in self.network.lines.values())

    def resolve(self, text: str) -> str | None:
        if text in self.network.stations:
            return text
        norm = normalize(text)
        if not norm:
            return None
        if norm in self.names:
            return self.names[norm]
        if norm.replace(" ", "") in self.compact:
            return self.compact[norm.replace(" ", "")]
        prefix = self.trie.top_k(norm, 2)
        if len(prefix) == 1 or (prefix and len(norm) >= 4):
            return prefix[0][0]
        best, score = self._best_fuzzy(norm)
        return best if score >= RESOLVE_MIN_SIM else None

    def suggest(self, text: str, k: int = 8) -> list[str]:
        norm = normalize(text)
        if not norm:
            return []
        out = [sid for sid, _ in self.trie.top_k(norm, k)]
        if not out and len(norm) >= 3:
            ranked = sorted(self.names.items(), key=lambda kv: -similarity(norm, kv[0][: len(norm) + 2]))
            for form, sid in ranked:
                if sid not in out and similarity(norm, form[: len(norm) + 2]) >= 0.6:
                    out.append(sid)
                if len(out) >= k:
                    break
        return out

    def _best_fuzzy(self, norm: str) -> tuple[str | None, float]:
        best, score = None, 0.0
        for form, sid in self.names.items():
            s = similarity(norm, form)
            if s > score:
                best, score = sid, s
        return best, score

    def find_mentions(self, text: str, max_words: int = 5) -> list[Mention]:
        """Non-overlapping station mentions in free text, as token spans of normalize(text).split()."""
        tokens = normalize(text).split()
        candidates: list[Mention] = []
        for n in range(min(max_words, len(tokens)), 0, -1):
            for i in range(len(tokens) - n + 1):
                phrase = " ".join(tokens[i : i + n])
                if len(phrase) < 3:
                    continue
                exact = self.names.get(phrase) or self.compact.get(phrase.replace(" ", ""))
                if exact:
                    candidates.append(Mention(exact, i, i + n, 1.0 + n * 0.01, phrase))
                    continue
                if len(phrase) < 5:
                    continue
                sid, score = self._best_fuzzy(phrase)
                threshold = SINGLE_WORD_MIN_SIM if n == 1 else PHRASE_MIN_SIM
                if sid and score >= threshold:
                    candidates.append(Mention(sid, i, i + n, score + n * 0.01, phrase))
        candidates.sort(key=lambda m: (-m.score, -(m.end - m.start), m.start))
        taken: list[Mention] = []
        used: set[int] = set()
        for m in candidates:
            span = set(range(m.start, m.end))
            if span & used:
                continue
            taken.append(m)
            used |= span
        return sorted(taken, key=lambda m: m.start)
