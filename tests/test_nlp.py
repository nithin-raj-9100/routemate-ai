import json

import pytest

from routemate.network.model import parse_hhmm
from routemate.nlp.gemini import GeminiClient, LLMError
from routemate.nlp.query import TripQuery
from routemate.nlp.rules import RuleParser
from routemate.nlp.stations import StationIndex

from .conftest import make_services


@pytest.fixture(scope="module")
def index(network):
    return StationIndex(network)


@pytest.fixture(scope="module")
def rules(index):
    return RuleParser(index)


@pytest.mark.parametrize(
    "text,origin,destination",
    [
        ("Miyapur to Hitech City", "miyapur", "hitech_city"),
        ("from lb nagar to miyapur", "lb_nagar", "miyapur"),
        ("how do I get to jubilee hills check post from uppal", "uppal", "jubilee_hills_check_post"),
        ("I'm at Ameerpeth, need to reach hitech city", "ameerpet", "hitech_city"),
        ("between kphb and dilsukhnagar", "kphb_colony", "dilsukhnagar"),
        ("mgbs to raidurg", "mg_bus_station", "raidurg"),
        ("going to road no 5 jubilee hills from nagole", "nagole", "road_no_5_jubilee_hills"),
        ("koti to jntu", "sultan_bazaar", "jntu_college"),
        ("i want to go from jntu to hitecity", "jntu_college", "hitech_city"),
        ("lbnagar to hitechcity", "lb_nagar", "hitech_city"),
    ],
)
def test_rule_parser_stations(rules, text, origin, destination):
    q = rules.parse(text, now=480)
    assert (q.origin, q.destination) == (origin, destination)


@pytest.mark.parametrize(
    "text,mode",
    [
        ("don't want to change trains much", "fewest_transfers"),
        ("direct train please", "fewest_transfers"),
        ("cheapest way", "cheapest"),
        ("I'm running late, quickest route", "fastest"),
        ("fewest stops", "fewest_stops"),
        ("shortest distance", "shortest_distance"),
        ("just a route", None),
    ],
)
def test_rule_parser_modes(rules, text, mode):
    assert rules.parse(f"Ameerpet to Uppal {text}", now=480).mode == mode


@pytest.mark.parametrize(
    "text,depart,arrive",
    [
        ("need to reach before 10", None, "10:00"),
        ("by 9:30 am", None, "09:30"),
        ("at 6pm", "18:00", None),
        ("leaving at 7:45", "07:45", None),
        ("by 10.15 pm", None, "22:15"),
        ("in 20 minutes", "08:20", None),
        ("", None, None),
    ],
)
def test_rule_parser_times(rules, text, depart, arrive):
    q = rules.parse(f"Ameerpet to Uppal {text}", now=480).to_dict()
    assert (q["departAt"], q["arriveBy"]) == (depart, arrive)


def test_numbers_inside_station_names_are_not_times(rules):
    q = rules.parse("Road No 5 Jubilee Hills to Ameerpet", now=480)
    assert q.origin == "road_no_5_jubilee_hills" and q.depart_at is None and q.arrive_by is None


def test_avoid_is_detected(rules):
    q = rules.parse("Uppal to Hitech City, avoid Ameerpet", now=480)
    assert q.avoid == ["ameerpet"] and q.destination == "hitech_city"


def test_follow_up_merges_previous_trip():
    prev = TripQuery(origin="ameerpet", destination="hitech_city", arrive_by=600)
    follow = TripQuery(mode="cheapest").merged_over(prev)
    assert (follow.origin, follow.destination, follow.mode, follow.arrive_by) == ("ameerpet", "hitech_city", "cheapest", 600)


def test_suggest_and_resolve(index):
    assert index.suggest("hit")[0] == "hitech_city"
    assert index.suggest("amee")[0] == "ameerpet"
    assert index.resolve("Ameerpeth") == "ameerpet"
    assert index.resolve("dilsuknagar") == "dilsukhnagar"
    assert index.resolve("zzzz") is None


class FakeGemini(GeminiClient):
    def __init__(self, replies):
        super().__init__("fake-key", "fake-model")
        self.replies = list(replies)
        self.calls = 0

    def generate(self, *, system, prompt, schema=None, temperature=0.2):
        self.calls += 1
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    def stream(self, *, system, prompt, schema=None, temperature=0.2):
        text = self.generate(system=system, prompt=prompt, temperature=temperature)
        for i in range(0, len(text), 7):
            yield text[i : i + 7]


def llm_reply(**kw):
    base = {"origin": "", "destination": "", "mode": "", "depart_at": "", "arrive_by": "", "avoid": [], "language": "en"}
    return base | kw


