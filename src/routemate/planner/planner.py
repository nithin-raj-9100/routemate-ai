from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from ..algorithms import (
    PathResult,
    SearchStats,
    astar,
    bfs,
    dijkstra,
    fold,
    pareto_search,
    yen_k_shortest,
    zero_one_bfs,
)
from ..ds.graph import Edge, OverlayGraph
from ..network.model import Network, fmt_hhmm
from ..network.transit_graph import SINK, SOURCE, TransitGraph, station_of, wait_node
from ..nlp.stations import StationIndex
from .itinerary import Itinerary, build_itinerary
from .timetable import Timetable

MODES = {
    "fastest": "Arrive as early as possible",
    "fewest_transfers": "Fewest line changes, then earliest arrival",
    "fewest_stops": "Fewest stations passed, then earliest arrival",
    "shortest_distance": "Least track distance, then earliest arrival",
    "cheapest": "Lowest fare (metro fares depend only on origin and destination)",
}
MODE_ALIASES = {
    "fast": "fastest",
    "quickest": "fastest",
    "time": "fastest",
    "transfers": "fewest_transfers",
    "fewest_changes": "fewest_transfers",
    "changes": "fewest_transfers",
    "stops": "fewest_stops",
    "distance": "shortest_distance",
    "shortest": "shortest_distance",
    "fare": "cheapest",
}
ARRIVE_BY_WINDOW_MIN = 240
ALTERNATIVE_SLACK = 1.6


class PlanningError(Exception):
    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass
class Plan:
    itinerary: Itinerary
    mode: str
    stats: SearchStats
    fare: float
    fare_basis_km: float
    requested_depart: float | None = None
    requested_arrive_by: float | None = None
    notes: list[str] = field(default_factory=list)
    label: str | None = None
    avoid: list[str] = field(default_factory=list)

    @property
    def meets_deadline(self) -> bool | None:
        if self.requested_arrive_by is None:
            return None
        return self.itinerary.arrive_at <= self.requested_arrive_by + 1e-6

    def to_dict(self, network: Network, tg: TransitGraph, include_trace: bool = False) -> dict[str, Any]:
        it = self.itinerary
        st = network.stations
        return {
            "label": self.label,
            "mode": self.mode,
            "origin": {"id": it.origin, "name": st[it.origin].name},
            "destination": {"id": it.destination, "name": st[it.destination].name},
            "summary": {
                "leaveAt": fmt_hhmm(it.depart_at),
                "boardAt": fmt_hhmm(it.board_at) if it.board_at is not None else None,
                "arriveAt": fmt_hhmm(it.arrive_at),
                "totalMinutes": round(it.arrive_at - it.depart_at, 1),
                "inVehicleMinutes": round(it.in_vehicle_min, 1),
                "waitingMinutes": round(it.waiting_min, 1),
                "walkingMinutes": round(it.walking_min, 1),
                "transfers": it.transfers,
                "stops": it.stops,
                "distanceKm": round(it.distance_km, 2),
                "fare": self.fare,
                "fareBasisKm": round(self.fare_basis_km, 2),
                "currency": network.fares.currency,
                "lines": [leg.line for leg in it.legs],
            },
            "request": {
                "departAt": fmt_hhmm(self.requested_depart) if self.requested_depart is not None else None,
                "arriveBy": fmt_hhmm(self.requested_arrive_by) if self.requested_arrive_by is not None else None,
                "meetsDeadline": self.meets_deadline,
                "avoid": [
                    {"id": a, "name": st[a].name, "passesThrough": any(a in leg.stations for leg in it.legs)}
                    for a in self.avoid
                ],
            },
            "steps": it.steps,
            "geometry": [
                {
                    "line": leg.line,
                    "color": network.lines[leg.line].color,
                    "coords": [[st[s].lat, st[s].lon] for s in leg.stations],
                }
                for leg in it.legs
            ],
            "notes": self.notes,
            "search": self.stats.as_dict(include_trace=include_trace),
        }


