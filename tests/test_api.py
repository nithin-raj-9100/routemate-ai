def test_health(client):
    body = client.get("/api/health").get_json()
    assert body["status"] == "ok"
    assert body["network"]["stations"] == 57
    assert body["ai"]["enabled"] is False


def test_network(client):
    body = client.get("/api/network").get_json()
    assert len(body["lines"]) == 3
    assert {m["id"] for m in body["modes"]} >= {"fastest", "fewest_transfers", "cheapest"}
    ameerpet = next(s for s in body["stations"] if s["id"] == "ameerpet")
    assert ameerpet["interchange"] and set(ameerpet["lines"]) == {"red", "blue"}


def test_suggest(client):
    body = client.get("/api/stations/suggest?q=hit").get_json()
    assert body["results"][0]["id"] == "hitech_city"


def test_route_and_history(client):
    r = client.get("/api/route?from=Miyapur&to=Hitec%20City&departAt=09:00")
    assert r.status_code == 200
    body = r.get_json()
    assert body["summary"]["transfers"] == 1
    assert body["search"]["settled"] > 0
    hist = client.get("/api/history").get_json()["searches"]
    assert hist[0]["origin"] == "miyapur" and hist[0]["found"] is True


def test_route_with_trace(client):
    body = client.get("/api/route?from=ameerpet&to=uppal&departAt=09:00&trace=1").get_json()
    assert body["search"]["trace"]


def test_route_errors(client):
    assert client.get("/api/route?from=Ameerpet").status_code == 400
    r = client.get("/api/route?from=Ameerpet&to=Atlantis")
    assert r.status_code == 400 and r.get_json()["error"]["code"] == "UNKNOWN_STATION"
    r = client.get("/api/route?from=Ameerpet&to=Uppal&departAt=25:99")
    assert r.get_json()["error"]["code"] == "BAD_TIME"
    r = client.get("/api/route?from=Ameerpet&to=Uppal&departAt=23:58")
    assert r.status_code == 404 and r.get_json()["error"]["code"] == "NO_SERVICE"


def test_options_alternatives_compare(client):
    q = "from=Raidurg&to=Nagole&departAt=09:00"
    assert client.get(f"/api/routes/options?{q}").get_json()["options"]
    assert len(client.get(f"/api/routes/alternatives?{q}&k=2").get_json()["alternatives"]) >= 1
    cmp = client.get(f"/api/algorithms/compare?{q}").get_json()
    keys = {a["key"] for a in cmp["algorithms"]}
    assert {"dijkstra_td", "astar_td", "bfs", "zero_one_bfs", "pareto", "yen"} <= keys


def test_ask(client):
    r = client.post("/api/ask", json={"text": "cheapest way from LB Nagar to Miyapur"})
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["query"]["mode"] == "cheapest"
    assert body["explanation"]["source"] == "template"


def test_ask_validation(client):
    assert client.post("/api/ask", json={}).status_code == 400
    assert client.post("/api/ask", json={"text": "x" * 600}).status_code == 400


def test_index_page(client):
    r = client.get("/")
    assert r.status_code == 200 and b"RouteMate" in r.data


def test_ask_stream(client):
    r = client.post("/api/ask/stream", json={"text": "Miyapur to Hitech City"})
    assert r.mimetype == "text/event-stream"
    body = r.get_data(as_text=True)
    events = [block.split("\n")[0].removeprefix("event: ") for block in body.strip().split("\n\n")]
    assert events[0] == "stage" and events[-1] == "done"
    assert "query" in events and "plan" in events and "explanation" in events


def test_ask_stream_reports_planning_errors(client):
    body = client.post("/api/ask/stream", json={"text": "Ameerpet to Uppal at 23:58"}).get_data(as_text=True)
    assert "event: error" in body and "NO_SERVICE" in body


def test_network_lists_bus_routes_and_sources(client):
    body = client.get("/api/network").get_json()
    assert len(body["busRoutes"]) > 100
    kinds = {s["kind"] for s in body["stations"]}
    assert kinds == {"metro", "bus"}
    assert body["crowding"]["source"] and body["busFare"]["source"]


def test_route_with_buses_flag(client):
    r = client.get("/api/route?from=Ameerpet&to=Mehdipatnam&departAt=09:00")
    body = r.get_json()
    assert r.status_code == 200 and body["buses"] is True
    assert "bus" in body["summary"]["modes"]
    assert any(g["type"] == "bus" for g in body["geometry"])
    assert sum(t["fare"] for t in body["summary"]["tickets"]) == body["summary"]["fare"]
    plain = client.get("/api/route?from=Ameerpet&to=Uppal&departAt=09:00").get_json()
    assert plain["buses"] is False and plain["summary"]["crowd"]["label"]


def test_least_crowded_route(client):
    body = client.get("/api/route?from=Miyapur&to=Uppal&departAt=09:00&mode=least_crowded").get_json()
    assert body["mode"] == "least_crowded"
    assert all(s["crowd"] for s in body["steps"] if s["type"] == "ride")


def test_saved_trips_crud(client):
    r = client.post("/api/trips", json={"origin": "Ameerpet", "destination": "Hitech City", "mode": "fewest_transfers", "departAt": "9:05"})
    assert r.status_code == 201
    trip = r.get_json()["trip"]
    assert trip["origin"] == "ameerpet" and trip["departAt"] == "09:05" and trip["name"] == "Ameerpet → Hitech City"
    assert [t["id"] for t in client.get("/api/trips").get_json()["trips"]] == [trip["id"]]
    assert client.delete(f"/api/trips/{trip['id']}").status_code == 200
    assert client.delete(f"/api/trips/{trip['id']}").status_code == 404
    assert client.get("/api/trips").get_json()["trips"] == []


def test_saved_trip_validation(client):
    assert client.post("/api/trips", json={"origin": "Atlantis", "destination": "Uppal"}).status_code == 400
    assert client.post("/api/trips", json={"origin": "Ameerpet", "destination": "Uppal", "arriveBy": "nope"}).status_code == 400