def test_llm_parse_is_validated_against_network():
    fake = FakeGemini([
        llm_reply(origin="Durgam Cheruvu", destination="Narnia Central", mode="fewest_transfers", arrive_by="10:00", language="te"),
        "Take the Blue Line.",
    ])
    s = make_services(fake)
    result = s.assistant.ask("inorbit mall nundi ameerpet ki 10 lopu", now=480)
    q = result["query"]
    assert q["origin"] == "durgam_cheruvu"
    assert q["destination"] == "ameerpet"
    assert q["parser"] == "gemini"
    assert q["language"] == "te"
    assert any("Narnia" in n for n in q["notes"])
    assert result["explanation"] == {"text": "Take the Blue Line.", "source": "gemini", "model": "fake-model"}


def test_llm_failure_falls_back_to_rules():
    fake = FakeGemini([LLMError("timeout"), LLMError("timeout")])
    s = make_services(fake)
    result = s.assistant.ask("Ameerpet to Hitech City", now=540)
    assert result["status"] == "ok"
    assert result["query"]["parser"] == "rules"
    assert result["explanation"]["source"] == "template"


def test_bad_key_disables_client():
    fake = FakeGemini([])
    fake._record_failure("400 API key not valid")
    assert not fake.enabled
    with pytest.raises(LLMError):
        list(GeminiClient.stream(fake, system="", prompt=""))


def test_assistant_asks_for_missing_origin():
    s = make_services()
    result = s.assistant.ask("take me to Hitech City", now=540)
    assert result["status"] == "clarify"
    assert "starting" in result["message"]
    follow = s.assistant.ask("from Uppal", context=result["query"], now=540)
    assert follow["status"] == "ok"
    assert follow["plan"]["origin"]["id"] == "uppal"
    assert follow["plan"]["destination"]["id"] == "hitech_city"


def test_template_explanation_mentions_key_facts():
    s = make_services()
    result = s.assistant.ask("Miyapur to Hitech City", now=540)
    text = result["explanation"]["text"]
    assert "Red Line" in text and "Blue Line" in text and "₹" in text
    assert result["plan"]["summary"]["transfers"] == 1


def test_arrive_by_through_assistant():
    s = make_services()
    result = s.assistant.ask("I'm at Ameerpet, need to reach Hitech City before 10, don't want to change trains much", now=480)
    plan = result["plan"]
    assert plan["mode"] == "fewest_transfers"
    assert plan["request"]["arriveBy"] == "10:00" and plan["request"]["meetsDeadline"]
    assert parse_hhmm(plan["summary"]["arriveAt"]) <= 600


def test_stream_events_arrive_in_order():
    fake = FakeGemini([llm_reply(origin="Ameerpet", destination="Hitech City"), "Take the Blue Line towards Raidurg."])
    s = make_services(fake)
    events = list(s.assistant.ask_stream("ameerpet to hitech city", now=540))
    names = [e for e, _ in events]
    assert names[0] == "stage" and names[-1] == "done"
    assert names.index("query") < names.index("plan") < names.index("explanation")
    deltas = "".join(d["text"] for e, d in events if e == "explanation_delta")
    assert deltas == "Take the Blue Line towards Raidurg."
    assert dict(events)["explanation"]["source"] == "gemini"


def test_generate_json_stops_reading_once_json_is_complete():
    class Streaming(GeminiClient):
        def __init__(self):
            super().__init__("fake-key", "fake-model")
            self.pulled = 0

        def stream(self, *, system, prompt, schema=None, temperature=0.2):
            for chunk in ['{"origin": "Ame', 'erpet", "n": {"a": 1}', "}", " trailing", " never-read"]:
                self.pulled += 1
                yield chunk

    client = Streaming()
    assert client.generate_json(system="", prompt="", schema={}) == {"origin": "Ameerpet", "n": {"a": 1}}
    assert client.pulled == 3


@pytest.mark.parametrize(
    "text, mode, buses",
    [
        ("Ameerpet to Uppal, I want a less crowded train", "least_crowded", None),
        ("Ameerpet to Mehdipatnam by bus", None, True),
        ("Ameerpet to Uppal, metro only please", None, False),
        ("Ameerpet to MG Bus Station", None, None),
    ],
)
def test_rule_parser_crowd_and_bus_preferences(rules, text, mode, buses):
    q = rules.parse(text, now=480)
    assert q.mode == mode and q.buses == buses


def test_rule_parser_finds_bus_only_places(rules):
    q = rules.parse("from Ameerpet to Mehdipatnam", now=480)
    assert q.origin == "ameerpet" and q.destination == "bus_mehdipatnam"
