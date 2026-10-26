from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from .network.model import (
    BusRoute,
    Crowding,
    FareTable,
    HeadwayBand,
    Line,
    Network,
    ServiceConfig,
    Station,
    WalkConfig,
    parse_hhmm,
)

STATIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'metro'
);
CREATE TABLE IF NOT EXISTS station_aliases (
    station_id TEXT NOT NULL REFERENCES stations(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    PRIMARY KEY (station_id, alias)
);
CREATE TABLE IF NOT EXISTS lines (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    corridor TEXT NOT NULL,
    color TEXT NOT NULL,
    length_km REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS line_stations (
    line_id TEXT NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL REFERENCES stations(id),
    PRIMARY KEY (line_id, seq)
);
CREATE TABLE IF NOT EXISTS interchanges (
    station_id TEXT PRIMARY KEY REFERENCES stations(id),
    walk_min REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS fare_slabs (
    seq INTEGER PRIMARY KEY,
    upto_km REAL,
    fare REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bus_routes (
    id TEXT PRIMARY KEY,
    number TEXT NOT NULL,
    headsign TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bus_route_stops (
    route_id TEXT NOT NULL REFERENCES bus_routes(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    station_id TEXT NOT NULL REFERENCES stations(id),
    offset_min REAL NOT NULL,
    km REAL NOT NULL,
    PRIMARY KEY (route_id, seq)
);
CREATE TABLE IF NOT EXISTS bus_departures (
    route_id TEXT NOT NULL REFERENCES bus_routes(id) ON DELETE CASCADE,
    minute REAL NOT NULL
);
"""

USER_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    query TEXT,
    origin TEXT,
    destination TEXT,
    mode TEXT,
    parser TEXT,
    found INTEGER NOT NULL,
    total_min REAL,
    elapsed_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_search_log_created ON search_log(created_at);
CREATE TABLE IF NOT EXISTS saved_trips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    name TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    mode TEXT NOT NULL,
    depart_at TEXT,
    arrive_by TEXT,
    avoid TEXT NOT NULL DEFAULT '[]',
    buses INTEGER NOT NULL DEFAULT 0
);
"""

SCHEMA = STATIC_SCHEMA + USER_SCHEMA

STATIC_TABLES = (
    "bus_departures",
    "bus_route_stops",
    "bus_routes",
    "station_aliases",
    "line_stations",
    "interchanges",
    "fare_slabs",
    "lines",
    "stations",
)


def default_seed_path() -> Path:
    return Path(str(resources.files("routemate") / "data" / "hyderabad_metro.json"))


def default_bus_path() -> Path:
    return Path(str(resources.files("routemate") / "data" / "hyderabad_bus.json"))


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn


def seed(conn: sqlite3.Connection, seed_path: Path | None = None, force: bool = False, bus_path: Path | None = None) -> bool:
    """Load the network JSON files into SQLite. Re-seeds automatically when the JSON or the table layout changes."""
    seed_path = seed_path or default_seed_path()
    bus_path = bus_path or default_bus_path()
    raw = seed_path.read_bytes()
    bus_raw = bus_path.read_bytes() if bus_path.exists() else b""
    digest = hashlib.sha256(raw + bus_raw + STATIC_SCHEMA.encode()).hexdigest()
    row = conn.execute("SELECT value FROM meta WHERE key = 'seed_digest'").fetchone()
    if row and row["value"] == digest and not force:
        return False

    data = json.loads(raw)
    bus = json.loads(bus_raw) if bus_raw else {"stops": [], "routes": []}
    with conn:
        # Static tables are rebuilt from scratch so a database created by an older layout picks up new columns.
        for table in STATIC_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(STATIC_SCHEMA)
        conn.executemany(
            "INSERT INTO stations (id, name, lat, lon, kind) VALUES (?, ?, ?, ?, ?)",
            [(s["id"], s["name"], s["lat"], s["lon"], "metro") for s in data["stations"]]
            + [(s["id"], s["name"], s["lat"], s["lon"], "bus") for s in bus["stops"]],
        )
        conn.executemany(
            "INSERT INTO station_aliases (station_id, alias) VALUES (?, ?)",
            [(s["id"], a) for s in data["stations"] for a in s.get("aliases", [])],
        )
        conn.executemany(
            "INSERT INTO lines (id, name, corridor, color, length_km) VALUES (?, ?, ?, ?, ?)",
            [(l["id"], l["name"], l["corridor"], l["color"], l["length_km"]) for l in data["lines"]],
        )
        conn.executemany(
            "INSERT INTO line_stations (line_id, seq, station_id) VALUES (?, ?, ?)",
            [(l["id"], i, sid) for l in data["lines"] for i, sid in enumerate(l["stations"])],
        )
        conn.executemany(
            "INSERT INTO interchanges (station_id, walk_min) VALUES (?, ?)",
            [(x["station"], x["walk_min"]) for x in data["interchanges"]],
        )
        conn.executemany(
            "INSERT INTO fare_slabs (seq, upto_km, fare) VALUES (?, ?, ?)",
            [(i, s["upto_km"], s["fare"]) for i, s in enumerate(data["fare"]["slabs"])],
        )
        conn.executemany(
            "INSERT INTO bus_routes (id, number, headsign) VALUES (?, ?, ?)",
            [(r["id"], r["number"], r["headsign"]) for r in bus["routes"]],
        )
        conn.executemany(
            "INSERT INTO bus_route_stops (route_id, seq, station_id, offset_min, km) VALUES (?, ?, ?, ?, ?)",
            [
                (r["id"], i, sid, off, km)
                for r in bus["routes"]
                for i, (sid, off, km) in enumerate(zip(r["stops"], r["offsets_min"], r["km"]))
            ],
        )
        conn.executemany(
            "INSERT INTO bus_departures (route_id, minute) VALUES (?, ?)",
            [(r["id"], m) for r in bus["routes"] for m in r["departures_min"]],
        )
        meta = {
            "city": data["city"],
            "system": data["system"],
            "timezone": data["timezone"],
            "service": json.dumps(data["service"]),
            "fare_currency": data["fare"]["currency"],
            "fare_source": data["fare"].get("source", ""),
            "crowding": json.dumps(data.get("crowding")),
            "bus_fare": json.dumps(data.get("bus_fare")),
            "walking": json.dumps(data.get("walking")),
            "bus_source": json.dumps(bus.get("source")),
            "seed_digest": digest,
        }
        conn.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", list(meta.items()))
    return True


def load_network(conn: sqlite3.Connection) -> Network:
    meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
    if "seed_digest" not in meta:
        raise RuntimeError("database is empty; run `routemate init-db` first")

    aliases: dict[str, list[str]] = {}
    for r in conn.execute("SELECT station_id, alias FROM station_aliases ORDER BY alias"):
        aliases.setdefault(r["station_id"], []).append(r["alias"])
    stations = {
        r["id"]: Station(r["id"], r["name"], r["lat"], r["lon"], tuple(aliases.get(r["id"], ())), r["kind"])
        for r in conn.execute("SELECT id, name, lat, lon, kind FROM stations ORDER BY rowid")
    }

    members: dict[str, list[str]] = {}
    for r in conn.execute("SELECT line_id, station_id FROM line_stations ORDER BY line_id, seq"):
        members.setdefault(r["line_id"], []).append(r["station_id"])
    lines = {
        r["id"]: Line(r["id"], r["name"], r["corridor"], r["color"], r["length_km"], tuple(members.get(r["id"], ())))
        for r in conn.execute("SELECT id, name, corridor, color, length_km FROM lines ORDER BY rowid")
    }

    interchanges = {r["station_id"]: r["walk_min"] for r in conn.execute("SELECT station_id, walk_min FROM interchanges")}
    slabs = [(r["upto_km"], r["fare"]) for r in conn.execute("SELECT upto_km, fare FROM fare_slabs ORDER BY seq")]

    svc = json.loads(meta["service"])
    service = ServiceConfig(
        first_departure=parse_hhmm(svc["first_departure"]),
        last_departure=parse_hhmm(svc["last_departure"]),
        avg_run_speed_kmph=svc["avg_run_speed_kmph"],
        max_speed_kmph=svc["max_speed_kmph"],
        dwell_min=svc["dwell_min"],
        access_min=svc["access_min"],
        headways=[HeadwayBand(parse_hhmm(h["from"]), parse_hhmm(h["to"]), h["every_min"]) for h in svc["headways"]],
    )
    return Network(
        city=meta["city"],
        system=meta["system"],
        timezone=meta["timezone"],
        stations=stations,
        lines=lines,
        interchanges=interchanges,
        service=service,
        fares=FareTable(slabs, meta.get("fare_currency", "INR"), meta.get("fare_source", "")),
        buses=_load_buses(conn),
        bus_fares=_fare_table(json.loads(meta.get("bus_fare") or "null")),
        walking=WalkConfig(**json.loads(meta["walking"])) if json.loads(meta.get("walking") or "null") else None,
        crowding=_crowding(json.loads(meta.get("crowding") or "null")),
    )


def _load_buses(conn: sqlite3.Connection) -> dict[str, BusRoute]:
    stops: dict[str, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT route_id, station_id, offset_min, km FROM bus_route_stops ORDER BY route_id, seq"):
        stops.setdefault(r["route_id"], []).append(r)
    departures: dict[str, list[float]] = {}
    for r in conn.execute("SELECT route_id, minute FROM bus_departures ORDER BY route_id, minute"):
        departures.setdefault(r["route_id"], []).append(r["minute"])
    return {
        r["id"]: BusRoute(
            r["id"],
            r["number"],
            r["headsign"],
            tuple(x["station_id"] for x in stops.get(r["id"], [])),
            tuple(x["offset_min"] for x in stops.get(r["id"], [])),
            tuple(x["km"] for x in stops.get(r["id"], [])),
            tuple(departures.get(r["id"], [])),
        )
        for r in conn.execute("SELECT id, number, headsign FROM bus_routes ORDER BY id")
    }


def _fare_table(data: dict | None) -> FareTable | None:
    if not data:
        return None
    return FareTable([(s["upto_km"], s["fare"]) for s in data["slabs"]], data.get("currency", "INR"), data.get("source", ""))


def _crowding(data: dict | None) -> Crowding | None:
    """2023 counts win; 2019-only stations are scaled by the median growth of stations counted in both years."""
    if not data:
        return None
    new, old = data.get("station_daily_entries_2023", {}), data.get("station_daily_entries_2019", {})
    growth = sorted(new[s] / old[s] for s in new if s in old)
    scale = growth[len(growth) // 2] if growth else 1.0
    entries = {**{s: v * scale for s, v in old.items()}, **new}
    bands = [(parse_hhmm(b["from"]), parse_hhmm(b["to"]), b["factor"]) for b in data["time_factors"]]
    return Crowding(data["line_daily_riders"], entries, bands, data.get("source", ""), data.get("modelled", ""))


def log_search(conn: sqlite3.Connection, **fields: Any) -> None:
    fields.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    fields["found"] = int(bool(fields.get("found")))
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with conn:
        conn.execute(f"INSERT INTO search_log ({cols}) VALUES ({marks})", list(fields.values()))


def recent_searches(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT created_at, channel, query, origin, destination, mode, parser, found, total_min, elapsed_ms "
        "FROM search_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        {
            "createdAt": r["created_at"],
            "channel": r["channel"],
            "query": r["query"],
            "origin": r["origin"],
            "destination": r["destination"],
            "mode": r["mode"],
            "parser": r["parser"],
            "found": bool(r["found"]),
            "totalMin": r["total_min"],
            "elapsedMs": r["elapsed_ms"],
        }
        for r in rows
    ]


def save_trip(conn: sqlite3.Connection, **fields: Any) -> int:
    fields.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    fields["avoid"] = json.dumps(fields.get("avoid") or [])
    fields["buses"] = int(bool(fields.get("buses")))
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    with conn:
        return conn.execute(f"INSERT INTO saved_trips ({cols}) VALUES ({marks})", list(fields.values())).lastrowid


def saved_trips(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, created_at, name, origin, destination, mode, depart_at, arrive_by, avoid, buses FROM saved_trips ORDER BY id DESC"
    ).fetchall()
    return [
        {
            "id": r["id"],
            "createdAt": r["created_at"],
            "name": r["name"],
            "origin": r["origin"],
            "destination": r["destination"],
            "mode": r["mode"],
            "departAt": r["depart_at"],
            "arriveBy": r["arrive_by"],
            "avoid": json.loads(r["avoid"]),
            "buses": bool(r["buses"]),
        }
        for r in rows
    ]


def delete_trip(conn: sqlite3.Connection, trip_id: int) -> bool:
    with conn:
        return conn.execute("DELETE FROM saved_trips WHERE id = ?", (trip_id,)).rowcount > 0


def open_network(path: str | Path) -> tuple[sqlite3.Connection, Network]:
    conn = connect(path)
    seed(conn)
    return conn, load_network(conn)

