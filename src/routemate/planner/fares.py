from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable

from ..ds.graph import Edge

Ticket = tuple | None


@dataclass
class FareRules:
    """Who charges what, and when.

    A metro fare depends on where you entered and where you leave, and a bus ticket on how far
    you rode, so neither is known when you board. The open ticket (metro entry station, or bus
    route + boarding stop) is carried along until the ride ends, and the fare is charged then.
    """

    metro_fare: Callable[[str, str], float]
    bus_fare: Callable[[float], float]
    bus_km: Callable[[str, int], float]
    is_bus: Callable[[str], bool]

    def step(self, ticket: Ticket, e: Edge) -> tuple[Ticket, float]:
        if e.kind == "access":
            return ("metro", e.v[1]), 0.0
        if e.kind == "board" and self.is_bus(e.line):
            return ("bus", e.line, e.direction), 0.0
        if e.kind in ("egress", "alight") and ticket is not None:
            if ticket[0] == "metro":
                return None, self.metro_fare(ticket[1], e.u[1])
            _, route, boarded = ticket
            return None, self.bus_fare(self.bus_km(route, e.direction) - self.bus_km(route, boarded))
        return ticket, 0.0

    def breakdown(self, edges: list[Edge]) -> list[dict[str, Any]]:
        """Every ticket bought along a path: what it was for and what it cost."""
        out: list[dict[str, Any]] = []
        ticket: Ticket = None
        start: str | None = None
        for e in edges:
            before = ticket
            ticket, fare = self.step(ticket, e)
            if before is None and ticket is not None:
                start = e.u[1] if ticket[0] == "bus" else e.v[1]
            if before is not None and ticket is None:
                out.append({"mode": before[0], "line": before[1] if before[0] == "bus" else None, "from": start, "to": e.u[1], "fare": fare})
        return out


class FareStateGraph:
    """The base graph with the open ticket folded into every node: state = (node, ticket).

    Each edge copy carries the fare it charges, so (fare, time) labels stay additive and a plain
    lexicographic Dijkstra or Pareto search finds the cheapest route exactly.
    """

    def __init__(self, base: Any, rules: FareRules, blocked: set | frozenset = frozenset()) -> None:
        self.base = base
        self.rules = rules
        self.blocked = blocked

    def out_edges(self, state: tuple) -> list[Edge]:
        node, ticket = state
        out = []
        for e in self.base.out_edges(node):
            if e.v in self.blocked:
                continue
            nxt, fare = self.rules.step(ticket, e)
            out.append(replace(e, u=state, v=(e.v, nxt), fare=fare))
        return out


def project(edges: list[Edge]) -> list[Edge]:
    """Map a path found in the fare-state graph back onto plain graph nodes (fares are kept)."""
    return [replace(e, u=e.u[0], v=e.v[0]) for e in edges]
