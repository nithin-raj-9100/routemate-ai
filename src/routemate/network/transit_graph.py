from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache

from ..ds.graph import DiGraph, Edge
from .model import Network, haversine_km

SOURCE = ("src",)
SINK = ("dst",)

FORWARD, BACKWARD = 1, -1

BUS_COLOR = "#0F766E"


def ride_node(station: str, line: str, direction: int) -> tuple:
    return ("ride", station, line, direction)


def wait_node(station: str, line: str) -> tuple:
    return ("wait", station, line)


def bus_node(stop: str, route: str, seq: int) -> tuple:
    return ("bus", stop, route, seq)


def departing_node(station: str, line: str, direction: int) -> tuple:
    """Just boarded, still at the platform. Its only way on is the next ride edge, so a
    multimodal route can't board and step straight back off to chain walks."""
    return ("dep", station, line, direction)


def street_node(place: str) -> tuple:
    return ("at", place)


def exit_node(station: str) -> tuple:
    """Street outside a metro station you just left. It has no way back in, so a route can't
    leave and re-enter the same station to dodge an interchange walk."""
    return ("out", station)


def walked_node(place: str) -> tuple:
    """Arrived on foot. You can board or finish here but not walk on, so walking is one hop of at
    most `max_km` between rides, never a free chain of hops across the city."""
    return ("walked", place)


def station_of(node: tuple, origin: str | None = None, destination: str | None = None) -> str | None:
    if node == SOURCE:
        return origin
    if node == SINK:
        return destination
    return node[1]


