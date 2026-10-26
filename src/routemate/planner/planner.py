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
from ..network.model import Network, crowd_label, fmt_hhmm
from ..network.transit_graph import (
    SINK,
    SOURCE,
    TransitGraph,
    exit_node,
    station_of,
    street_node,
    wait_node,
    walked_node,
)
from ..nlp.stations import StationIndex
from .fares import FareRules, FareStateGraph, project
from .itinerary import Itinerary, build_itinerary
from .timetable import Timetable

MODES = {
    "fastest": "Arrive as early as possible",
    "fewest_transfers": "Fewest line changes, then earliest arrival",
    "fewest_stops": "Fewest stations passed, then earliest arrival",
    "shortest_distance": "Least track distance, then earliest arrival",
    "cheapest": "Lowest total fare, comparing the metro with TGSRTC buses and short walks",
    "least_crowded": "Least crowded metro trains (modelled from published ridership), then earliest arrival",
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
    "crowd": "least_crowded",
    "crowded": "least_crowded",
    "less_crowded": "least_crowded",
    "quiet": "least_crowded",
}
LEXICOGRAPHIC: dict[str, Callable[[Edge], float]] = {
    "fewest_transfers": lambda e: e.boardings,
    "fewest_stops": lambda e: e.stops,
    "shortest_distance": lambda e: e.distance,
    "least_crowded": lambda e: e.crowd_cost,
}
ARRIVE_BY_WINDOW_MIN = 240
ALTERNATIVE_SLACK = 1.6
CROWD_EPSILON = 0.5
OPTION_SLACK = 1.5
OPTION_MAX_BOARDINGS = 4


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
    fare_basis_km: float | None
    requested_depart: float | None = None
    requested_arrive_by: float | None = None
    notes: list[str] = field(default_factory=list)
    label: str | None = None
    avoid: list[str] = field(default_factory=list)
    tickets: list[dict[str, Any]] = field(default_factory=list)
    buses: bool = False

    @property
    def meets_deadline(self) -> bool | None:
        if self.requested_arrive_by is None:
            return None
        return self.itinerary.arrive_at <= self.requested_arrive_by + 1e-6

    def to_dict(self, network: Network, tg: TransitGraph, include_trace: bool = False) -> dict[str, Any]:
        it = self.itinerary
        st = network.stations
        crowd = it.crowd_level
        return {
            "label": self.label,
            "mode": self.mode,
            "buses": self.buses,
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
                "walkingKm": round(it.walk_km, 2),
                "transfers": it.transfers,
                "stops": it.stops,
                "distanceKm": round(it.distance_km, 2),
                "fare": self.fare,
                "fareBasisKm": round(self.fare_basis_km, 2) if self.fare_basis_km is not None else None,
                "tickets": [
                    {
                        **t,
                        "fromName": st[t["from"]].name,
                        "toName": st[t["to"]].name,
                        "lineName": tg.line_info(t["line"])["name"] if t["line"] else "Metro",
                    }
                    for t in self.tickets
                ],
                "currency": network.fares.currency,
                "lines": [leg.line for leg in it.legs],
                "modes": it.modes,
                "crowd": None if crowd is None else {"level": round(crowd, 2), "label": crowd_label(crowd)},
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
                    "type": leg.mode,
                    "line": leg.line,
                    "color": tg.line_info(leg.line)["color"],
                    "coords": [[st[s].lat, st[s].lon] for s in leg.stations],
                }
                for leg in it.legs
            ]
            + [
                {"type": "walk", "line": None, "color": "#6b7280", "coords": [[st[w.start].lat, st[w.start].lon], [st[w.end].lat, st[w.end].lon]]}
                for w in it.walks
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
        bus_fares = network.bus_fares
        self.fares = FareRules(
            metro_fare=lambda a, b: self.fare_between(a, b)[0],
            bus_fare=bus_fares.fare_for if bus_fares else (lambda km: 0.0),
            bus_km=lambda route, seq: network.buses[route].km[seq],
            is_bus=lambda line: line in network.buses,
        )

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
            if e.line in self.network.buses:
                return self.timetable.next_bus(e.line, e.direction, clock)
            node = e.u[0] if isinstance(e.u[0], tuple) else e.u  # fare-state edges wrap the node as (node, ticket)
            return self.timetable.next_departure(e.line, e.direction, node[1], clock)
        return clock + e.time

    def _lexicographic(self, cost: Callable[[Edge], float]) -> Callable[[tuple, Edge], tuple | None]:
        def extend(label: tuple, e: Edge) -> tuple | None:
            c = self.clock_extend(label[1], e)
            return None if c is None else (label[0] + cost(e), c)

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

    def _overlay(self, origin: str, destination: str, multi: bool = False) -> OverlayGraph:
        if multi:
            return OverlayGraph(self.tg.full, self.tg.multimodal_query_edges(origin, destination))
        return OverlayGraph(self.tg.graph, self.tg.query_edges(origin, destination))

    def _blocked(self, avoid: Iterable[str], origin: str, destination: str) -> set:
        """Avoiding a station means never boarding, changing or getting off there; trains may still pass through it."""
        out: set = set()
        for sid in avoid:
            if sid in (origin, destination):
                continue
            out |= {wait_node(sid, lid) for lid in self.network.lines_at(sid)}
            out |= {street_node(sid), exit_node(sid), walked_node(sid)}
        return out

    def uses_buses(self, origin: str, destination: str, mode: str, buses: bool) -> bool:
        if not self.network.has_buses:
            return False
        if not (self.network.is_metro(origin) and self.network.is_metro(destination)):
            return True
        return mode != "least_crowded" and (buses or mode == "cheapest")

    def crowd_factor(self, minute: float) -> float:
        return self.network.crowding.factor(minute) if self.network.crowding else 0.0

    def _search(self, origin: str, destination: str, mode: str, depart: float, blocked: set, trace: bool = False, multi: bool = False) -> PathResult:
        g = self._overlay(origin, destination, multi)
        if mode == "cheapest" and multi:
            fg = FareStateGraph(g, self.fares, blocked)

            def extend(label: tuple, e: Edge) -> tuple | None:
                c = self.clock_extend(label[1], e)
                return None if c is None else (label[0] + e.fare, c)

            res = dijkstra(fg, (SOURCE, None), (SINK, None), extend=extend, initial=(0.0, depart), trace=trace,
                           name="dijkstra (lexicographic fare, time; metro + bus)")
            if res.found:
                res.edges = project(res.edges)
                res.nodes = [n for n, _ in res.nodes]
            res.stats.trace = [n for n, _ in res.stats.trace]
            return res
        suffix = "; metro + bus" if multi else ""
        if mode in ("fastest", "cheapest"):
            return astar(g, SOURCE, SINK, self._heuristic(destination), extend=self.clock_extend, initial=depart,
                         blocked_nodes=blocked, trace=trace, name=f"a* (time-dependent{suffix})")
        cost = LEXICOGRAPHIC[mode]
        what = {"fewest_transfers": "boardings", "fewest_stops": "stops", "shortest_distance": "distance", "least_crowded": "crowd"}[mode]
        return dijkstra(g, SOURCE, SINK, extend=self._lexicographic(cost), initial=(0, depart),
                        blocked_nodes=blocked, trace=trace, name=f"dijkstra (lexicographic {what}, time{suffix})")

    def _clocks(self, edges: list[Edge], depart: float) -> list[float]:
        clocks = [depart]
        for e in edges:
            nxt = self.clock_extend(clocks[-1], e)
            if nxt is None:
                raise PlanningError("NO_SERVICE", "The last train or bus leaves before this trip can be completed.")
            clocks.append(nxt)
        return clocks

    @lru_cache(maxsize=4096)
    def fare_between(self, origin: str, destination: str) -> tuple[float, float]:
        """Fare zone is set by the shortest track distance between the two stations, whatever route is ridden."""
        res = dijkstra(self.tg.station_graph, origin, destination, extend=lambda d, e: d + e.distance, initial=0.0)
        km = res.cost if res.found else 0.0
        return self.network.fares.fare_for(km), km

    def _make_plan(self, res: PathResult, origin: str, destination: str, mode: str, depart: float, multi: bool = False, **kw) -> Plan:
        clocks = self._clocks(res.edges, depart)
        it = build_itinerary(self.tg, res.edges, clocks, origin, destination, self.crowd_factor(depart))
        tickets = self.fares.breakdown(res.edges)
        metro = [t for t in tickets if t["mode"] == "metro"]
        basis = self.fare_between(metro[0]["from"], metro[0]["to"])[1] if len(tickets) == 1 and metro else None
        return Plan(it, mode, res.stats, sum(t["fare"] for t in tickets), basis, tickets=tickets, buses=multi, **kw)

    def _validate(self, origin: str, destination: str, avoid: Iterable[str]) -> tuple[str, str, list[str]]:
        o = self.resolve(origin, "starting point")
        d = self.resolve(destination, "destination")
        if o == d:
            raise PlanningError("SAME_STATION", "Origin and destination are the same station.")
        avoid_ids = [self.resolve(a, "station to avoid") for a in avoid]
        return o, d, avoid_ids

    def _metro_only(self, o: str, d: str, what: str) -> None:
        if not (self.network.is_metro(o) and self.network.is_metro(d)):
            raise PlanningError("METRO_ONLY", f"{what} works between metro stations only.")

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
        buses: bool = False,
    ) -> Plan:
        o, d, avoid_ids = self._validate(origin, destination, avoid)
        mode = self.normalize_mode(mode)
        multi = self.uses_buses(o, d, mode, buses)
        notes: list[str] = []
        if mode == "least_crowded" and multi:
            notes.append("Crowding is only modelled for the metro, so this bus trip is planned for the fastest arrival.")
            mode = "fastest"
        blocked = self._blocked(avoid_ids, o, d)
        if arrive_by is not None:
            plan = self._plan_arrive_by(o, d, mode, arrive_by, blocked, trace, multi)
            plan.avoid = avoid_ids
            plan.notes[:0] = notes
            return plan

        requested = self.now() if depart_at is None else depart_at
        depart = self._effective_departure(requested)
        res = self._search(o, d, mode, depart, blocked, trace, multi)
        if not res.found:
            raise self._no_route(o, d, depart, blocked)
        plan = self._make_plan(res, o, d, mode, depart, multi, requested_depart=depart, avoid=avoid_ids)
        plan.notes = notes + self._service_notes(requested) + self._mode_notes(plan, o, d)
        if avoid_ids:
            plan.notes.append("No boarding or changing at " + ", ".join(self.network.stations[a].name for a in avoid_ids) + " (trains may pass through).")
        return plan

    def _mode_notes(self, plan: Plan, o: str, d: str) -> list[str]:
        notes: list[str] = []
        both_metro = self.network.is_metro(o) and self.network.is_metro(d)
        if plan.mode == "cheapest" and not plan.buses:
            notes.append(f"Metro fares depend only on the two stations, so every route costs ₹{plan.fare:g}; showing the fastest one.")
        elif plan.mode == "cheapest" and both_metro:
            metro_fare = self.fare_between(o, d)[0]
            if plan.fare < metro_fare:
                notes.append(f"₹{plan.fare:g} with buses and walking instead of ₹{metro_fare:g} by metro alone.")
            else:
                notes.append(f"No bus combination beats the metro fare of ₹{metro_fare:g}.")
        if plan.mode == "least_crowded":
            notes.append("Crowding is modelled from published HMRL station counts and peak hours, not live data.")
        if "bus" in plan.itinerary.modes:
            notes.append("Bus times come from the TGSRTC timetable (Open Data Telangana); road traffic can delay buses.")
        return notes

    def _plan_arrive_by(self, o: str, d: str, mode: str, deadline: float, blocked: set, trace: bool, multi: bool = False) -> Plan:
        """Latest departure that still arrives by the deadline.

        Arrival time never decreases as departure time increases (FIFO timetable), so the
        latest feasible departure is found by binary search over whole minutes.
        """
        cache: dict[int, PathResult] = {}

        def arrival(dep: int) -> float:
            if dep not in cache:
                cache[dep] = self._search(o, d, mode, float(dep), blocked, multi=multi)
            res = cache[dep]
            if not res.found:
                return math.inf
            return fold(res.edges, float(dep), self.clock_extend) or math.inf

        lo = int(math.floor(deadline)) - ARRIVE_BY_WINDOW_MIN
        hi = int(math.floor(deadline))
        if arrival(lo) > deadline:
            earliest = max(lo, int(self.network.service.first_departure) - 5)
            res = cache.get(lo) if earliest == lo else self._search(o, d, mode, float(earliest), blocked, multi=multi)
            if not res or not res.found:
                raise self._no_route(o, d, float(earliest), blocked)
            plan = self._make_plan(res, o, d, mode, float(earliest), multi, requested_arrive_by=deadline)
            plan.notes.append(f"No train gets you there by {fmt_hhmm(deadline)}; this is the earliest possible arrival.")
            return plan
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if arrival(mid) <= deadline:
                lo = mid
            else:
                hi = mid - 1
        res = cache[lo]
        plan = self._make_plan(res, o, d, mode, float(lo), multi, requested_arrive_by=deadline)
        plan.stats.algorithm += f" + binary search ({len(cache)} searches)"
        slack = deadline - plan.itinerary.arrive_at
        plan.notes.append(f"Leave by {fmt_hhmm(lo)} to arrive at {fmt_hhmm(plan.itinerary.arrive_at)} ({round(slack)} min to spare).")
        plan.notes.extend(self._mode_notes(plan, o, d))
        return plan

    def _no_route(self, o: str, d: str, depart: float, blocked: set) -> PlanningError:
        if blocked:
            return PlanningError("NO_ROUTE", "Every route needs a change at a station you asked to avoid. Try removing it.", 404)
        if self.network.lines_at(o):
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
        return PlanningError("NO_ROUTE", "No train or bus connection found before service ends.", 404)

    def options(
        self, origin: str, destination: str, *, depart_at: float | None = None, avoid: Iterable[str] = (), buses: bool = False
    ) -> tuple[list[Plan], SearchStats]:
        """Every Pareto-optimal trade-off: arrival time x line changes x crowding (metro) or fare (with buses)."""
        o, d, avoid_ids = self._validate(origin, destination, avoid)
        depart = self._effective_departure(self.now() if depart_at is None else depart_at)
        blocked = self._blocked(avoid_ids, o, d)
        multi = self.uses_buses(o, d, "fastest", buses)

        if multi:
            g: Any = FareStateGraph(self._overlay(o, d, True), self.fares, blocked)
            source, target = (SOURCE, None), (SINK, None)
            # Bus networks have a huge Pareto front of slow, cheap detours; only keep trips a rider
            # would consider next to the fastest one.
            fastest = self._search(o, d, "fastest", depart, blocked, multi=True)
            if not fastest.found:
                raise self._no_route(o, d, depart, blocked)
            latest = depart + OPTION_SLACK * (fastest.cost - depart) + 15

            def extend(v: tuple, e: Edge) -> tuple | None:
                c = self.clock_extend(v[0], e)
                if c is None or c > latest or v[1] + e.boardings > OPTION_MAX_BOARDINGS:
                    return None
                return (c, v[1] + e.boardings, v[2] + e.fare)
        else:
            g, source, target = _Blocked(self._overlay(o, d), blocked), SOURCE, SINK

            def extend(v: tuple, e: Edge) -> tuple | None:
                c = self.clock_extend(v[0], e)
                # Rounded so near-identical crowding doesn't multiply the front into look-alike routes.
                crowd = round((v[2] + e.crowd_cost) / CROWD_EPSILON) * CROWD_EPSILON
                return None if c is None else (c, v[1] + e.boardings, crowd)

        paths, stats = pareto_search(g, source, target, extend=extend, initial=(depart, 0, 0.0))
        if multi:
            for p in paths:
                p.edges = project(p.edges)
        plans = _distinct([self._make_plan(p, o, d, "pareto", depart, multi, requested_depart=depart) for p in paths], multi)
        _label_options(plans, multi)
        return plans, stats

    def alternatives(self, origin: str, destination: str, *, k: int = 3, depart_at: float | None = None) -> tuple[list[Plan], SearchStats]:
        """The k fastest genuinely different routes (Yen's algorithm), dropping absurd detours."""
        o, d, _ = self._validate(origin, destination, ())
        self._metro_only(o, d, "Alternative routes")
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
        self._metro_only(o, d, "The algorithm comparison")
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
        results.append({"key": "pareto", "title": "Pareto label-setting (time × changes × crowding)", "found": True, "result": None, "route": [], "stats": pareto_stats.as_dict()})
        results.append({"key": "yen", "title": "Yen's k-shortest paths (k=3)", "found": True, "result": None, "route": [], "stats": yen_stats.as_dict()})

        dj, ast = results[0]["stats"], results[1]["stats"]
        saving = 1 - ast["settled"] / dj["settled"] if dj["settled"] else 0.0
        return {
            "origin": o,
            "destination": d,
            "departAt": fmt_hhmm(depart),
            "graph": {"nodes": len(self.tg.graph), "edges": self.tg.graph.edge_count, "stations": len(self.network.metro_stations)},
            "algorithms": results,
            "insights": [
                f"A* settled {ast['settled']} nodes vs Dijkstra's {dj['settled']} ({saving:.0%} less work) and found the same arrival time.",
                "Average-wait searches ignore the real timetable, so their times are estimates; time-dependent searches use actual train departures.",
            ],
        }