class RoutePlanner:
    def __init__(self, network: Network, now: Callable[[], float] | None = None) -> None:
        self.network = network
        self.tg = TransitGraph(network)
        self.timetable = Timetable(self.tg)
        self.stations = StationIndex(network)
        self._now = now

    def now(self) -> float:
        if self._now:
            return self._now()
        t = datetime.now(ZoneInfo(self.network.timezone))
        return t.hour * 60 + t.minute + t.second / 60

    def resolve(self, value: str, role: str) -> str:
        sid = self.stations.resolve(value or "")
        if sid is None:
            hint = ", ".join(self.network.stations[s].name for s in self.stations.suggest(value or "", 3))
            raise PlanningError(
                "UNKNOWN_STATION",
                f"Could not find a station called '{value}' for the {role}." + (f" Did you mean: {hint}?" if hint else ""),
            )
        return sid

    def normalize_mode(self, mode: str | None) -> str:
        m = (mode or "fastest").strip().lower().replace("-", "_").replace(" ", "_")
        m = MODE_ALIASES.get(m, m)
        if m not in MODES:
            raise PlanningError("BAD_MODE", f"Unknown mode '{mode}'. Use one of: {', '.join(MODES)}")
        return m

    def clock_extend(self, clock: float, e: Edge) -> float | None:
        if e.kind == "board":
            return self.timetable.next_departure(e.line, e.direction, e.u[1], clock)
        return clock + e.time

    def _lexicographic(self, attr: str) -> Callable[[tuple, Edge], tuple | None]:
        def extend(label: tuple, e: Edge) -> tuple | None:
            c = self.clock_extend(label[1], e)
            return None if c is None else (label[0] + getattr(e, attr), c)

        return extend

    def _heuristic(self, destination: str) -> Callable[[Any], float]:
        svc = self.network.service
        per_km = self.tg.min_scale / svc.max_speed_kmph * 60

        def h(node: Any) -> float:
            if node == SINK:
                return 0.0
            s = station_of(node, destination=destination)
            return self.tg.straight_km(s, destination) * per_km if s else 0.0

        return h

    def _overlay(self, origin: str, destination: str) -> OverlayGraph:
        return OverlayGraph(self.tg.graph, self.tg.query_edges(origin, destination))

    def _blocked(self, avoid: Iterable[str], origin: str, destination: str) -> set:
        """Avoiding a station means never boarding or changing there; trains may still pass through it."""
        return {
            wait_node(sid, lid)
            for sid in avoid
            if sid not in (origin, destination)
            for lid in self.network.lines_at(sid)
        }

    def _search(self, origin: str, destination: str, mode: str, depart: float, blocked: set, trace: bool = False) -> PathResult:
        g = self._overlay(origin, destination)
        if mode in ("fastest", "cheapest"):
            return astar(g, SOURCE, SINK, self._heuristic(destination), extend=self.clock_extend, initial=depart,
                         blocked_nodes=blocked, trace=trace, name="a* (time-dependent)")
        attr = {"fewest_transfers": "transfers", "fewest_stops": "stops", "shortest_distance": "distance"}[mode]
        return dijkstra(g, SOURCE, SINK, extend=self._lexicographic(attr), initial=(0, depart),
                        blocked_nodes=blocked, trace=trace, name=f"dijkstra (lexicographic {attr}, time)")

    def _clocks(self, edges: list[Edge], depart: float) -> list[float]:
        clocks = [depart]
        for e in edges:
            nxt = self.clock_extend(clocks[-1], e)
            if nxt is None:
                raise PlanningError("NO_SERVICE", "The last train leaves before this trip can be completed.")
            clocks.append(nxt)
        return clocks

    @lru_cache(maxsize=4096)
    def fare_between(self, origin: str, destination: str) -> tuple[float, float]:
        """Fare zone is set by the shortest track distance between the two stations, whatever route is ridden."""
        res = dijkstra(self.tg.station_graph, origin, destination, extend=lambda d, e: d + e.distance, initial=0.0)
        km = res.cost if res.found else 0.0
        return self.network.fares.fare_for(km), km

    def _make_plan(self, res: PathResult, origin: str, destination: str, mode: str, depart: float, **kw) -> Plan:
        clocks = self._clocks(res.edges, depart)
        it = build_itinerary(self.tg, res.edges, clocks, origin, destination)
        fare, basis = self.fare_between(origin, destination)
        return Plan(it, mode, res.stats, fare, basis, **kw)

    def _validate(self, origin: str, destination: str, avoid: Iterable[str]) -> tuple[str, str, list[str]]:
        o = self.resolve(origin, "starting point")
        d = self.resolve(destination, "destination")
        if o == d:
            raise PlanningError("SAME_STATION", "Origin and destination are the same station.")
        avoid_ids = [self.resolve(a, "station to avoid") for a in avoid]
        return o, d, avoid_ids

    def _effective_departure(self, requested: float) -> float:
        """Before service starts, plan from the first train instead of counting the night as travel time."""
        svc = self.network.service
        return max(requested, svc.first_departure - svc.access_min)

    def _service_notes(self, requested: float) -> list[str]:
        svc = self.network.service
        if requested < svc.first_departure - svc.access_min:
            return [f"The metro is closed now; this plan uses the first train after {fmt_hhmm(svc.first_departure)}."]
        return []

    def plan(
        self,
        origin: str,
        destination: str,
        *,
        mode: str | None = "fastest",
        depart_at: float | None = None,
        arrive_by: float | None = None,
        avoid: Iterable[str] = (),
        trace: bool = False,
    ) -> Plan:
        o, d, avoid_ids = self._validate(origin, destination, avoid)
        mode = self.normalize_mode(mode)
        blocked = self._blocked(avoid_ids, o, d)
        if arrive_by is not None:
            plan = self._plan_arrive_by(o, d, mode, arrive_by, blocked, trace)
            plan.avoid = avoid_ids
            return plan

        requested = self.now() if depart_at is None else depart_at
        depart = self._effective_departure(requested)
        res = self._search(o, d, mode, depart, blocked, trace)
        if not res.found:
            raise self._no_route(o, d, depart, blocked)
        notes = self._service_notes(requested) + self._mode_notes(mode, o, d)
        if avoid_ids:
            notes.append("No boarding or changing at " + ", ".join(self.network.stations[a].name for a in avoid_ids) + " (trains may pass through).")
        return self._make_plan(res, o, d, mode, depart, requested_depart=depart, notes=notes, avoid=avoid_ids)

    def _mode_notes(self, mode: str, o: str, d: str) -> list[str]:
        if mode == "cheapest":
            fare, _ = self.fare_between(o, d)
            return [f"Metro fares depend only on the two stations, so every route costs ₹{fare:g}; showing the fastest one."]
        return []

    def _plan_arrive_by(self, o: str, d: str, mode: str, deadline: float, blocked: set, trace: bool) -> Plan:
        """Latest departure that still arrives by the deadline.

        Arrival time never decreases as departure time increases (FIFO timetable), so the
        latest feasible departure is found by binary search over whole minutes.
        """
        cache: dict[int, PathResult] = {}

        def arrival(dep: int) -> float:
            if dep not in cache:
                cache[dep] = self._search(o, d, mode, float(dep), blocked)
            res = cache[dep]
            if not res.found:
                return math.inf
            return fold(res.edges, float(dep), self.clock_extend) or math.inf

        lo = int(math.floor(deadline)) - ARRIVE_BY_WINDOW_MIN
        hi = int(math.floor(deadline))
        if arrival(lo) > deadline:
            earliest = max(lo, int(self.network.service.first_departure) - 5)
            res = cache.get(lo) if earliest == lo else self._search(o, d, mode, float(earliest), blocked)
            if not res or not res.found:
                raise self._no_route(o, d, float(earliest), blocked)
            plan = self._make_plan(res, o, d, mode, float(earliest), requested_arrive_by=deadline)
            plan.notes.append(f"No train gets you there by {fmt_hhmm(deadline)}; this is the earliest possible arrival.")
            return plan
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if arrival(mid) <= deadline:
                lo = mid
            else:
                hi = mid - 1
        res = cache[lo]
        plan = self._make_plan(res, o, d, mode, float(lo), requested_arrive_by=deadline)
        plan.stats.algorithm += f" + binary search ({len(cache)} searches)"
        slack = deadline - plan.itinerary.arrive_at
        plan.notes.append(f"Leave by {fmt_hhmm(lo)} to arrive at {fmt_hhmm(plan.itinerary.arrive_at)} ({round(slack)} min to spare).")
        plan.notes.extend(self._mode_notes(mode, o, d))
        return plan

    def _no_route(self, o: str, d: str, depart: float, blocked: set) -> PlanningError:
        if blocked:
            return PlanningError("NO_ROUTE", "Every route needs a change at a station you asked to avoid. Try removing it.", 404)
        last = max(
            self.timetable.last_departure_from(l, dirn, o)
            for l in self.network.lines_at(o)
            for dirn in (1, -1)
        )
        if depart > last:
            return PlanningError(
                "NO_SERVICE",
                f"No more trains today from {self.network.stations[o].name} after {fmt_hhmm(last)}. "
                f"Service resumes at {fmt_hhmm(self.network.service.first_departure)}.",
                404,
            )
        return PlanningError("NO_ROUTE", "No train connection found before service ends.", 404)

    def options(self, origin: str, destination: str, *, depart_at: float | None = None, avoid: Iterable[str] = ()) -> tuple[list[Plan], SearchStats]:
        """Every Pareto-optimal trade-off between arrival time and number of line changes."""
        o, d, avoid_ids = self._validate(origin, destination, avoid)
        depart = self._effective_departure(self.now() if depart_at is None else depart_at)
        blocked = self._blocked(avoid_ids, o, d)
        g = _Blocked(self._overlay(o, d), blocked)

        def extend(v: tuple, e: Edge) -> tuple | None:
            c = self.clock_extend(v[0], e)
            return None if c is None else (c, v[1] + e.transfers)

        paths, stats = pareto_search(g, SOURCE, SINK, extend=extend, initial=(depart, 0))
        plans = [self._make_plan(p, o, d, "pareto", depart, requested_depart=depart) for p in paths]
        if plans:
            fastest = min(plans, key=lambda p: p.itinerary.arrive_at)
            fewest = min(plans, key=lambda p: (p.itinerary.transfers, p.itinerary.arrive_at))
            for p in plans:
                tags = []
                if p is fastest:
                    tags.append("Fastest")
                if p is fewest:
                    tags.append("Fewest changes")
                p.label = " · ".join(tags) or "Balanced"
                if p is not fastest:
                    extra = round(p.itinerary.arrive_at - fastest.itinerary.arrive_at)
                    saved = fastest.itinerary.transfers - p.itinerary.transfers
                    p.notes.append(f"{extra} min slower than the fastest, with {saved} fewer change{'s' if saved != 1 else ''}.")
        return plans, stats

    def alternatives(self, origin: str, destination: str, *, k: int = 3, depart_at: float | None = None) -> tuple[list[Plan], SearchStats]:
        """The k fastest genuinely different routes (Yen's algorithm), dropping absurd detours."""
        o, d, _ = self._validate(origin, destination, ())
        depart = self._effective_departure(self.now() if depart_at is None else depart_at)
        paths, stats = yen_k_shortest(self._overlay(o, d), SOURCE, SINK, max(1, k) * 3, extend=self.clock_extend, initial=depart)
        plans: list[Plan] = []
        seen: set = set()
        for p in paths:
            plan = self._make_plan(p, o, d, "fastest", depart, requested_depart=depart)
            if plan.itinerary.signature in seen:
                continue
            if plans and (plan.itinerary.arrive_at - depart) > ALTERNATIVE_SLACK * (plans[0].itinerary.arrive_at - depart) + 5:
                break
            seen.add(plan.itinerary.signature)
            plan.label = "Best" if not plans else f"Alternative {len(plans)}"
            plans.append(plan)
            if len(plans) == k:
                break
        return plans, stats

    def compare(self, origin: str, destination: str, *, depart_at: float | None = None, include_trace: bool = True) -> dict[str, Any]:
        """Run every algorithm on the same query and report what each found and how hard it worked."""
        o, d, _ = self._validate(origin, destination, ())
        depart = self._effective_departure(self.now() if depart_at is None else depart_at)
        g = self._overlay(o, d)
        expected = self.timetable.expected_wait(depart)

        def static(label: float, e: Edge) -> float:
            return label + (expected if e.kind == "board" else e.time)

        def stations_of(nodes: list) -> list[str]:
            out: list[str] = []
            for n in nodes:
                s = station_of(n, o, d) if isinstance(n, tuple) else n
                if s and (not out or out[-1] != s):
                    out.append(s)
            return out

        runs: list[tuple[str, str, PathResult, Callable[[PathResult], dict]]] = []
        runs.append(("dijkstra_td", "Dijkstra (time-dependent)",
                     dijkstra(g, SOURCE, SINK, extend=self.clock_extend, initial=depart, trace=True),
                     lambda r: {"arriveAt": fmt_hhmm(r.cost), "minutes": round(r.cost - depart, 1)}))
        runs.append(("astar_td", "A* (time-dependent)",
                     astar(g, SOURCE, SINK, self._heuristic(d), extend=self.clock_extend, initial=depart, trace=True),
                     lambda r: {"arriveAt": fmt_hhmm(r.cost), "minutes": round(r.cost - depart, 1)}))
        runs.append(("dijkstra_static", "Dijkstra (average waits)",
                     dijkstra(g, SOURCE, SINK, extend=static, initial=0.0, trace=True),
                     lambda r: {"minutes": round(r.cost, 1)}))
        runs.append(("astar_static", "A* (average waits)",
                     astar(g, SOURCE, SINK, self._heuristic(d), extend=static, initial=0.0, trace=True),
                     lambda r: {"minutes": round(r.cost, 1)}))
        runs.append(("bfs", "BFS (fewest stops)",
                     bfs(self.tg.station_graph, o, d, trace=True),
                     lambda r: {"stops": r.cost}))
        runs.append(("zero_one_bfs", "0-1 BFS (fewest line changes)",
                     zero_one_bfs(g, SOURCE, SINK, weight=lambda e: e.transfers, trace=True),
                     lambda r: {"transfers": r.cost}))

        results = []
        for key, title, res, summarize in runs:
            entry = {
                "key": key,
                "title": title,
                "found": res.found,
                "result": summarize(res) if res.found else None,
                "route": stations_of(res.nodes) if res.found else [],
                "stats": res.stats.as_dict(),
            }
            if include_trace:
                entry["exploration"] = stations_of(res.stats.trace)
            results.append(entry)

        _, pareto_stats = self.options(o, d, depart_at=depart)
        _, yen_stats = self.alternatives(o, d, k=3, depart_at=depart)
        results.append({"key": "pareto", "title": "Pareto label-setting (time × changes)", "found": True, "result": None, "route": [], "stats": pareto_stats.as_dict()})
        results.append({"key": "yen", "title": "Yen's k-shortest paths (k=3)", "found": True, "result": None, "route": [], "stats": yen_stats.as_dict()})

        dj, ast = results[0]["stats"], results[1]["stats"]
        saving = 1 - ast["settled"] / dj["settled"] if dj["settled"] else 0.0
        return {
            "origin": o,
            "destination": d,
            "departAt": fmt_hhmm(depart),
            "graph": {"nodes": len(self.tg.graph), "edges": self.tg.graph.edge_count, "stations": len(self.network.stations)},
            "algorithms": results,
            "insights": [
                f"A* settled {ast['settled']} nodes vs Dijkstra's {dj['settled']} ({saving:.0%} less work) and found the same arrival time.",
                "Average-wait searches ignore the real timetable, so their times are estimates; time-dependent searches use actual train departures.",
            ],
        }


class _Blocked:
    def __init__(self, graph: OverlayGraph, blocked: set) -> None:
        self.graph = graph
        self.blocked = blocked

    def out_edges(self, u):
        if not self.blocked:
            return self.graph.out_edges(u)
        return [e for e in self.graph.out_edges(u) if e.v not in self.blocked]
