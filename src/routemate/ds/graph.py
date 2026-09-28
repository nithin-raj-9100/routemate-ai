from __future__ import annotations

from dataclasses import dataclass, field
from typing import Hashable, Iterable, Iterator

Node = Hashable


@dataclass(frozen=True, slots=True)
class Edge:
    u: Node
    v: Node
    kind: str = "link"
    time: float = 0.0
    distance: float = 0.0
    line: str | None = None
    direction: int = 0
    transfers: int = 0
    stops: int = 0

    @property
    def key(self) -> tuple[Node, Node]:
        return (self.u, self.v)


@dataclass
class DiGraph:
    """Adjacency-list directed multigraph with typed edges."""

    _adj: dict[Node, list[Edge]] = field(default_factory=dict)
    _edge_count: int = 0

    def add_node(self, u: Node) -> None:
        self._adj.setdefault(u, [])

    def add_edge(self, u: Node, v: Node, **attrs) -> Edge:
        edge = Edge(u, v, **attrs)
        self._adj.setdefault(u, []).append(edge)
        self._adj.setdefault(v, [])
        self._edge_count += 1
        return edge

    def out_edges(self, u: Node) -> list[Edge]:
        return self._adj.get(u, [])

    def nodes(self) -> Iterator[Node]:
        return iter(self._adj)

    def edges(self) -> Iterator[Edge]:
        for out in self._adj.values():
            yield from out

    def __contains__(self, u: object) -> bool:
        return u in self._adj

    def __len__(self) -> int:
        return len(self._adj)

    @property
    def edge_count(self) -> int:
        return self._edge_count


class OverlayGraph:
    """Read-only view of a base graph plus per-query extra edges (virtual source/sink), without copying the base."""

    def __init__(self, base: DiGraph, extra: Iterable[Edge] = ()) -> None:
        self.base = base
        self._extra: dict[Node, list[Edge]] = {}
        for e in extra:
            self._extra.setdefault(e.u, []).append(e)

    def out_edges(self, u: Node) -> list[Edge]:
        extra = self._extra.get(u)
        if extra is None:
            return self.base.out_edges(u)
        return self.base.out_edges(u) + extra

    def __contains__(self, u: object) -> bool:
        return u in self.base or u in self._extra
