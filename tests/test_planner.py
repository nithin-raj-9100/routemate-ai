import pytest

from routemate.network.model import parse_hhmm
from routemate.planner.planner import PlanningError


def test_network_shape(network, planner):
    assert len(network.stations) == 57
    assert set(network.lines) == {"red", "blue", "green"}
    assert {s for s in network.stations if network.is_interchange(s)} == {"ameerpet", "mg_bus_station", "parade_ground"}
    total = planner.tg.offsets[("red", 1)][-1]
    assert 45 < total < 60


def test_direct_trip_has_no_change(planner):
    plan = planner.plan("Ameerpet", "Hitech City", depart_at=parse_hhmm("09:00"))
    it = plan.itinerary
    assert it.transfers == 0
    assert [l.line for l in it.legs] == ["blue"]
    assert it.legs[0].towards == "raidurg"
    assert it.stops == 8


def test_cross_line_trip_changes_at_ameerpet(planner):
    plan = planner.plan("Miyapur", "Hitech City", depart_at=parse_hhmm("09:00"))
    it = plan.itinerary
    assert [l.line for l in it.legs] == ["red", "blue"]
    assert any(s["type"] == "transfer" and s["station"] == "ameerpet" for s in it.steps)
    assert it.arrive_at > it.depart_at


def test_times_are_consistent_with_steps(planner):
    plan = planner.plan("LB Nagar", "Nagole", depart_at=parse_hhmm("09:00"))
    it = plan.itinerary
    total = it.walking_min + it.waiting_min + it.in_vehicle_min
    assert total == pytest.approx(it.arrive_at - it.depart_at, abs=1e-6)
    for a, b in zip(it.legs, it.legs[1:]):
        assert b.depart_at >= a.arrive_at


def test_fewest_transfers_never_has_more_changes_than_fastest(planner):
    for o, d in [("LB Nagar", "Nagole"), ("Miyapur", "JBS Parade Ground"), ("Raidurg", "MG Bus Station")]:
        fast = planner.plan(o, d, mode="fastest", depart_at=540)
        few = planner.plan(o, d, mode="fewest_transfers", depart_at=540)
        assert few.itinerary.transfers <= fast.itinerary.transfers
        assert fast.itinerary.arrive_at <= few.itinerary.arrive_at


def test_astar_and_dijkstra_agree_on_real_network(planner):
    stations = list(planner.network.stations)
    for i in range(0, len(stations), 7):
        for j in range(3, len(stations), 11):
            if stations[i] == stations[j]:
                continue
            report = planner.compare(stations[i], stations[j], depart_at=600, include_trace=False)
            by_key = {a["key"]: a for a in report["algorithms"]}
            assert by_key["astar_td"]["result"] == by_key["dijkstra_td"]["result"]
            assert by_key["astar_td"]["stats"]["settled"] <= by_key["dijkstra_td"]["stats"]["settled"]


def test_arrive_by_leaves_as_late_as_possible(planner):
    deadline = parse_hhmm("10:00")
    plan = planner.plan("Ameerpet", "Hitech City", arrive_by=deadline)
    assert plan.meets_deadline
    one_minute_later = planner.plan("Ameerpet", "Hitech City", depart_at=plan.itinerary.depart_at + 1)
    assert one_minute_later.itinerary.arrive_at > deadline


def test_arrive_by_impossible_deadline_is_flagged(planner):
    plan = planner.plan("Miyapur", "LB Nagar", arrive_by=parse_hhmm("06:10"))
    assert plan.meets_deadline is False
    assert any("earliest possible" in n for n in plan.notes)


def test_no_service_after_last_train(planner):
    with pytest.raises(PlanningError) as exc:
        planner.plan("Ameerpet", "Uppal", depart_at=parse_hhmm("23:55"))
    assert exc.value.code == "NO_SERVICE"


def test_before_first_train_waits_for_it(planner):
    plan = planner.plan("Ameerpet", "Uppal", depart_at=parse_hhmm("00:39"))
    first = planner.network.service.first_departure
    assert plan.itinerary.board_at >= first
    assert plan.itinerary.depart_at >= first - planner.network.service.access_min
    assert plan.itinerary.arrive_at - plan.itinerary.depart_at < 60
    assert any("closed" in n for n in plan.notes)


def test_avoid_station_reroutes(planner):
    plan = planner.plan("LB Nagar", "Nagole", depart_at=540, avoid=["Ameerpet"])
    changes = {s["station"] for s in plan.itinerary.steps if s["type"] == "transfer"}
    assert changes == {"mg_bus_station", "parade_ground"}


def test_avoid_still_allows_passing_through(planner):
    plan = planner.plan("Uppal", "Jubilee Hills Check Post", depart_at=540, avoid=["Ameerpet"])
    assert plan.itinerary.transfers == 0
    assert "ameerpet" in plan.itinerary.legs[0].stations


def test_errors(planner):
    with pytest.raises(PlanningError) as e1:
        planner.plan("Ameerpet", "Ameerpet")
    assert e1.value.code == "SAME_STATION"
    with pytest.raises(PlanningError) as e2:
        planner.plan("Atlantis", "Ameerpet")
    assert e2.value.code == "UNKNOWN_STATION"
    with pytest.raises(PlanningError) as e3:
        planner.plan("Ameerpet", "Uppal", mode="teleport")
    assert e3.value.code == "BAD_MODE"


def test_fare_uses_shortest_distance_and_slabs(planner):
    fare, km = planner.fare_between("ameerpet", "sr_nagar")
    assert km < 2 and fare == 11
    fare, km = planner.fare_between("miyapur", "lb_nagar")
    assert km > 24 and fare == 69
    cheap = planner.plan("Miyapur", "LB Nagar", mode="cheapest", depart_at=540)
    assert cheap.fare == 69 and cheap.notes


def test_options_are_pareto_optimal(planner):
    plans, _ = planner.options("Raidurg", "Uppal", depart_at=540)
    assert plans
    vecs = [(p.itinerary.arrive_at, p.itinerary.transfers) for p in plans]
    for i, a in enumerate(vecs):
        for j, b in enumerate(vecs):
            if i != j:
                assert not (b[0] <= a[0] and b[1] <= a[1])
    assert plans[0].label and "Fastest" in plans[0].label


def test_alternatives_are_distinct_and_sorted(planner):
    plans, _ = planner.alternatives("Miyapur", "Nagole", k=3, depart_at=540)
    assert len(plans) >= 2
    assert len({p.itinerary.signature for p in plans}) == len(plans)
    arrivals = [p.itinerary.arrive_at for p in plans]
    assert arrivals == sorted(arrivals)


def test_plan_serializes(planner, network):
    d = planner.plan("Miyapur", "Hitech City", depart_at=540).to_dict(network, planner.tg)
    assert d["summary"]["lines"] == ["red", "blue"]
    assert d["geometry"][0]["coords"][0] == [network.stations["miyapur"].lat, network.stations["miyapur"].lon]
    assert {s["type"] for s in d["steps"]} >= {"walk", "wait", "ride", "transfer"}
