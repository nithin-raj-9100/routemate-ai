from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any

from .network.model import FareTable, HeadwayBand, Line, Network, ServiceConfig, Station, parse_hhmm

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL
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
"""

STATIC_TABLES = ("station_aliases", "line_stations", "interchanges", "fare_slabs", "lines", "stations")


def default_seed_path() -> Path:
    return Path(str(resources.files("routemate") / "data" / "hyderabad_metro.json"))


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


def seed(conn: sqlite3.Connection, seed_path: Path | None = None, force: bool = False) -> bool:
    """Load the network JSON into SQLite. Re-seeds automatically when the JSON content changes."""
    seed_path = seed_path or default_seed_path()
    raw = seed_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    row = conn.execute("SELECT value FROM meta WHERE key = 'seed_digest'").fetchone()
    if row and row["value"] == digest and not force:
        return False

    data = json.loads(raw)
    with conn:
        for table in STATIC_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.executemany(
            "INSERT INTO stations (id, name, lat, lon) VALUES (?, ?, ?, ?)",
            [(s["id"], s["name"], s["lat"], s["lon"]) for s in data["stations"]],
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
        meta = {
            "city": data["city"],
            "system": data["system"],
            "timezone": data["timezone"],
            "service": json.dumps(data["service"]),
            "fare_currency": data["fare"]["currency"],
            "fare_source": data["fare"].get("source", ""),
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
        r["id"]: Station(r["id"], r["name"], r["lat"], r["lon"], tuple(aliases.get(r["id"], ())))
        for r in conn.execute("SELECT id, name, lat, lon FROM stations")
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
    )


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


def open_network(path: str | Path) -> tuple[sqlite3.Connection, Network]:
    conn = connect(path)
    seed(conn)
    return conn, load_network(conn)

