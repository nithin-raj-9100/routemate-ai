from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")
_SPACES = re.compile(r"\s+")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = text.lower().replace("&", " and ").replace("-", " ").replace(".", " ")
    text = _NON_ALNUM.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def damerau_levenshtein(a: str, b: str, max_distance: int | None = None) -> int:
    """Optimal-string-alignment edit distance (insert, delete, substitute, adjacent transpose).

    With max_distance set, returns max_distance + 1 as soon as every cell in a row exceeds it.
    """
    if a == b:
        return 0
    if abs(len(a) - len(b)) > (max_distance if max_distance is not None else len(a) + len(b)):
        return (max_distance or 0) + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        row_min = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            row_min = min(row_min, cur[j])
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        prev2, prev = prev, cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """1.0 for identical strings, falling towards 0 as the edit distance approaches the longer length."""
    if not a and not b:
        return 1.0
    longest = max(len(a), len(b))
    return 1.0 - damerau_levenshtein(a, b) / longest