@dataclass
class TransitGraph:
    """Line-expanded graph of the metro, plus an optional multimodal graph with buses and walking.

    Riding and waiting are separate node types, so a route's line changes, boardings and
    in-vehicle stretches are all explicit edges:

        wait(s, L) --board--> ride(s, L, dir) --ride--> ride(s+1, L, dir) ...
        ride(s, L1, dir) --transfer(walk)--> wait(s, L2)          (interchanges only)

    Per query, a virtual SOURCE feeds wait(origin, *) and ride(destination, *) feeds SINK.

    The multimodal graph adds a street node per place. Metro stations are entered and left
    through it, TGSRTC buses board and alight there, and walking edges join places that are
    close together:

        at(s) --access--> wait(s, L) ... ride(s', L, dir) --egress--> out(s') --walk--> at(stop)
        at(stop) --board--> bus(stop, R, i) --ride--> bus(stop', R, i+1) --egress--> at(stop')
        at(a) --walk--> walked(b) --access/board--> ...
    """

    network: Network
    graph: DiGraph = field(default_factory=DiGraph)
    full: DiGraph | None = None
    station_graph: DiGraph = field(default_factory=DiGraph)
    seg_km: dict[str, list[float]] = field(default_factory=dict)
    seg_min: dict[str, list[float]] = field(default_factory=dict)
    offsets: dict[tuple[str, int], list[float]] = field(default_factory=dict)
    index: dict[tuple[str, str], int] = field(default_factory=dict)
    min_scale: float = 1.0

    def __post_init__(self) -> None:
        self._build()
        if self.network.has_buses:
            self._build_multimodal()

    def _build(self) -> None:
        net, svc, crowd = self.network, self.network.service, self.network.crowding
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
                    self.graph.add_edge(w, ride_node(sid, line.id, FORWARD), kind="board", line=line.id, direction=FORWARD, boardings=1)
                if i > 0:
                    self.graph.add_edge(w, ride_node(sid, line.id, BACKWARD), kind="board", line=line.id, direction=BACKWARD, boardings=1)
            for i in range(n - 1):
                a, b = line.stations[i], line.stations[i + 1]
                level = crowd.segment(line.id, a, b) if crowd else 0.0
                common = dict(kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1, crowd=level)
                self.graph.add_edge(ride_node(a, line.id, FORWARD), ride_node(b, line.id, FORWARD), direction=FORWARD, **common)
                self.graph.add_edge(ride_node(b, line.id, BACKWARD), ride_node(a, line.id, BACKWARD), direction=BACKWARD, **common)
                self.station_graph.add_edge(a, b, kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1)
                self.station_graph.add_edge(b, a, kind="ride", time=minutes[i], distance=km[i], line=line.id, stops=1)
        self.min_scale = min(1.0, *scales)

        for sid, walk in net.interchanges.items():
            here = net.lines_at(sid)
            level = crowd.platform(sid) if crowd else 0.0
            for l1 in here:
                for l2 in here:
                    if l1 == l2:
                        continue
                    for d in (FORWARD, BACKWARD):
                        node = ride_node(sid, l1, d)
                        if node in self.graph:
                            self.graph.add_edge(node, wait_node(sid, l2), kind="transfer", time=walk, transfers=1, line=l2, crowd=level)

    def _build_multimodal(self) -> None:
        net, svc, walk = self.network, self.network.service, self.network.walking
        assert walk is not None
        g = DiGraph()
        for e in self.graph.edges():
            if e.kind == "board":
                g.add(replace(e, v=departing_node(*e.v[1:])))
                continue
            g.add(e)
            if e.kind == "ride":
                g.add(replace(e, u=departing_node(*e.u[1:])))
        for sid in net.metro_stations:
            for lid in net.lines_at(sid):
                for start in (street_node(sid), walked_node(sid)):
                    g.add_edge(start, wait_node(sid, lid), kind="access", time=svc.access_min, line=lid)
                for d in (FORWARD, BACKWARD):
                    node = ride_node(sid, lid, d)
                    if node in self.graph:
                        g.add_edge(node, exit_node(sid), kind="egress", time=walk.metro_exit_min, line=lid, direction=d)

        for route in net.buses.values():
            n = len(route.stops)
            for i, stop in enumerate(route.stops):
                here = bus_node(stop, route.id, i)
                if i < n - 1:
                    boarded = departing_node(stop, route.id, i)
                    for start in (street_node(stop), walked_node(stop)):
                        g.add_edge(start, boarded, kind="board", line=route.id, direction=i, boardings=1)
                    for u in (boarded, here):
                        g.add_edge(
                            u,
                            bus_node(route.stops[i + 1], route.id, i + 1),
                            kind="ride",
                            time=route.offsets[i + 1] - route.offsets[i],
                            distance=route.km[i + 1] - route.km[i],
                            line=route.id,
                            direction=i + 1,
                            stops=1,
                        )
                if i > 0:
                    g.add_edge(here, street_node(stop), kind="egress", line=route.id, direction=i)

        places = list(net.stations.values())
        cell = walk.max_km / 111.0
        grid: dict[tuple[int, int], list] = {}
        for st in places:
            grid.setdefault((int(st.lat // cell), int(st.lon // cell)), []).append(st)
        for a in places:
            ci, cj = int(a.lat // cell), int(a.lon // cell)
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    for b in grid.get((ci + di, cj + dj), ()):
                        if a.id == b.id:
                            continue
                        km = haversine_km(a.lat, a.lon, b.lat, b.lon)
                        if km > walk.max_km:
                            continue
                        attrs = dict(kind="walk", time=walk.minutes(km), distance=km * walk.detour)
                        g.add_edge(street_node(a.id), walked_node(b.id), **attrs)
                        if a.kind == "metro":
                            g.add_edge(exit_node(a.id), walked_node(b.id), **attrs)
        self.full = g

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

    def multimodal_query_edges(self, origin: str, destination: str) -> list[Edge]:
        extra = [
            Edge(SOURCE, street_node(origin), kind="start"),
            Edge(street_node(destination), SINK, kind="end"),
            Edge(walked_node(destination), SINK, kind="end"),
        ]
        if self.network.is_metro(destination):
            extra.append(Edge(exit_node(destination), SINK, kind="end"))
        return extra

    def towards(self, line: str, direction: int) -> str:
        if line in self.network.buses:
            return self.network.buses[line].stops[-1]
        first, last = self.network.lines[line].terminals
        return last if direction == FORWARD else first

    def line_info(self, line: str) -> dict:
        if line in self.network.buses:
            r = self.network.buses[line]
            return {"id": line, "mode": "bus", "name": f"Bus {r.number}", "color": BUS_COLOR, "number": r.number}
        l = self.network.lines[line]
        return {"id": line, "mode": "metro", "name": l.name, "color": l.color}

    def straight_km(self, a: str, b: str) -> float:
        return _straight_km(self.network, a, b)


@lru_cache(maxsize=65536)
def _straight_km_cached(key: tuple[float, float, float, float]) -> float:
    return haversine_km(*key)


def _straight_km(net: Network, a: str, b: str) -> float:
    sa, sb = net.stations[a], net.stations[b]
    return _straight_km_cached((sa.lat, sa.lon, sb.lat, sb.lon))
