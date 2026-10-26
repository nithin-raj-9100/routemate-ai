import pytest

from routemate.network.model import crowd_label
from routemate.network.transit_graph import SINK, SOURCE
from routemate.planner.fares import FareStateGraph
from routemate.planner.planner import PlanningError


def total_minutes(plan):
    it = plan.itinerary
    return it.walking_min + it.waiting_min + it.in_vehicle_min


def test_bus_data_is_loaded(network):
    assert len(network.buses) > 100
    assert all(network.stations[s].kind == "bus" for r in network.buses.values() for s in r.stops)
    for r in network.buses.values():
        assert list(r.departures) == sorted(r.departures)
        assert all(b >= a for a, b in zip(r.offsets, r.offsets[1:]))


def test_bus_fares_follow_tgsrtc_stages(network):
    f = network.bus_fares.fare_for
    assert (f(1), f(4), f(5), f(9), f(12), f(40)) == (15, 15, 20, 25, 30, 45)


def test_trip_to_a_bus_only_place_uses_buses(planner):
    plan = planner.plan("Ameerpet", "Mehdipatnam", depart_at=540)
    it = plan.itinerary
    assert plan.buses and "bus" in it.modes
    assert it.destination.startswith("bus_")
    assert total_minutes(plan) == pytest.approx(it.arrive_at - it.depart_at, abs=1e-6)
    assert plan.fare == sum(t["fare"] for t in plan.tickets)


def test_steps_never_board_and_alight_at_the_same_stop(planner):
    for o, d in [("Ameerpet", "Mehdipatnam"), ("Charminar", "Hitech City"), ("Kondapur", "Koti")]:
        plan = planner.plan(o, d, depart_at=540)
        assert all(leg.stop_count >= 1 for leg in plan.itinerary.legs)


def test_walks_are_single_short_hops(planner, network):
    plan = planner.plan("Charminar", "Hitech City", depart_at=540)
    steps = plan.itinerary.steps
    for a, b in zip(steps, steps[1:]):
        assert not ("distanceM" in a and "distanceM" in b), "two walks in a row"
    assert all(w.distance_km <= network.walking.max_km * network.walking.detour + 1e-9 for w in plan.itinerary.walks)


def test_cheapest_is_never_dearer_than_the_metro(planner):
    for o, d in [("Miyapur", "LB Nagar"), ("Ameerpet", "Uppal"), ("Raidurg", "Nagole")]:
        metro_fare, _ = planner.fare_between(planner.resolve(o, "o"), planner.resolve(d, "d"))
        cheap = planner.plan(o, d, mode="cheapest", depart_at=540)
        assert cheap.fare <= metro_fare
        assert cheap.notes


def test_cheapest_matches_brute_force_over_the_pareto_front(planner):
    cheap = planner.plan("Ameerpet", "Mehdipatnam", mode="cheapest", depart_at=540)
    options, _ = planner.options("Ameerpet", "Mehdipatnam", depart_at=540)
    assert cheap.fare <= min(p.fare for p in options)


def test_fare_state_graph_charges_metro_on_exit(planner):
    g = FareStateGraph(planner._overlay("ameerpet", "uppal", True), planner.fares)
    out = g.out_edges((SOURCE, None))
    assert out and all(e.fare == 0 for e in out)


def test_metro_only_trips_do_not_switch_to_buses_by_default(planner):
    plan = planner.plan("Ameerpet", "Uppal", depart_at=540)
    assert not plan.buses and plan.itinerary.modes == ["metro"]


def test_bus_trips_respect_avoid(planner):
    plan = planner.plan("Ameerpet", "Mehdipatnam", depart_at=540, avoid=["Lakdikapul"])
    touched = {s for leg in plan.itinerary.legs for s in (leg.stations[0], leg.stations[-1])}
    assert "lakdikapul" not in touched


def test_options_with_buses_trade_time_for_fare(planner):
    plans, _ = planner.options("Ameerpet", "Mehdipatnam", depart_at=540)
    assert plans
    vecs = [(round(p.itinerary.arrive_at), p.itinerary.transfers, p.fare) for p in plans]
    for i, a in enumerate(vecs):
        for j, b in enumerate(vecs):
            if i != j:
                assert not all(y <= x for x, y in zip(a, b))
    assert any("Cheapest" in p.label for p in plans) and any("Fastest" in p.label for p in plans)


def test_alternatives_and_compare_are_metro_only(planner):
    with pytest.raises(PlanningError) as exc:
        planner.compare("Ameerpet", "Mehdipatnam")
    assert exc.value.code == "METRO_ONLY"


def test_crowding_follows_published_counts_and_peaks(network):
    c = network.crowding
    assert c.station("raidurg") == 1.0
    assert c.station("ameerpet") > c.station("uppal")
    assert c.factor(9 * 60) == 1.0 and c.factor(14 * 60) < 1.0 and c.factor(22 * 60) < c.factor(14 * 60)
    assert c.segment("red", "ameerpet", "punjagutta") > c.segment("green", "parade_ground", "secunderabad_west")
    assert crowd_label(0.1) == "Low" and crowd_label(0.9) == "Packed"


def test_least_crowded_never_more_crowded_than_fastest(planner):
    for o, d in [("Miyapur", "Uppal"), ("LB Nagar", "Nagole"), ("JNTU College", "Parade Ground")]:
        fast = planner.plan(o, d, mode="fastest", depart_at=540)
        calm = planner.plan(o, d, mode="least_crowded", depart_at=540)
        exposure = lambda p: sum(l.crowd_exposure for l in p.itinerary.legs)
        assert exposure(calm) <= exposure(fast) + 1e-6
        assert calm.itinerary.crowd_level is not None


def test_off_peak_trips_are_less_crowded(planner):
    peak = planner.plan("Miyapur", "Ameerpet", depart_at=9 * 60)
    late = planner.plan("Miyapur", "Ameerpet", depart_at=22 * 60)
    assert late.itinerary.crowd_level < peak.itinerary.crowd_level


def test_arrive_by_with_buses(planner):
    plan = planner.plan("Ameerpet", "Mehdipatnam", arrive_by=10 * 60)
    assert plan.meets_deadline
    later = planner.plan("Ameerpet", "Mehdipatnam", depart_at=plan.itinerary.depart_at + 1)
    assert later.itinerary.arrive_at > 10 * 60
