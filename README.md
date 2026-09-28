# RouteMate AI

An AI-assisted route planner for the Hyderabad Metro. You type a trip in plain English (or Telugu or Hindi). Gemini works out what you mean, our own graph algorithms find the best route, and Gemini explains the result in simple words.

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
uv run pytest                    # 111 tests
```

Without a Gemini key, everything still works. The app uses the offline rule parser and template explanations, and shows "AI offline".

### CLI

```bash
uv run routemate route Miyapur "Hitech City" --at 08:40
uv run routemate route "LB Nagar" Nagole --mode fewest_transfers --arrive-by 10:00
uv run routemate route "LB Nagar" Nagole --avoid Ameerpet
uv run routemate options Raidurg Uppal            # Pareto trade-offs
uv run routemate compare Miyapur Nagole --at 09:00  # benchmark every algorithm
uv run routemate ask "cheapest way from LB Nagar to Miyapur"
```

---

## What is built (Part 1 — about 50%)

### 1. Data layer
- **Real network:** Hyderabad Metro, 3 lines and 57 stations (Red/Blue/Green), with coordinates, aliases ("MGBS", "Koti", "Hitech City") and the 3 interchanges.
- **Real fares:** the L&T Metro Rail revised fare chart (24 May 2025), ₹11–₹69 in distance slabs.
- **Storage:** stored in **SQLite** (`stations`, `lines`, `line_stations`, `interchanges`, `fare_slabs`, `search_log`). The seed JSON is hashed, so editing the data re-seeds the database automatically.
- **Calibrated track lengths:** straight-line distances between stations are scaled so each line matches its official length (Red 29.2 km, Blue 27 km, Green 11 km).

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

**Timetable:** trains leave the terminals by time-of-day headways (4 min at peak, 6–8 min off-peak, 06:00–23:00). Waiting times are therefore real, not averages. Because the schedule is fixed, leaving later never makes you arrive earlier. That ordering (called FIFO) is what makes time-dependent Dijkstra correct, and it lets "arrive by" use binary search.

**Planning modes:** `fastest`, `fewest_transfers`, `fewest_stops`, `shortest_distance`, `cheapest`. For `cheapest`: metro fares depend only on the origin and destination stations, so the planner says so honestly and shows the fastest route.

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
- **Results:** summary tiles, a step-by-step itinerary, the route drawn on the map in line colours, and a clickable list of Pareto trade-offs.
- **Algorithms tab:** every algorithm run on the same trip, showing nodes settled and time taken.

### 7. REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | status + AI state |
| GET | `/api/network` | lines, stations, coordinates, modes |
| GET | `/api/stations/suggest?q=` | autocomplete |
| GET | `/api/route?from&to&mode&departAt&arriveBy&avoid&trace` | plan one route |
| GET | `/api/routes/options?from&to&departAt` | Pareto trade-offs |
| GET | `/api/routes/alternatives?from&to&k` | Yen's k alternatives |
| GET | `/api/algorithms/compare?from&to&departAt` | algorithm benchmark, with exploration order |
| POST | `/api/ask` `{text, context?, language?}` | natural-language planning |
| GET | `/api/history` | recent searches (SQLite) |

### 8. Tests (111, about 2 s)
- **Algorithms:** checked against brute force on random graphs: Dijkstra vs Floyd–Warshall, BFS vs all-pairs hop counts, 0-1 BFS vs Dijkstra, Yen vs enumerating every simple path, Pareto vs a brute-force Pareto front.
- **Data structures:** heaps checked against sorting and a reference model.
- **Planner invariants:** step times add up to the total, fewest-changes never has more changes than fastest, arrive-by really is the latest departure, and more.
- **AI layer:** tested with a fake Gemini (validation, fallback, key rejection).
- **API:** end-to-end through the Flask test client.

---

## What is left (Part 2 — the other ~50%)

| Feature | Owner | Notes |
|---|---|---|
| **Animated algorithm visualiser** | Frontend | `/api/algorithms/compare` already returns each algorithm's `exploration` order. Animate Dijkstra, A* and BFS side by side on the map |
| **Crowding / peak-hour model** | Algorithms | Time-of-day crowd weights per station; add "least crowded" as a third Pareto criterion |
| **Bus + walking legs (multi-modal)** | Backend | Add TSRTC bus stops as nodes and walking edges within ~500 m; that makes "cheapest" a real trade-off |
| **Voice input + Telugu/Hindi UI** | Frontend/AI | Web Speech API → `/api/ask`; the backend already handles the language |
| **Search history + saved trips UI** | Frontend | `/api/history` already exists |
| **Deploy + presentation** | Everyone | gunicorn/Railway; slides; demo script |

## Suggested team split

| Member | Part 1 ownership (to explain in the viva) | Part 2 work |
|---|---|---|
| 1 — Algorithms | `ds/`, `algorithms/`, `test_algorithms.py` | Crowding criterion, visualiser data |
| 2 — Backend | `db.py`, `network/`, `planner/`, `web/api.py` | Bus/walk multi-modal graph |
| 3 — Frontend | `web/templates`, `web/static` | Visualiser animation, voice, history UI |
| 4 — AI + docs | `nlp/`, README, tests for the AI layer | Prompt tuning, Telugu/Hindi, slides |

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
  db.py  config.py  services.py  cli.py
tests/                        111 tests
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
- **Coordinates are approximate** (within about 200 m). Running speed (40 km/h average including acceleration, 20 s dwell) and headways are modelled, not the official timetable. All of these are editable in `data/hyderabad_metro.json`.
