from __future__ import annotations

from typing import Any

from ..ds.heap import BinaryHeap
from .core import Extend, GraphLike, Node, PathResult, SearchStats, Stopwatch, add_time, fold
from .shortest import dijkstra


def yen_k_shortest(
    graph: GraphLike,
    source: Node,
    target: Node,
    k: int,
    *,
    extend: Extend = add_time,
    initial: Any = 0.0,
) -> tuple[list[PathResult], SearchStats]:
    """Yen's algorithm: the k cheapest loopless paths, cheapest first.

    Each new path deviates from an accepted one at a spur node: the shared root is kept, the
    edges already used by accepted paths out of that root are blocked, root nodes are blocked
    to stay loopless, and a Dijkstra from the spur (seeded with the root's label, so
    time-dependent costs stay correct) finishes the path.
    """
    stats = SearchStats("yen")
    clock = Stopwatch()
    first = dijkstra(graph, source, target, extend=extend, initial=initial)
    _merge(stats, first.stats)
    if not first.found:
        stats.elapsed_ms = clock.ms
        return [], stats

    accepted: list[PathResult] = [first]
    seen = {first.signature}
    candidates: BinaryHeap[PathResult] = BinaryHeap()

    while len(accepted) < k:
        last = accepted[-1]
        for i in range(len(last.nodes) - 1):
            spur = last.nodes[i]
            root_nodes = last.nodes[: i + 1]
            root_edges = last.edges[:i]
            blocked_edges = {p.edges[i].key for p in accepted if p.nodes[: i + 1] == root_nodes and len(p.edges) > i}
            root_label = fold(root_edges, initial, extend)
            if root_label is None:
                continue
            spur_res = dijkstra(
                graph,
                spur,
                target,
                extend=extend,
                initial=root_label,
                blocked_nodes=set(root_nodes[:-1]),
                blocked_edges=blocked_edges,
            )
            _merge(stats, spur_res.stats)
            if not spur_res.found:
                continue
            total = PathResult(True, root_nodes[:-1] + spur_res.nodes, root_edges + spur_res.edges, spur_res.cost, spur_res.stats)
            if total.signature in seen:
                continue
            seen.add(total.signature)
            candidates.push(total.cost, total)
        if not candidates:
            break
        _, best = candidates.pop()
        accepted.append(best)

    stats.elapsed_ms = clock.ms
    return accepted, stats


def _merge(into: SearchStats, other: SearchStats) -> None:
    into.settled += other.settled
    into.relaxed += other.relaxed
    into.pushes += other.pushes
    into.pops += other.pops
    into.decrease_keys += other.decrease_keys
    into.max_frontier = max(into.max_frontier, other.max_frontier)
