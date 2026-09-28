from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ds.graph import Edge
from ..network.model import Network, fmt_hhmm
from ..network.transit_graph import TransitGraph


@dataclass
class Leg:
    line: str
    towards: str
    stations: list[str]
    depart_at: float
    arrive_at: float
    distance_km: float = 0.0

    @property
    def minutes(self) -> float:
        return self.arrive_at - self.depart_at

    @property
    def stop_count(self) -> int:
        return len(self.stations) - 1


@dataclass
class Itinerary:
    origin: str
    destination: str
    depart_at: float
    arrive_at: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    waiting_min: float = 0.0
    walking_min: float = 0.0

    @property
    def transfers(self) -> int:
        return max(0, len(self.legs) - 1)

    @property
    def in_vehicle_min(self) -> float:
        return sum(l.minutes for l in self.legs)

    @property
    def stops(self) -> int:
        return sum(l.stop_count for l in self.legs)

    @property
    def distance_km(self) -> float:
        return sum(l.distance_km for l in self.legs)

    @property
    def board_at(self) -> float | None:
        return self.legs[0].depart_at if self.legs else None

    @property
    def signature(self) -> tuple:
        return tuple((l.line, l.stations[0], l.stations[-1]) for l in self.legs)


def build_itinerary(
    tg: TransitGraph,
    edges: list[Edge],
    clocks: list[float],
    origin: str,
    destination: str,
) -> Itinerary:
    """Turn a node-level path plus the clock at every node into human steps and train legs."""
    net = tg.network
    it = Itinerary(origin, destination, clocks[0], clocks[-1])
    leg: Leg | None = None
    for i, e in enumerate(edges):
        before, after = clocks[i], clocks[i + 1]
        if e.kind == "access":
            it.walking_min += e.time
            it.steps.append(
                {
                    "type": "walk",
                    "station": origin,
                    "minutes": round(e.time, 1),
                    "text": f"Enter {net.stations[origin].name} and go to the platform",
                }
            )
        elif e.kind == "board":
            station = e.u[1]
            wait = after - before
            it.waiting_min += wait
            towards = tg.towards(e.line, e.direction)
            it.steps.append(
                {
                    "type": "wait",
                    "station": station,
                    "line": e.line,
                    "towards": towards,
                    "minutes": round(wait, 1),
                    "trainAt": fmt_hhmm(after),
                    "text": (
                        f"Wait {round(wait)} min for the {net.lines[e.line].name} train towards {net.stations[towards].name}"
                        if wait >= 0.5
                        else f"Board the {net.lines[e.line].name} train towards {net.stations[towards].name} at {fmt_hhmm(after)}"
                    ),
                }
            )
            leg = Leg(e.line, towards, [station], after, after)
        elif e.kind == "ride":
            assert leg is not None
            leg.stations.append(e.v[1])
            leg.arrive_at = after
            leg.distance_km += e.distance
        elif e.kind in ("transfer", "alight"):
            if leg is not None:
                it.legs.append(leg)
                it.steps.append(_ride_step(net, leg))
                leg = None
            if e.kind == "transfer":
                station = e.u[1]
                it.walking_min += e.time
                it.steps.append(
                    {
                        "type": "transfer",
                        "station": station,
                        "fromLine": e.u[2],
                        "toLine": e.line,
                        "minutes": round(e.time, 1),
                        "text": f"Change at {net.stations[station].name}: walk to the {net.lines[e.line].name} platform",
                    }
                )
    return it


def _ride_step(net: Network, leg: Leg) -> dict[str, Any]:
    line = net.lines[leg.line]
    return {
        "type": "ride",
        "line": leg.line,
        "lineName": line.name,
        "color": line.color,
        "towards": leg.towards,
        "towardsName": net.stations[leg.towards].name,
        "from": leg.stations[0],
        "to": leg.stations[-1],
        "stations": leg.stations,
        "stopCount": leg.stop_count,
        "departAt": fmt_hhmm(leg.depart_at),
        "arriveAt": fmt_hhmm(leg.arrive_at),
        "minutes": round(leg.minutes, 1),
        "distanceKm": round(leg.distance_km, 2),
        "text": (
            f"Ride the {line.name} from {net.stations[leg.stations[0]].name} to "
            f"{net.stations[leg.stations[-1]].name} ({leg.stop_count} stop{'s' if leg.stop_count != 1 else ''})"
        ),
    }