def _distinct(plans: list[Plan], multi: bool) -> list[Plan]:
    """Drop options that only win by seconds or by rounding: compared in whole minutes, rupees and 0.05 crowd steps."""

    def key(p: Plan) -> tuple:
        third = p.fare if multi else round((p.itinerary.crowd_level or 0.0) / 0.05)
        return (round(p.itinerary.arrive_at), p.itinerary.transfers, third)

    keys = [key(p) for p in plans]
    keep = []
    for i, p in enumerate(plans):
        beaten = any(j != i and all(a <= b for a, b in zip(keys[j], keys[i])) and (keys[j] != keys[i] or j < i) for j in range(len(plans)))
        if not beaten:
            keep.append(p)
    return keep


def _label_options(plans: list[Plan], multi: bool) -> None:
    if not plans:
        return
    fastest = min(plans, key=lambda p: p.itinerary.arrive_at)
    picks = [(fastest, "Fastest"), (min(plans, key=lambda p: (p.itinerary.transfers, p.itinerary.arrive_at)), "Fewest changes")]
    if multi:
        picks.append((min(plans, key=lambda p: (p.fare, p.itinerary.arrive_at)), "Cheapest"))
    else:
        picks.append((min(plans, key=lambda p: (p.itinerary.crowd_level or 0.0, p.itinerary.arrive_at)), "Least crowded"))
    for p in plans:
        p.label = " · ".join(tag for pick, tag in picks if pick is p) or "Balanced"
        if p is fastest:
            continue
        extra = round(p.itinerary.arrive_at - fastest.itinerary.arrive_at)
        gains = []
        saved = fastest.itinerary.transfers - p.itinerary.transfers
        if saved > 0:
            gains.append(f"{saved} fewer change{'s' if saved != 1 else ''}")
        if multi and p.fare < fastest.fare:
            gains.append(f"₹{fastest.fare - p.fare:g} cheaper")
        if not multi and (p.itinerary.crowd_level or 0) < (fastest.itinerary.crowd_level or 0) - 0.02:
            gains.append("less crowded")
        p.notes.append(f"{extra} min slower than the fastest" + (", but " + " and ".join(gains) if gains else "") + ".")


class _Blocked:
    def __init__(self, graph: OverlayGraph, blocked: set) -> None:
        self.graph = graph
        self.blocked = blocked

    def out_edges(self, u):
        if not self.blocked:
            return self.graph.out_edges(u)
        return [e for e in self.graph.out_edges(u) if e.v not in self.blocked]
