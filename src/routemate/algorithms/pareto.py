from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..ds.graph import Edge
from ..ds.heap import BinaryHeap
from .core import GraphLike, Node, PathResult, SearchStats, Stopwatch

Vector = tuple[float, ...]
VectorExtend = Callable[[Vector, Edge], Vector | None]


@dataclass(eq=False)
class Label:
    values: Vector
    node: Node
    parent: "Label | None" = None
    edge: Edge | None = None
    dead: bool = field(default=False)


def dominates(a: Vector, b: Vector) -> bool:
    """a is at least as good as b in every criterion (equal vectors count, so duplicates are pruned)."""
    return all(x <= y for x, y in zip(a, b))


def pareto_search(
    graph: GraphLike,
    source: Node,
    target: Node,
    *,
    extend: VectorExtend,
    initial: Vector,
    max_labels: int = 50_000,
) -> tuple[list[PathResult], SearchStats]:
    """Multi-criteria label-setting search returning every Pareto-optimal path.

    A single number cannot say whether "12 min faster" beats "one change fewer"; instead every
    node keeps a bag of mutually non-dominated labels and the search returns the whole front
    (e.g. fastest, fewest changes, and any sensible compromise between them).
    Labels are expanded in lexicographic order and pruned against both their own bag and the
    target's bag, so hopeless partial routes die early.
    """
    stats = SearchStats("pareto")
    clock = Stopwatch()
    bags: dict[Node, list[Label]] = {}
    heap: BinaryHeap[Label] = BinaryHeap()
    root = Label(initial, source)
    bags[source] = [root]
    heap.push(initial, root)
    created = 1

    while heap:
        _, label = heap.pop()
        if label.dead:
            continue
        stats.settled += 1
        if label.node == target:
            continue
        if any(dominates(t.values, label.values) and t is not label for t in bags.get(target, ())):
            continue
        for e in graph.out_edges(label.node):
            values = extend(label.values, e)
            if values is None:
                continue
            stats.relaxed += 1
            if _dominated(values, bags.get(target, ())) and e.v != target:
                continue
            bag = bags.setdefault(e.v, [])
            if _dominated(values, bag):
                continue
            for old in bag:
                if dominates(values, old.values):
                    old.dead = True
            bag[:] = [old for old in bag if not old.dead]
            new = Label(values, e.v, label, e)
            bag.append(new)
            heap.push(values, new)
            created += 1
            if created > max_labels:
                raise RuntimeError("pareto search exceeded its label budget")

    stats.absorb(heap.counters)
    stats.elapsed_ms = clock.ms
    front = sorted(bags.get(target, []), key=lambda lab: lab.values)
    return [_to_path(lab, stats) for lab in front], stats


def _dominated(values: Vector, bag: list[Label]) -> bool:
    return any(dominates(other.values, values) for other in bag)


def _to_path(label: Label, stats: SearchStats) -> PathResult:
    edges: list[Edge] = []
    cur: Label | None = label
    while cur is not None and cur.edge is not None:
        edges.append(cur.edge)
        cur = cur.parent
    edges.reverse()
    nodes = [edges[0].u] + [e.v for e in edges] if edges else [label.node]
    return PathResult(True, nodes, edges, label.values, stats)
