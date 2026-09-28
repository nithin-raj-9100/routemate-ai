from __future__ import annotations

from dataclasses import dataclass, field

from .heap import BinaryHeap


@dataclass
class _Node:
    children: dict[str, "_Node"] = field(default_factory=dict)
    payloads: dict[str, float] = field(default_factory=dict)
    best: float = 0.0


class Trie:
    """Prefix tree mapping normalised terms to payload ids.

    Every node caches the best weight in its subtree, so a ranked top-k prefix query
    walks the subtree best-first through a heap instead of collecting every match.
    """

    def __init__(self) -> None:
        self._root = _Node()
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def insert(self, term: str, payload: str, weight: float = 1.0) -> None:
        node = self._root
        node.best = max(node.best, weight)
        for ch in term:
            node = node.children.setdefault(ch, _Node())
            node.best = max(node.best, weight)
        if payload not in node.payloads:
            self._size += 1
        node.payloads[payload] = max(node.payloads.get(payload, 0.0), weight)

    def _find(self, prefix: str) -> _Node | None:
        node = self._root
        for ch in prefix:
            node = node.children.get(ch)
            if node is None:
                return None
        return node

    def contains(self, term: str) -> bool:
        node = self._find(term)
        return bool(node and node.payloads)

    def exact(self, term: str) -> dict[str, float]:
        node = self._find(term)
        return dict(node.payloads) if node else {}

    def top_k(self, prefix: str, k: int = 8) -> list[tuple[str, float]]:
        """Highest-weight distinct payloads under prefix, best-first."""
        start = self._find(prefix)
        if start is None:
            return []
        heap: BinaryHeap[tuple[str, object]] = BinaryHeap()
        heap.push(-start.best, ("node", start))
        seen: dict[str, float] = {}
        out: list[tuple[str, float]] = []
        while heap and len(out) < k:
            neg, (kind, obj) = heap.pop()
            if kind == "payload":
                pid = obj  # type: ignore[assignment]
                if pid not in seen:
                    seen[pid] = -neg
                    out.append((pid, -neg))
                continue
            node: _Node = obj  # type: ignore[assignment]
            for pid, w in node.payloads.items():
                heap.push(-w, ("payload", pid))
            for child in node.children.values():
                heap.push(-child.best, ("node", child))
        return out
