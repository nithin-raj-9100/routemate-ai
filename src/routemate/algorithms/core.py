from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Collection, Hashable, Protocol

from ..ds.graph import Edge
from ..ds.heap import HeapCounters

Node = Hashable
Extend = Callable[[Any, Edge], Any]
"""Maps (label at u, edge u->v) to the label at v, or None when the edge cannot be taken (e.g. no train)."""


class GraphLike(Protocol):
    def out_edges(self, u: Node) -> list[Edge]: ...


def add_time(label: float, edge: Edge) -> float:
    return label + edge.time


@dataclass
class SearchStats:
    algorithm: str
    settled: int = 0
    relaxed: int = 0
    pushes: int = 0
    pops: int = 0
    decrease_keys: int = 0
    max_frontier: int = 0
    elapsed_ms: float = 0.0
    trace: list[Node] = field(default_factory=list)

    def absorb(self, counters: HeapCounters) -> None:
        self.pushes += counters.pushes
        self.pops += counters.pops
        self.decrease_keys += counters.decrease_keys
        self.max_frontier = max(self.max_frontier, counters.max_size)

    def as_dict(self, include_trace: bool = False) -> dict:
        d = {
            "algorithm": self.algorithm,
            "settled": self.settled,
            "relaxed": self.relaxed,
            "pushes": self.pushes,
            "pops": self.pops,
            "decreaseKeys": self.decrease_keys,
            "maxFrontier": self.max_frontier,
            "elapsedMs": round(self.elapsed_ms, 3),
        }
        if include_trace:
            d["trace"] = self.trace
        return d


@dataclass
class PathResult:
    found: bool
    nodes: list[Node]
    edges: list[Edge]
    cost: Any
    stats: SearchStats

    @property
    def signature(self) -> tuple:
        return tuple(self.nodes)


class Stopwatch:
    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0


def rebuild(pred: dict[Node, Edge], source: Node, target: Node) -> tuple[list[Node], list[Edge]]:
    edges: list[Edge] = []
    node = target
    while node != source:
        e = pred[node]
        edges.append(e)
        node = e.u
    edges.reverse()
    return [source] + [e.v for e in edges], edges


def fold(edges: list[Edge], initial: Any, extend: Extend) -> Any:
    label = initial
    for e in edges:
        label = extend(label, e)
        if label is None:
            return None
    return label


EMPTY: Collection = frozenset()
