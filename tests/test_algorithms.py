import itertools
import math
import random

import pytest

from routemate.algorithms import astar, bfs, dijkstra, dominates, pareto_search, yen_k_shortest, zero_one_bfs
from routemate.ds.graph import DiGraph


def random_graph(seed: int, n: int = 25, p: float = 0.15, integer: bool = False) -> DiGraph:
    rng = random.Random(seed)
    g = DiGraph()
    for u in range(n):
        g.add_node(u)
    for u, v in itertools.permutations(range(n), 2):
        if rng.random() < p:
            w = rng.randint(0, 1) if integer else round(rng.uniform(0.5, 20), 2)
            g.add_edge(u, v, time=w, transfers=int(w) if integer else rng.randint(0, 1))
    return g


def floyd_warshall(g: DiGraph, n: int, weight=lambda e: e.time) -> list[list[float]]:
    d = [[0.0 if i == j else math.inf for j in range(n)] for i in range(n)]
    for e in g.edges():
        d[e.u][e.v] = min(d[e.u][e.v], weight(e))
    for k in range(n):
        for i in range(n):
            for j in range(n):
                if d[i][k] + d[k][j] < d[i][j]:
                    d[i][j] = d[i][k] + d[k][j]
    return d


def path_cost(edges) -> float:
    return sum(e.time for e in edges)


@pytest.mark.parametrize("seed", range(8))
def test_dijkstra_matches_floyd_warshall(seed):
    n = 25
    g = random_graph(seed, n)
    ref = floyd_warshall(g, n)
    for s in range(0, n, 4):
        for t in range(n):
            if s == t:
                continue
            res = dijkstra(g, s, t)
            if math.isinf(ref[s][t]):
                assert not res.found
            else:
                assert res.found
                assert res.cost == pytest.approx(ref[s][t])
                assert path_cost(res.edges) == pytest.approx(res.cost)
                assert res.nodes[0] == s and res.nodes[-1] == t


@pytest.mark.parametrize("seed", range(5))
def test_astar_with_zero_heuristic_equals_dijkstra(seed):
    g = random_graph(seed, 30)
    for t in range(1, 30, 3):
        a, d = astar(g, 0, t, heuristic=lambda _: 0.0), dijkstra(g, 0, t)
        assert a.found == d.found
        if a.found:
            assert a.cost == pytest.approx(d.cost)


def test_astar_on_grid_expands_fewer_nodes_than_dijkstra():
    g = DiGraph()
    n = 20
    for x in range(n):
        for y in range(n):
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                if 0 <= x + dx < n and 0 <= y + dy < n:
                    g.add_edge((x, y), (x + dx, y + dy), time=1.0)
    target = (n - 1, n - 1)
    h = lambda node: abs(node[0] - target[0]) + abs(node[1] - target[1])
    a, d = astar(g, (0, 0), target, h), dijkstra(g, (0, 0), target)
    assert a.cost == d.cost == 2 * (n - 1)
    assert a.stats.settled < d.stats.settled / 3


@pytest.mark.parametrize("seed", range(6))
def test_zero_one_bfs_matches_dijkstra(seed):
    g = random_graph(seed, 30, integer=True)
    for t in range(1, 30, 2):
        z = zero_one_bfs(g, 0, t, weight=lambda e: e.transfers)
        d = dijkstra(g, 0, t, extend=lambda lab, e: lab + e.transfers, initial=0)
        assert z.found == d.found
        if z.found:
            assert z.cost == d.cost


def test_zero_one_bfs_rejects_other_weights():
    g = DiGraph()
    g.add_edge("a", "b", transfers=2)
    with pytest.raises(ValueError):
        zero_one_bfs(g, "a", "b", weight=lambda e: e.transfers)


@pytest.mark.parametrize("seed", range(5))
def test_bfs_gives_fewest_edges(seed):
    g = random_graph(seed, 25)
    ref = floyd_warshall(g, 25, weight=lambda e: 1)
    for t in range(1, 25):
        res = bfs(g, 0, t)
        assert res.found == (not math.isinf(ref[0][t]))
        if res.found:
            assert res.cost == ref[0][t] == len(res.edges)


def test_blocked_nodes_and_edges_are_respected():
    g = DiGraph()
    g.add_edge("s", "a", time=1)
    g.add_edge("a", "t", time=1)
    g.add_edge("s", "b", time=5)
    g.add_edge("b", "t", time=5)
    assert dijkstra(g, "s", "t").nodes == ["s", "a", "t"]
    assert dijkstra(g, "s", "t", blocked_nodes={"a"}).nodes == ["s", "b", "t"]
    assert dijkstra(g, "s", "t", blocked_edges={("a", "t")}).nodes == ["s", "b", "t"]


def test_lexicographic_labels():
    g = DiGraph()
    g.add_edge("s", "t", time=10, transfers=0)
    g.add_edge("s", "m", time=1, transfers=1)
    g.add_edge("m", "t", time=1, transfers=0)
    ext = lambda lab, e: (lab[0] + e.transfers, lab[1] + e.time)
    assert dijkstra(g, "s", "t").nodes == ["s", "m", "t"]
    assert dijkstra(g, "s", "t", extend=ext, initial=(0, 0)).nodes == ["s", "t"]


def all_simple_path_costs(g: DiGraph, s, t) -> list[float]:
    costs = []

    def dfs(u, seen, cost):
        if u == t:
            costs.append(cost)
            return
        for e in g.out_edges(u):
            if e.v not in seen:
                dfs(e.v, seen | {e.v}, cost + e.time)

    dfs(s, {s}, 0.0)
    return sorted(costs)


@pytest.mark.parametrize("seed", range(6))
def test_yen_matches_brute_force(seed):
    g = random_graph(seed, 9, p=0.35)
    brute = all_simple_path_costs(g, 0, 8)
    paths, _ = yen_k_shortest(g, 0, 8, 5)
    assert [p.cost for p in paths] == pytest.approx(brute[:5])
    for p in paths:
        assert len(set(p.nodes)) == len(p.nodes)
        assert path_cost(p.edges) == pytest.approx(p.cost)


@pytest.mark.parametrize("seed", range(6))
def test_pareto_front_matches_brute_force(seed):
    g = random_graph(seed, 9, p=0.35)
    vectors = set()

    def dfs(u, seen, time, transfers):
        if u == 8:
            vectors.add((round(time, 6), transfers))
            return
        for e in g.out_edges(u):
            if e.v not in seen:
                dfs(e.v, seen | {e.v}, time + e.time, transfers + e.transfers)

    dfs(0, {0}, 0.0, 0)
    front = sorted(v for v in vectors if not any(o != v and dominates(o, v) for o in vectors))
    paths, _ = pareto_search(g, 0, 8, extend=lambda v, e: (v[0] + e.time, v[1] + e.transfers), initial=(0.0, 0))
    got = sorted((round(p.cost[0], 6), p.cost[1]) for p in paths)
    assert got == front
