import heapq
import random

import pytest

from routemate.ds.fuzzy import damerau_levenshtein, normalize, similarity
from routemate.ds.heap import BinaryHeap, IndexedMinHeap
from routemate.ds.trie import Trie


def test_binary_heap_sorts_like_heapq():
    rng = random.Random(7)
    values = [rng.randint(-1000, 1000) for _ in range(2000)]
    h: BinaryHeap[int] = BinaryHeap()
    for v in values:
        h.push(v, v)
    out = [h.pop()[1] for _ in range(len(values))]
    assert out == sorted(values)
    assert h.counters.pushes == h.counters.pops == len(values)


def test_binary_heap_is_stable_for_equal_priorities():
    h: BinaryHeap[str] = BinaryHeap()
    for name in "abcde":
        h.push(1, name)
    assert [h.pop()[1] for _ in range(5)] == list("abcde")


def test_indexed_heap_decrease_key_matches_reference():
    rng = random.Random(11)
    h: IndexedMinHeap[int] = IndexedMinHeap()
    best: dict[int, int] = {}
    for _ in range(5000):
        k, p = rng.randint(0, 300), rng.randint(0, 10_000)
        changed = h.push_or_decrease(k, p) if (k in h or k not in best) else False
        if k not in best:
            best[k] = p
        elif changed:
            best[k] = min(best[k], p)
    ref = sorted((p, k) for k, p in best.items())
    got = []
    while h:
        k, p = h.pop()
        got.append((p, k))
    assert [p for p, _ in got] == [p for p, _ in ref]


def test_indexed_heap_rejects_increase():
    h: IndexedMinHeap[str] = IndexedMinHeap()
    h.push_or_decrease("a", 5)
    assert h.push_or_decrease("a", 9) is False
    assert h.priority("a") == 5
    assert h.push_or_decrease("a", 2) is True
    assert h.pop() == ("a", 2)
    with pytest.raises(IndexError):
        h.pop()


def test_trie_top_k_is_ranked_and_distinct():
    t = Trie()
    t.insert("ameerpet", "ameerpet", 3)
    t.insert("amirpet", "ameerpet", 1)
    t.insert("assembly", "assembly", 1)
    t.insert("amberpet", "amberpet", 2)
    assert [pid for pid, _ in t.top_k("am", 5)] == ["ameerpet", "amberpet"]
    assert [pid for pid, _ in t.top_k("a", 5)] == ["ameerpet", "amberpet", "assembly"]
    assert t.top_k("zz") == []
    assert t.contains("assembly") and not t.contains("assem")


@pytest.mark.parametrize(
    "a,b,d",
    [("", "", 0), ("abc", "abc", 0), ("abc", "acb", 1), ("kitten", "sitting", 3), ("ameerpet", "ameerpeth", 1), ("ca", "abc", 3)],
)
def test_damerau_levenshtein(a, b, d):
    assert damerau_levenshtein(a, b) == d


def test_similarity_and_normalize():
    assert normalize("  Hi-Tech  City! ") == "hi tech city"
    assert normalize("L.B. Nagar") == "l b nagar"
    assert similarity("hitec city", "hitech city") > 0.9
    assert similarity("uppal", "raidurg") < 0.3
