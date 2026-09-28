from __future__ import annotations

from collections import deque
from typing import Any, Callable, Collection

from ..ds.graph import Edge
from ..ds.heap import IndexedMinHeap
from .core import (
    EMPTY,
    Extend,
    GraphLike,
    Node,
    PathResult,
    SearchStats,
    Stopwatch,
    add_time,
    rebuild,
)


def dijkstra(
    graph: GraphLike,
    source: Node,
    target: Node,
    *,
    extend: Extend = add_time,
    initial: Any = 0.0,
    heuristic: Callable[[Node], float] | None = None,
    blocked_nodes: Collection[Node] = EMPTY,
    blocked_edges: Collection[tuple[Node, Node]] = EMPTY,
    trace: bool = False,
    name: str | None = None,
) -> PathResult:
    """Label-setting shortest path with an indexed heap and decrease-key.

    Labels may be numbers or tuples (lexicographic objectives) and `extend` may depend on the
    incoming label, which makes this a time-dependent Dijkstra whenever edge costs are FIFO.
    With `heuristic` it becomes A*: nodes are ordered by label + h(node); h must be consistent.
    """
    stats = SearchStats(name or ("astar" if heuristic else "dijkstra"))
    clock = Stopwatch()

    def priority(label: Any, node: Node) -> Any:
        if heuristic is None:
            return label
        f = label + heuristic(node)
        return (f, -label) if isinstance(label, (int, float)) else f

    best: dict[Node, Any] = {source: initial}
    pred: dict[Node, Edge] = {}
    settled: set[Node] = set()
    heap: IndexedMinHeap[Node] = IndexedMinHeap()
    heap.push_or_decrease(source, priority(initial, source))

    found = False
    while heap:
        u, _ = heap.pop()
        settled.add(u)
        stats.settled += 1
        if trace:
            stats.trace.append(u)
        if u == target:
            found = True
            break
        label_u = best[u]
        for e in graph.out_edges(u):
            v = e.v
            if v in settled or v in blocked_nodes or (blocked_edges and e.key in blocked_edges):
                continue
            label_v = extend(label_u, e)
            if label_v is None:
                continue
            stats.relaxed += 1
            if v not in best or label_v < best[v]:
                best[v] = label_v
                pred[v] = e
                heap.push_or_decrease(v, priority(label_v, v))

    stats.absorb(heap.counters)
    stats.elapsed_ms = clock.ms
    if not found:
        return PathResult(False, [], [], None, stats)
    nodes, edges = rebuild(pred, source, target)
    return PathResult(True, nodes, edges, best[target], stats)


def astar(graph: GraphLike, source: Node, target: Node, heuristic: Callable[[Node], float], **kw) -> PathResult:
    return dijkstra(graph, source, target, heuristic=heuristic, **kw)


def bfs(
    graph: GraphLike,
    source: Node,
    target: Node,
    *,
    trace: bool = False,
    edge_filter: Callable[[Edge], bool] | None = None,
) -> PathResult:
    """Classic breadth-first search: fewest edges from source to target."""
    stats = SearchStats("bfs")
    clock = Stopwatch()
    pred: dict[Node, Edge] = {}
    seen = {source}
    queue: deque[Node] = deque([source])
    found = source == target
    while queue and not found:
        u = queue.popleft()
        stats.settled += 1
        stats.pops += 1
        if trace:
            stats.trace.append(u)
        for e in graph.out_edges(u):
            if edge_filter and not edge_filter(e):
                continue
            stats.relaxed += 1
            if e.v in seen:
                continue
            seen.add(e.v)
            pred[e.v] = e
            if e.v == target:
                found = True
                break
            queue.append(e.v)
            stats.pushes += 1
            stats.max_frontier = max(stats.max_frontier, len(queue))
    stats.elapsed_ms = clock.ms
    if not found:
        return PathResult(False, [], [], None, stats)
    nodes, edges = rebuild(pred, source, target)
    return PathResult(True, nodes, edges, len(edges), stats)


def zero_one_bfs(
    graph: GraphLike,
    source: Node,
    target: Node,
    weight: Callable[[Edge], int],
    *,
    trace: bool = False,
) -> PathResult:
    """BFS on a deque for graphs whose edge weights are only 0 or 1 (here: 1 per line change).

    0-edges go to the front of the deque, 1-edges to the back, which keeps the deque sorted by
    distance and gives Dijkstra's answer in O(V + E) with no heap.
    """
    stats = SearchStats("0-1 bfs")
    clock = Stopwatch()
    dist: dict[Node, int] = {source: 0}
    pred: dict[Node, Edge] = {}
    done: set[Node] = set()
    dq: deque[Node] = deque([source])
    while dq:
        u = dq.popleft()
        if u in done:
            continue
        done.add(u)
        stats.settled += 1
        stats.pops += 1
        if trace:
            stats.trace.append(u)
        if u == target:
            break
        for e in graph.out_edges(u):
            w = weight(e)
            if w not in (0, 1):
                raise ValueError(f"0-1 BFS needs 0/1 weights, got {w} on {e.kind} edge")
            nd = dist[u] + w
            stats.relaxed += 1
            if e.v not in dist or nd < dist[e.v]:
                dist[e.v] = nd
                pred[e.v] = e
                (dq.appendleft if w == 0 else dq.append)(e.v)
                stats.pushes += 1
                stats.max_frontier = max(stats.max_frontier, len(dq))
    stats.elapsed_ms = clock.ms
    if target not in done:
        return PathResult(False, [], [], None, stats)
    nodes, edges = rebuild(pred, source, target)
    return PathResult(True, nodes, edges, dist[target], stats)
