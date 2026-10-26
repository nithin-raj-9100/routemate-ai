from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import dataclass, field

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def parse_hhmm(value: str) -> float:
    hh, mm = value.strip().split(":")
    h, m = int(hh), int(mm)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def fmt_hhmm(minutes: float) -> str:
    total = int(round(minutes))
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


@dataclass(frozen=True)
class Station:
    id: str
    name: str
    lat: float
    lon: float
    aliases: tuple[str, ...] = ()
    kind: str = "metro"


@dataclass(frozen=True)
class Line:
    id: str
    name: str
    corridor: str
    color: str
    length_km: float
    stations: tuple[str, ...]

    @property
    def terminals(self) -> tuple[str, str]:
        return self.stations[0], self.stations[-1]


@dataclass(frozen=True)
class HeadwayBand:
    start: float
    end: float
    every: float


@dataclass
class ServiceConfig:
    first_departure: float
    last_departure: float
    avg_run_speed_kmph: float
    max_speed_kmph: float
    dwell_min: float
    access_min: float
    headways: list[HeadwayBand]

    def headway_at(self, minute: float) -> float:
        for band in self.headways:
            if band.start <= minute < band.end:
                return band.every
        return self.headways[-1].every if minute >= self.headways[-1].start else self.headways[0].every


@dataclass
class FareTable:
    slabs: list[tuple[float | None, float]]
    currency: str = "INR"
    source: str = ""
    _limits: list[float] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self._limits = [u for u, _ in self.slabs if u is not None]

    def fare_for(self, km: float) -> float:
        i = bisect_right(self._limits, km - 1e-9)
        return self.slabs[min(i, len(self.slabs) - 1)][1]


@dataclass(frozen=True)
class BusRoute:
    """One TGSRTC route pattern in one direction: real stop order, median run times and every departure."""

    id: str
    number: str
    headsign: str
    stops: tuple[str, ...]
    offsets: tuple[float, ...]
    km: tuple[float, ...]
    departures: tuple[float, ...]


@dataclass(frozen=True)
class WalkConfig:
    speed_kmph: float
    max_km: float
    detour: float
    metro_exit_min: float

    def minutes(self, straight_km: float) -> float:
        return straight_km * self.detour / self.speed_kmph * 60


@dataclass
class Crowding:
    """How full the metro is likely to be, from published daily entry counts and peak windows.

    level = time factor x (60% corridor intensity + 40% busyness of the two stations), in [0, 1].
    Edges store the peak-hour level; a trip scales it by the time factor at its departure.
    """

    line_daily_riders: dict[str, float]
    station_entries: dict[str, float]
    time_bands: list[tuple[float, float, float]]
    source: str = ""
    modelled: str = ""
    _station: dict[str, float] = field(init=False, default_factory=dict)
    _line: dict[str, float] = field(init=False, default_factory=dict)

    def calibrate(self, lines: dict[str, "Line"]) -> None:
        per_km = {lid: self.line_daily_riders.get(lid, 0.0) / lines[lid].length_km for lid in lines}
        top = max(per_km.values()) or 1.0
        self._line = {lid: v / top for lid, v in per_km.items()}
        entries: dict[str, float] = {}
        for lid, line in lines.items():
            avg = self.line_daily_riders.get(lid, 0.0) / len(line.stations)
            for sid in line.stations:
                entries[sid] = max(entries.get(sid, 0.0), self.station_entries.get(sid, avg))
        busiest = max(entries.values()) or 1.0
        self._station = {sid: v / busiest for sid, v in entries.items()}

    def factor(self, minute: float) -> float:
        m = minute % (24 * 60)
        return next((f for start, end, f in self.time_bands if start <= m < end), 0.0)

    def station(self, sid: str) -> float:
        return self._station.get(sid, 0.0)

    def segment(self, line: str, a: str, b: str) -> float:
        return 0.6 * self._line.get(line, 0.0) + 0.4 * (self.station(a) + self.station(b)) / 2

    def platform(self, sid: str) -> float:
        return self.station(sid)


CROWD_LABELS = ((0.3, "Low"), (0.55, "Moderate"), (0.75, "Busy"), (float("inf"), "Packed"))


def crowd_label(level: float) -> str:
    return next(label for limit, label in CROWD_LABELS if level < limit)


@dataclass
class Network:
    city: str
    system: str
    timezone: str
    stations: dict[str, Station]
    lines: dict[str, Line]
    interchanges: dict[str, float]
    service: ServiceConfig
    fares: FareTable
    buses: dict[str, BusRoute] = field(default_factory=dict)
    bus_fares: FareTable | None = None
    walking: WalkConfig | None = None
    crowding: Crowding | None = None
    _lines_at: dict[str, list[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for line in self.lines.values():
            for sid in line.stations:
                if sid not in self.stations:
                    raise ValueError(f"line {line.id} references unknown station {sid}")
                self._lines_at.setdefault(sid, []).append(line.id)
        for route in self.buses.values():
            for sid in route.stops:
                if sid not in self.stations:
                    raise ValueError(f"bus route {route.id} references unknown stop {sid}")
        if self.crowding:
            self.crowding.calibrate(self.lines)

    @property
    def metro_stations(self) -> list[str]:
        return [sid for sid, st in self.stations.items() if st.kind == "metro"]

    @property
    def has_buses(self) -> bool:
        return bool(self.buses) and self.walking is not None and self.bus_fares is not None

    def is_metro(self, station_id: str) -> bool:
        return self.stations[station_id].kind == "metro"

    def lines_at(self, station_id: str) -> list[str]:
        return self._lines_at.get(station_id, [])

    def is_interchange(self, station_id: str) -> bool:
        return len(self.lines_at(station_id)) > 1
