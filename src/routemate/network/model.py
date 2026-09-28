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
    _lines_at: dict[str, list[str]] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        for line in self.lines.values():
            for sid in line.stations:
                if sid not in self.stations:
                    raise ValueError(f"line {line.id} references unknown station {sid}")
                self._lines_at.setdefault(sid, []).append(line.id)

    def lines_at(self, station_id: str) -> list[str]:
        return self._lines_at.get(station_id, [])

    def is_interchange(self, station_id: str) -> bool:
        return len(self.lines_at(station_id)) > 1
