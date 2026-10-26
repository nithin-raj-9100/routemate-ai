"""Build src/routemate/data/hyderabad_bus.json from the official TGSRTC GTFS feed.

Usage:
    uv run python scripts/build_bus_data.py path/to/tgsrtc_gtfs.zip

Feed: "TGSRTC Buses GTFS", Open Data Telangana (public domain), mirrored at
https://data.opencity.in/dataset/hyderabad-bus-stops and https://mobilitydatabase.org/feeds/mdb-3361
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "src" / "routemate" / "data"

CITY_BOX = (17.30, 17.52, 78.32, 78.60)
MIN_TRIPS_PER_DAY = 30
MIN_SHARE_INSIDE = 0.9
METRO_LINK_KM = 0.4
MERGE_KM = 0.8
ROAD_DETOUR = 1.3
# The feed's intermediate stop times are interpolated (timepoint=0) and grow with every stop,
# implying ~2 km/h, so run times come from distance at a published average city-bus speed:
# 17 km Nagole-Ameerpet takes 75-90 min by RTC bus (Deccan Chronicle) -> ~12 km/h.
BUS_SPEED_KMPH = 12.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def minutes(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 60 + m + s / 60


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def read(zf: zipfile.ZipFile, name: str) -> list[dict[str, str]]:
    with zf.open(name) as fh:
        return list(csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig")))


def dominant_patterns(zf: zipfile.ZipFile) -> dict[tuple[str, str], tuple[tuple[str, ...], list[list[float]]]]:
    """Per (route, direction): the stop sequence most trips use, with every trip's stop times."""
    trips = {t["trip_id"]: t for t in read(zf, "trips.txt")}
    rows: dict[str, list[tuple[int, str, float]]] = defaultdict(list)
    for r in read(zf, "stop_times.txt"):
        rows[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"], minutes(r["departure_time"])))
    patterns: dict[tuple, list[list[float]]] = defaultdict(list)
    for tid, seq in rows.items():
        seq.sort()
        t = trips[tid]
        patterns[(t["route_id"], t["direction_id"], tuple(s for _, s, _ in seq))].append([m for _, _, m in seq])
    best: dict[tuple[str, str], tuple[tuple[str, ...], list[list[float]]]] = {}
    for (rid, direction, stops), times in patterns.items():
        if (rid, direction) not in best or len(times) > len(best[(rid, direction)][1]):
            best[(rid, direction)] = (stops, times)
    return best


def build(feed: Path) -> dict:
    metro = json.loads((DATA / "hyderabad_metro.json").read_text())
    metro_pts = [(s["lat"], s["lon"]) for s in metro["stations"]]
    metro_names = {slug(s["name"]) for s in metro["stations"]} | {slug(a) for s in metro["stations"] for a in s.get("aliases", [])}

    with zipfile.ZipFile(feed) as zf:
        stops = {s["stop_id"]: s for s in read(zf, "stops.txt")}
        feed_info = read(zf, "feed_info.txt")[0]
        best = dominant_patterns(zf)

    def pt(sid: str) -> tuple[float, float]:
        return float(stops[sid]["stop_lat"]), float(stops[sid]["stop_lon"])

    def inside(sid: str) -> bool:
        lat, lon = pt(sid)
        return CITY_BOX[0] <= lat <= CITY_BOX[1] and CITY_BOX[2] <= lon <= CITY_BOX[3]

    def near_metro(sid: str) -> bool:
        lat, lon = pt(sid)
        return any(haversine_km(lat, lon, a, b) <= METRO_LINK_KM for a, b in metro_pts)

    chosen = [
        (rid, direction, seq, times)
        for (rid, direction), (seq, times) in best.items()
        if len(times) >= MIN_TRIPS_PER_DAY
        and len(seq) >= 4
        and sum(map(inside, seq)) / len(seq) >= MIN_SHARE_INSIDE
        and any(map(near_metro, seq))
    ]

    # GTFS has a separate stop per kerb/direction; riders think in places, so same-name stops close together merge.
    places: list[dict] = []
    place_of: dict[str, str] = {}
    by_name: dict[str, list[dict]] = defaultdict(list)
    for sid in sorted({s for _, _, seq, _ in chosen for s in seq}):
        name = stops[sid]["stop_name"].strip()
        lat, lon = pt(sid)
        home = next((p for p in by_name[slug(name)] if haversine_km(lat, lon, p["lat"], p["lon"]) <= MERGE_KM), None)
        if home is None:
            n = len(by_name[slug(name)])
            suffix = f" ({n + 1})" if n else ""
            home = {"id": f"bus_{slug(name)}" + (f"_{n + 1}" if n else ""), "name": name + suffix, "lat": lat, "lon": lon, "gtfs": []}
            by_name[slug(name)].append(home)
            places.append(home)
        home["gtfs"].append(sid)
        place_of[sid] = home["id"]
    for p in places:
        pts = [pt(s) for s in p["gtfs"]]
        p["lat"] = round(sum(a for a, _ in pts) / len(pts), 6)
        p["lon"] = round(sum(b for _, b in pts) / len(pts), 6)
        if slug(p["name"]) in metro_names:
            # metro names must keep resolving to the metro station
            p["name"] = f"{p['name']} Bus Stop"

    routes = []
    for rid, direction, seq, times in sorted(chosen, key=lambda c: (c[0], c[1])):
        idx = [0] + [i for i in range(1, len(seq)) if place_of[seq[i]] != place_of[seq[i - 1]]]
        ids = [place_of[seq[i]] for i in idx]
        names = {p["id"]: p for p in places}
        km = [0.0]
        for a, b in zip(ids, ids[1:]):
            km.append(round(km[-1] + ROAD_DETOUR * haversine_km(names[a]["lat"], names[a]["lon"], names[b]["lat"], names[b]["lon"]), 3))
        offsets = [round(k / BUS_SPEED_KMPH * 60, 1) for k in km]
        routes.append(
            {
                "id": f"bus_{slug(rid)}_{direction}",
                "number": rid,
                "headsign": names[ids[-1]]["name"],
                "stops": ids,
                "offsets_min": offsets,
                "km": km,
                "departures_min": sorted(round(t[0] % (24 * 60), 1) for t in times),
            }
        )

    used = {s for r in routes for s in r["stops"]}
    return {
        "source": {
            "name": "TGSRTC Buses GTFS (Open Data Telangana, public domain)",
            "url": "https://data.opencity.in/dataset/hyderabad-bus-stops",
            "feed_version": feed_info.get("feed_version", ""),
            "selection": (
                f"Most-used stop pattern per route and direction with >= {MIN_TRIPS_PER_DAY} trips/day, "
                f">= {MIN_SHARE_INSIDE:.0%} of stops inside the metro area and at least one stop within "
                f"{METRO_LINK_KM * 1000:.0f} m of a metro station. Stop order and first-stop departure times are from the feed."
            ),
            "modelled": (
                f"Bus distance = straight line between stops x {ROAD_DETOUR} (road detour). Run time = distance at "
                f"{BUS_SPEED_KMPH:g} km/h, because the feed's intermediate stop times are interpolated and unrealistic."
            ),
        },
        "stops": [{k: p[k] for k in ("id", "name", "lat", "lon")} for p in places if p["id"] in used],
        "routes": routes,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("feed", type=Path, help="TGSRTC GTFS zip")
    ap.add_argument("--out", type=Path, default=DATA / "hyderabad_bus.json")
    args = ap.parse_args()
    data = build(args.feed)
    args.out.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    print(f"{args.out}: {len(data['routes'])} route patterns, {len(data['stops'])} stops, "
          f"{sum(len(r['departures_min']) for r in data['routes'])} trips/day")


if __name__ == "__main__":
    main()
