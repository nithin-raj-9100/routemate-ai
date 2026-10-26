from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..ds.graph import Edge
from ..network.model import crowd_label, fmt_hhmm
from ..network.transit_graph import TransitGraph


@dataclass
class Leg:
    line: str
    towards: str
    stations: list[str]
    depart_at: float
    arrive_at: float
    distance_km: float = 0.0
    mode: str = "metro"
    crowd_exposure: float = 0.0

    @property
    def minutes(self) -> float:
        return self.arrive_at - self.depart_at

    @property
    def stop_count(self) -> int:
        return len(self.stations) - 1

    @property
    def crowd_level(self) -> float | None:
        if self.mode != "metro" or self.minutes <= 0:
            return None
        return self.crowd_exposure / self.minutes


@dataclass
class Walk:
    start: str
    end: str
    minutes: float
    distance_km: float


@dataclass
class Itinerary:
    origin: str
    destination: str
    depart_at: float
    arrive_at: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    legs: list[Leg] = field(default_factory=list)
    walks: list[Walk] = field(default_factory=list)
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
    def walk_km(self) -> float:
        return sum(w.distance_km for w in self.walks)

    @property
    def board_at(self) -> float | None:
        return self.legs[0].depart_at if self.legs else None

    @property
    def signature(self) -> tuple:
        return tuple((l.line, l.stations[0], l.stations[-1]) for l in self.legs)

    @property
    def modes(self) -> list[str]:
        out = [l.mode for l in self.legs] + (["walk"] if self.walks else [])
        return list(dict.fromkeys(out))

    @property
    def crowd_level(self) -> float | None:
        metro = [l for l in self.legs if l.mode == "metro"]
        minutes = sum(l.minutes for l in metro)
        return sum(l.crowd_exposure for l in metro) / minutes if minutes > 0 else None


def build_itinerary(
    tg: TransitGraph,
    edges: list[Edge],
    clocks: list[float],
    origin: str,
    destination: str,
    crowd_factor: float = 0.0,
) -> Itinerary:
    """Turn a node-level path plus the clock at every node into human steps and vehicle legs."""
    net = tg.network
    name = lambda sid: net.stations[sid].name
    it = Itinerary(origin, destination, clocks[0], clocks[-1])
    leg: Leg | None = None

    def close_leg() -> None:
        nonlocal leg
        if leg is not None:
            it.legs.append(leg)
            it.steps.append(_ride_step(tg, leg))
            leg = None

    for i, e in enumerate(edges):
        before, after = clocks[i], clocks[i + 1]
        if e.kind == "access":
            station = e.v[1]
            it.walking_min += e.time
            it.steps.append(
                {"type": "walk", "station": station, "minutes": round(e.time, 1), "text": f"Enter {name(station)} and go to the platform"}
            )
        elif e.kind == "board":
            station = e.u[1]
            wait = after - before
            it.waiting_min += wait
            info = tg.line_info(e.line)
            towards = tg.towards(e.line, e.direction)
            vehicle = f"{info['name']} towards {name(towards)}" if info["mode"] == "bus" else f"{info['name']} train towards {name(towards)}"
            it.steps.append(
                {
                    "type": "wait",
                    "mode": info["mode"],
                    "station": station,
                    "line": e.line,
                    "towards": towards,
                    "minutes": round(wait, 1),
                    "trainAt": fmt_hhmm(after),
                    "text": (
                        f"Wait {round(wait)} min for the {vehicle}"
                        + (f" at {name(station)}" if info["mode"] == "bus" else "")
                        if wait >= 0.5
                        else f"Board the {vehicle} at {fmt_hhmm(after)}"
                    ),
                }
            )
            leg = Leg(e.line, towards, [station], after, after, mode=info["mode"])
        elif e.kind == "ride":
            assert leg is not None
            leg.stations.append(e.v[1])
            leg.arrive_at = after
            leg.distance_km += e.distance
            leg.crowd_exposure += e.crowd * crowd_factor * (after - before)
        elif e.kind in ("transfer", "alight", "egress"):
            was_metro = leg is not None and leg.mode == "metro"
            close_leg()
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
                        "text": f"Change at {name(station)}: walk to the {net.lines[e.line].name} platform",
                    }
                )
            elif e.kind == "egress" and was_metro:
                station = e.u[1]
                it.walking_min += e.time
                it.steps.append({"type": "exit", "station": station, "minutes": round(e.time, 1), "text": f"Leave {name(station)} station"})
        elif e.kind == "walk":
            start, end = e.u[1], e.v[1]
            it.walking_min += e.time
            it.walks.append(Walk(start, end, e.time, e.distance))
            metres = int(round(e.distance * 1000, -1))
            it.steps.append(
                {
                    "type": "walk",
                    "from": start,
                    "station": end,
                    "minutes": round(e.time, 1),
                    "distanceM": metres,
                    "text": f"Walk {metres} m from {name(start)} to {name(end)}",
                }
            )
    close_leg()
    return it


def _ride_step(tg: TransitGraph, leg: Leg) -> dict[str, Any]:
    net = tg.network
    info = tg.line_info(leg.line)
    name = lambda sid: net.stations[sid].name
    level = leg.crowd_level
    stop_word = f"stop{'s' if leg.stop_count != 1 else ''}"
    return {
        "type": "ride",
        "mode": info["mode"],
        "line": leg.line,
        "lineName": info["name"],
        "color": info["color"],
        "towards": leg.towards,
        "towardsName": name(leg.towards),
        "from": leg.stations[0],
        "to": leg.stations[-1],
        "stations": leg.stations,
        "stopCount": leg.stop_count,
        "departAt": fmt_hhmm(leg.depart_at),
        "arriveAt": fmt_hhmm(leg.arrive_at),
        "minutes": round(leg.minutes, 1),
        "distanceKm": round(leg.distance_km, 2),
        "crowd": None if level is None else {"level": round(level, 2), "label": crowd_label(level)},
        "text": f"Ride the {info['name']} from {name(leg.stations[0])} to {name(leg.stations[-1])} ({leg.stop_count} {stop_word})",
    }
