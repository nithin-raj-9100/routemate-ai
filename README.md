# RouteMate AI

An AI-assisted route planner for the Hyderabad Metro, with TGSRTC city buses and short walks for places the metro doesn't reach. You type a trip in plain English (or Telugu or Hindi). Gemini works out what you mean, our own graph algorithms find the best route, and Gemini explains the result in simple words.

> The AI never picks the route. It only reads the question and explains the answer. Every route comes from the algorithms below, so it is always a real, optimal route.

```
"I'm at Inorbit mall, need to reach Koti before 10, don't want to change trains much"
        │
        ▼  Gemini (or the offline rule parser)
{origin: Durgam Cheruvu, destination: Sultan Bazaar, mode: fewest_transfers, arriveBy: 10:00}
        │
        ▼  time-dependent Dijkstra + binary search on departure time
Blue Line → change at Parade Ground → Green Line · leave 09:05 · arrive 09:58 · ₹56
        │
        ▼  Gemini, given only the verified facts
"Board the Blue Line towards Nagole, change at Parade Ground to the Green Line…"
```

---

## Quick start

Requires [uv](https://docs.astral.sh/uv/). No `pip`, and no manual virtualenv.

```bash
uv sync                          # install dependencies into .venv
cp .env.example .env             # then paste your GEMINI_API_KEY (optional)
uv run routemate init-db         # create + seed var/routemate.db (SQLite)
uv run routemate serve           # http://127.0.0.1:5050
uv run pytest                    # 143 tests
```

Without a Gemini key, everything still works. The app uses the offline rule parser and template explanations, and shows "AI offline".

---

## What is built

### 1. Data layer
- **Real network:** Hyderabad Metro, 3 lines and 57 stations (Red/Blue/Green), with coordinates, aliases ("MGBS", "Koti", "Hitech City") and the 3 interchanges.
- **Real fares:** the L&T Metro Rail revised fare chart (24 May 2025), ₹11–₹69 in distance slabs.
- **Storage:** stored in **SQLite** (`stations`, `lines`, `line_stations`, `interchanges`, `fare_slabs`, `search_log`). The seed JSON is hashed, so editing the data re-seeds the database automatically.
- **Calibrated track lengths:** straight-line distances between stations are scaled so each line matches its official length (Red 29.2 km, Blue 27 km, Green 11 km).
- **TGSRTC buses (real data):** 127 route patterns, 667 stops and 10,601 daily departures imported from the official TGSRTC GTFS feed by `scripts/build_bus_data.py` and stored in SQLite (`bus_routes`, `bus_route_stops`, `bus_departures`). See "Data sources" for what is modelled.
- **Crowding (modelled):** built from published HMRL station entry counts, corridor ridership and peak windows. It is a model, not live data, and the app labels it that way.
- **Saved trips:** a `saved_trips` table, alongside `search_log`.

### 2. Data structures, written from scratch (`src/routemate/ds/`)

| Structure | Used for |
|---|---|
| `BinaryHeap` | Pareto search, Yen's candidate list, trie ranking |
| `IndexedMinHeap` (with **decrease-key**) | Dijkstra / A* priority queue |
| `Trie` with best-in-subtree weights | Ranked station autocomplete (a top-k prefix query walks best-first, instead of collecting every match) |
| `DiGraph` / `OverlayGraph` | Adjacency-list graph, plus per-query virtual source/sink without copying the graph |
| Damerau–Levenshtein distance | Typo-tolerant matching ("Ameerpeth", "dilsuknagar") |

### 3. Graph model: a line-expanded transit graph
Each station becomes several nodes: one per (line, direction) for being on a train, and one per line for waiting on the platform. Boarding, riding, and changing lines are separate edge types. The number of line changes is therefore counted exactly, and a route can never quietly "teleport" between lines.

```
wait(s, L) ─board─▶ ride(s, L, dir) ─ride─▶ ride(s+1, L, dir) …
ride(s, L1, dir) ─transfer(walk)─▶ wait(s, L2)     (interchanges only)
```

### 4. Algorithms (`src/routemate/algorithms/`)

| Algorithm | What it answers |
|---|---|
| **Dijkstra (label-setting, generic labels)** | Fastest route. The same code handles numbers, lexicographic tuples such as `(line changes, time)`, and **time-dependent** costs |
| **A\*** with a haversine heuristic (admissible: straight-line km ÷ top speed), tie-broken toward deeper nodes | Same answer as Dijkstra with less work (tests check this on the real network) |
| **BFS** | Fewest stations |
| **0-1 BFS** (deque) | Fewest line changes in O(V+E), with no heap |
| **Yen's k-shortest loopless paths** | Genuinely different alternative routes |
| **Multi-criteria Pareto label-setting** | Every sensible trade-off between arrival time and line changes |
| **Binary search on departure time** | "Arrive by 10:00": the latest you can leave |
| **Dijkstra over (node, open ticket) states** | Cheapest trip with metro + bus. A metro fare depends on entry *and* exit and a bus fare on distance ridden, so the open ticket rides along in the search state and is charged when you get off. This keeps fares additive, so the result is exact |

**Timetable:** trains leave the terminals by time-of-day headways (4 min at peak, 6–8 min off-peak, 06:00–23:00). Waiting times are therefore real, not averages. Because the schedule is fixed, leaving later never makes you arrive earlier. That ordering (called FIFO) is what makes time-dependent Dijkstra correct, and it lets "arrive by" use binary search.

**Planning modes:** `fastest`, `fewest_transfers`, `fewest_stops`, `shortest_distance`, `cheapest`, `least_crowded`.
- `cheapest` compares the metro with buses and short walks, and the note shows how much it saves over the metro fare.
- `least_crowded` minimises crowd exposure (crowd level × minutes on board, plus interchange platforms), then arrival time. It is metro-only, because there is no crowding data for buses.

**Multimodal graph:** every place gets a street node. Metro stations are entered (`access`) and left (`egress`) through it, buses are boarded at a stop and left at a later one, and walking edges join places within 500 m. Three node types stop shortcuts that don't exist in reality:
- An "out" node after leaving a station cannot re-enter that station, so a route can't exit and re-enter to dodge an interchange walk.
- A "walked" node can't walk again, so walking is one hop between rides, never a free chain across the city.
- A "departing" node must ride at least one stop, so a route can't board and step straight back off.

Buses are used when you tick "Include buses", choose `cheapest`, or start or end at a place the metro doesn't serve (e.g. Mehdipatnam, Kondapur, Charminar).

**Trade-offs:** the Pareto search has three criteria: arrival × line changes × crowding for metro trips, and arrival × changes × fare when buses are involved. The bus version only keeps trips within 1.5× the fastest time, so its front stays small.

**Also handled:** avoiding stations (no boarding or changing there, but trains may pass through), times before the first train, and a clear `NO_SERVICE` error after the last one.

### 5. AI layer (`src/routemate/nlp/`)
- **Gemini parser:** turns free text into strict JSON, using `gemini-3.1-flash-lite` by default (thinking off). It handles landmarks (Inorbit Mall → Durgam Cheruvu), Telugu and Hindi, and follow-up questions. **Every station Gemini returns is re-checked** against the real network; anything unknown is dropped.
- **Offline rule parser:** always runs. It spots stations with a sliding window, assigns roles from cue words (from/at → start, to/reach → destination, avoid), detects mode with regexes, and resolves times ("before 10", "by 9:30 am", "in 20 minutes"). Where Gemini leaves a field empty, this parser fills it.
- **Explainer:** Gemini receives only a compact JSON of verified facts and is told not to invent anything. If Gemini is unavailable, a template explainer takes over.
- **Circuit breaker:** a bad key switches the AI off for the whole run, and repeated timeouts pause it for 60 s. The app never hangs waiting for the AI.
- **Conversation:** follow-ups like "what if I want the fastest?" reuse the previous trip. If a station is missing, it asks for it.

### 6. Web app (Flask + Leaflet)
- **Ask tab:** a chat-style request box with example prompts and follow-up context.
- **Plan tab:** From/To with autocomplete, mode chips, and leave now / depart at / arrive by. You can also click stations on the map.
- **Results:** summary tiles (with a crowding tile and a per-ticket fare breakdown), a step-by-step itinerary with crowd badges, and the route on the map: metro solid, bus dashed, walking dotted. There is also a clickable list of Pareto trade-offs and a ☆ Save button.
- **Buses on the map:** zoom in to see bus stops. Bus stops also appear in autocomplete.
- **Algorithms tab:** every algorithm run on the same trip, showing nodes settled and time taken. **▶ Watch** opens the visualiser: Dijkstra, A* and BFS animate side by side on three synced maps, one settled station per step. You can play, pause, change speed and replay, and each search draws its route when it finishes.
- **History tab:** saved trips (plan again or delete) and recent searches (click to run again; Ask searches replay the original sentence).

### 7. REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | status + AI state |
| GET | `/api/network` | lines, stations, coordinates, modes |
| GET | `/api/stations/suggest?q=` | autocomplete |
| GET | `/api/route?from&to&mode&departAt&arriveBy&avoid&buses&trace` | plan one route |
| GET | `/api/routes/options?from&to&departAt&avoid&buses` | Pareto trade-offs |
| GET | `/api/routes/alternatives?from&to&k` | Yen's k alternatives |
| GET | `/api/algorithms/compare?from&to&departAt` | algorithm benchmark, with exploration order |
| POST | `/api/ask` `{text, context?, language?}` | natural-language planning |
| GET | `/api/history` | recent searches (SQLite) |
| GET / POST | `/api/trips` | list / save a trip `{origin, destination, mode, departAt?, arriveBy?, avoid?, buses?, name?}` |
| DELETE | `/api/trips/<id>` | delete a saved trip |

### 8. Tests (143, about 10 s)
- **Algorithms:** checked against brute force on random graphs: Dijkstra vs Floyd–Warshall, BFS vs all-pairs hop counts, 0-1 BFS vs Dijkstra, Yen vs enumerating every simple path, Pareto vs a brute-force Pareto front.
- **Data structures:** heaps checked against sorting and a reference model.
- **Planner invariants:** step times add up to the total, fewest-changes never has more changes than fastest, arrive-by really is the latest departure, and more.
- **Multimodal and crowding:** bus data integrity, TGSRTC fare stages, no zero-stop legs, single-hop walks, cheapest never dearer than the metro, avoid with buses, arrive-by with buses, least-crowded never more crowded than fastest, and off-peak quieter than peak.
- **AI layer:** tested with a fake Gemini (validation, fallback, key rejection).
- **API:** end-to-end through the Flask test client.

---

## Part 2 status

| Feature | Status |
|---|---|
| Animated algorithm visualiser | Done: Algorithms tab → ▶ Watch |
| Crowding / peak-hour model | Done: `least_crowded` mode, crowd badges, third Pareto criterion |
| Bus + walking legs (multimodal) | Done: real TGSRTC GTFS routes and timetables, `cheapest` is now a real trade-off |
| Search history + saved trips UI | Done: History tab |
| Voice input + Telugu/Hindi UI | Not planned for now |
| Deploy + presentation | The app is already deployed |

## Project layout

```
src/routemate/
  data/hyderabad_metro.json   network, fares, timetable bands
  ds/                         heap, trie, graph, fuzzy matching
  algorithms/                 dijkstra/A*, bfs, 0-1 bfs, yen, pareto
  network/                    model + line-expanded transit graph
  planner/                    timetable, itinerary, route planner
  nlp/                        station index, rule parser, Gemini parser/explainer, assistant
  web/                        Flask app, API, UI
  data/hyderabad_bus.json     generated from the TGSRTC GTFS feed
  planner/fares.py            fare rules + (node, open ticket) search graph
  db.py  config.py  services.py  cli.py (init-db, serve)
scripts/build_bus_data.py     rebuilds hyderabad_bus.json from a TGSRTC GTFS zip
tests/                        143 tests
```

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `GEMINI_API_KEY` | — | Enables the AI layer |
| `ROUTEMATE_GEMINI_MODEL` | `gemini-3.1-flash-lite` | ~3 s to first word in testing; `gemini-3.5-flash-lite` took 20–50 s and `gemini-3.8-flash` about 2 min |
| `ROUTEMATE_AI` | `on` | `off` forces offline mode |
| `ROUTEMATE_DB` | `var/routemate.db` | SQLite path |
| `PORT` | `5050` | |

`.env` overrides variables already set in your shell, so an old shell key can't silently win.

## Data sources and assumptions
- Station order, interchanges and line lengths: Hyderabad Metro Rail (Corridors I–III).
- Fares: [L&T Metro Rail revised fare chart, 24 May 2025](https://ltmetro.com/wp-content/uploads/2025/05/Hyderabad-Metro-Rail-Revised-Fare-Chart-Final.pdf).
- **Buses:** [TGSRTC Buses GTFS](https://data.opencity.in/dataset/hyderabad-bus-stops) (Open Data Telangana, public domain; also at [Mobility Database mdb-3361](https://mobilitydatabase.org/feeds/mdb-3361)). Stop order, stop locations and first-stop departure times are real. For each route and direction, the most-used stop pattern is kept if it has 30+ trips a day, has 90%+ of its stops inside the metro area and passes within 400 m of a metro station. Same-name stops within 800 m are merged into one place. To rebuild: `uv run python scripts/build_bus_data.py feed.zip`.
- **Bus run times are modelled.** The feed's stop times between terminals are interpolated (`timepoint=0`) and grow with every stop, implying about 2 km/h. Instead, run time = road distance (straight line × 1.3) at 12 km/h, from the RTC bus figure of 75–90 min for the 17 km Nagole–Ameerpet trip reported by Deccan Chronicle.
- **Bus fares:** TGSRTC City Ordinary 2 km stages (circular 21/2019-OPD(P)) plus the 6 Oct 2025 surcharge (+₹5 on stages 1–3, +₹10 from stage 4), which gives a minimum fare of ₹15, matching press reports. Check against the current TGSRTC chart before relying on it.
- **Crowding is modelled.** Station entries: HMRL figures reported by Deccan Chronicle for 3 Jul 2023 (Raidurg 32k, LB Nagar 30k, Ameerpet 29k, Miyapur 23k; corridor I 2.6 lakh, II 25k, III 2.25 lakh) and 7 Jun 2019 (HITEC City 18k, KPHB 13k; scaled by the median 2019→2023 growth). Stations without a count get their corridor average. Peak windows: HMRL 08:00–11:00 and 17:00–20:00, plus press-reported rush hours. Crowd level = time factor × (60% corridor intensity + 40% station busyness). The time factor is taken at the trip's departure time, which keeps the search exact.
- **Coordinates are approximate** (within about 200 m). Running speed (40 km/h average including acceleration, 20 s dwell) and headways are modelled, not the official timetable. All of these are editable in `data/hyderabad_metro.json`.
