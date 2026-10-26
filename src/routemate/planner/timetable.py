from __future__ import annotations

from bisect import bisect_left

from ..network.transit_graph import TransitGraph


class Timetable:
    """Deterministic train schedule derived from the headway bands.

    Trains leave each terminal at the same clock times; a train reaches station i at
    departure + running offset(i). Because the schedule is fixed, "leave later, arrive later"
    always holds (FIFO), which is what keeps time-dependent Dijkstra correct.
    """

    def __init__(self, tg: TransitGraph) -> None:
        self.tg = tg
        svc = tg.network.service
        departures: list[float] = []
        t = svc.first_departure
        while t <= svc.last_departure + 1e-9:
            departures.append(t)
            t += svc.headway_at(t)
        self.terminal_departures = departures

    def next_bus(self, route: str, seq: int, clock: float) -> float | None:
        """Real TGSRTC departure times from the first stop, shifted by the median run time to stop `seq`."""
        r = self.tg.network.buses[route]
        off = r.offsets[seq]
        j = bisect_left(r.departures, clock - off - 1e-9)
        return None if j == len(r.departures) else r.departures[j] + off

    def offset(self, line: str, direction: int, station: str) -> float:
        return self.tg.offsets[(line, direction)][self.tg.index[(line, station)]]

    def next_departure(self, line: str, direction: int, station: str, clock: float) -> float | None:
        off = self.offset(line, direction, station)
        deps = self.terminal_departures
        j = bisect_left(deps, clock - off - 1e-9)
        return None if j == len(deps) else deps[j] + off

    def last_departure_from(self, line: str, direction: int, station: str) -> float:
        return self.terminal_departures[-1] + self.offset(line, direction, station)

    def first_departure_from(self, line: str, direction: int, station: str) -> float:
        return self.terminal_departures[0] + self.offset(line, direction, station)

    def expected_wait(self, clock: float) -> float:
        return self.tg.network.service.headway_at(clock) / 2
