from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from ..ds.graph import DiGraph, Edge
from .model import Network, haversine_km

SOURCE = ("src",)
SINK = ("dst",)

FORWARD, BACKWARD = 1, -1


def ride_node(station: str, line: str, direction: int) -> tuple:
    return ("ride", station, line, direction)


def wait_node(station: str, line: str) -> tuple:
    return ("wait", station, line)


def station_of(node: tuple, origin: str | None = None, destination: str | None = None) -> str | None:
    if node == SOURCE:
        return origin
    if node == SINK:
        return destination
    return node[1]


@dataclass
class TransitGraph:
    """Line-expanded graph of the metro.

    Riding and waiting are separate node types, so a route's line changes, boardings and
    in-vehicle stretches are all explicit edges:

        wait(s, L) --board--> ride(s, L, dir) --ride--> ride(s+1, L, dir) ...
        ride(s, L1, dir) --transfer(walk)--> wait(s, L2)          (interchanges only)

    Per query, a virtual SOURCE feeds wait(origin, *) and ride(destination, *) feeds SINK.
    """

    network: Network
    graph: DiGraph = field(default_factory=DiGraph)
    station_graph: DiGraph = field(default_factory=DiGraph)
    seg_km: dict[str, list[float]] = field(default_factory=dict)
    seg_min: dict[str, list[float]] = field(default_factory=dict)
    offsets: dict[tuple[str, int], list[float]] = field(default_factory=dict)
    index: dict[tuple[str, str], int] = field(default_factory=dict)
    min_scale: float = 1.0

    def __post_init__(self) -> None:
        self._build()

    def _build(self) -> None:
        net, svc = self.network, self.network.service
        scales = []
        for line in net.lines.values():
            raw = [
                haversine_km(net.stations[a].lat, net.stations[a].lon, net.stations[b].lat, net.stations[b].lon)
                for a, b in zip(line.stations, line.stations[1:])
            ]
            scale = line.length_km / sum(raw)
            scales.append(scale)
            km = [d * scale for d in raw]
            minutes = [d / svc.avg_run_speed_kmph * 60 + svc.dwell_min for d in km]
            self.seg_km[line.id], self.seg_min[line.id] = km, minutes
            n = len(line.stations)
            for i, sid in enumerate(line.stations):
                self.index[(line.id, sid)] = i
            fwd = [0.0]
            for m in minutes:
                fwd.append(fwd[-1] + m)
            bwd = [fwd[-1] - t for t in fwd]
            self.offsets[(line.id, FORWARD)] = fwd
            self.offsets[(line.id, BACKWARD)] = bwd

            for i, sid in enumerate(line.stations):
                w = wait_node(sid, line.id)
                if i < n - 1:
                    self.graph.add_edge(w, ride_node(sid, line.id, FORWARD), kind="board", line=line.id, direction=FORWARD)
                if i > 0:
                    self.graph.add_edge(w, ride_node(sid, line.id, BACKWARD), kind="board", line=line.id, direction=BACKWARD)
            for i in range(n - 1):
                a, b = line.stations[i], line.stations[i + 1]
                common = dict(kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1)
                self.graph.add_edge(ride_node(a, line.id, FORWARD), ride_node(b, line.id, FORWARD), direction=FORWARD, **common)
                self.graph.add_edge(ride_node(b, line.id, BACKWARD), ride_node(a, line.id, BACKWARD), direction=BACKWARD, **common)
                self.station_graph.add_edge(a, b, kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1)
                self.station_graph.add_edge(b, a, kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1)
        self.min_scale = min(1.0, *scales)

        for sid, walk in net.interchanges.items():
            here = net.lines_at(sid)
            for l1 in here:
                for l2 in here:
                    if l1 == l2:
                        continue
                    for d in (FORWARD, BACKWARD):
                        node = ride_node(sid, l1, d)
                        if node in self.graph:
                            self.graph.add_edge(node, wait_node(sid, l2), kind="transfer", time=walk, transfers=1, line=l2)

    def query_edges(self, origin: str, destination: str) -> list[Edge]:
        net = self.network
        extra = [
            Edge(SOURCE, wait_node(origin, lid), kind="access", time=net.service.access_min, line=lid)
            for lid in net.lines_at(origin)
        ]
        for lid in net.lines_at(destination):
            for d in (FORWARD, BACKWARD):
                node = ride_node(destination, lid, d)
                if node in self.graph:
                    extra.append(Edge(node, SINK, kind="alight", line=lid, direction=d))
        return extra

    def towards(self, line: str, direction: int) -> str:
        first, last = self.network.lines[line].terminals
        return last if direction == FORWARD else first

    def straight_km(self, a: str, b: str) -> float:
        return _straight_km(self.network, a, b)


@lru_cache(maxsize=8192)
def _straight_km_cached(key: tuple[float, float, float, float]) -> float:
    return haversine_km(*key)


def _straight_km(net: Network, a: str, b: str) -> float:
    sa, sb = net.stations[a], net.stations[b]
    return _straight_km_cached((sa.lat, sa.lon, sb.lat, sb.lon))
