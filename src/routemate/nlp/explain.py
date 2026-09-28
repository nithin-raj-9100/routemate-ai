from __future__ import annotations

import json
from typing import Any, Iterator

from .gemini import GeminiClient, LLMError

LANGUAGES = {"en": "English", "te": "Telugu", "hi": "Hindi", "ta": "Tamil", "ur": "Urdu", "kn": "Kannada"}

MODE_REASON = {
    "fastest": "it gets you there earliest",
    "cheapest": "metro fares are fixed between two stations, so the fastest route costs the same",
    "fewest_transfers": "it needs the fewest line changes",
    "fewest_stops": "it passes the fewest stations",
    "shortest_distance": "it covers the least track distance",
}


def facts_from(plan: dict[str, Any], options: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The only information the LLM may use: a compact, already-verified summary of the route."""
    s = plan["summary"]
    legs = [
        {
            "line": step["lineName"],
            "from": step["from"],
            "to": step["to"],
            "towards": step["towardsName"],
            "stops": step["stopCount"],
            "departAt": step["departAt"],
            "arriveAt": step["arriveAt"],
        }
        for step in plan["steps"]
        if step["type"] == "ride"
    ]
    return {
        "origin": plan["origin"]["name"],
        "destination": plan["destination"]["name"],
        "mode": plan["mode"],
        "leaveAt": s["leaveAt"],
        "arriveAt": s["arriveAt"],
        "totalMinutes": round(s["totalMinutes"]),
        "lineChanges": s["transfers"],
        "changeAt": [st["station"] for st in plan["steps"] if st["type"] == "transfer"],
        "stops": s["stops"],
        "fare": f"₹{s['fare']:g}",
        "legs": legs,
        "deadline": plan["request"]["arriveBy"],
        "meetsDeadline": plan["request"]["meetsDeadline"],
        "otherOptions": [
            {"label": o["label"], "arriveAt": o["summary"]["arriveAt"], "lineChanges": o["summary"]["transfers"]}
            for o in (options or [])
            if o["summary"]["lines"] != s["lines"] or o["summary"]["arriveAt"] != s["arriveAt"]
        ],
        "avoidedStations": [
            {"station": a["name"], "trainPassesThroughWithoutStoppingForYou": a["passesThrough"]}
            for a in plan["request"]["avoid"]
        ],
        "notes": plan["notes"],
    }


def template_explanation(plan: dict[str, Any], names: dict[str, str]) -> str:
    s = plan["summary"]
    rides = [st for st in plan["steps"] if st["type"] == "ride"]
    parts: list[str] = []
    for i, r in enumerate(rides):
        verb = "Take" if i == 0 else "then change to"
        parts.append(
            f"{verb} the {r['lineName']} from {names[r['from']]} towards {r['towardsName']} and get off at "
            f"{names[r['to']]} ({r['stopCount']} stop{'s' if r['stopCount'] != 1 else ''}, about {round(r['minutes'])} min)"
        )
    route = "; ".join(parts) + "."
    route = route[0].upper() + route[1:]
    changes = "No line changes needed." if s["transfers"] == 0 else f"{s['transfers']} line change{'s' if s['transfers'] > 1 else ''}."
    timing = f"Leave at {s['leaveAt']}, board at {s['boardAt']} and arrive around {s['arriveAt']} — {round(s['totalMinutes'])} min door to door. Fare ₹{s['fare']:g}."
    reason = MODE_REASON.get(plan["mode"])
    why = f" Chosen because {reason}." if reason else ""
    deadline = ""
    if plan["request"]["arriveBy"]:
        deadline = (
            f" You'll make your {plan['request']['arriveBy']} deadline."
            if plan["request"]["meetsDeadline"]
            else f" Sorry, no train reaches before {plan['request']['arriveBy']}."
        )
    return f"{route} {changes} {timing}{why}{deadline}"


class Explainer:
    def __init__(self, client: GeminiClient | None, names: dict[str, str]) -> None:
        self.client = client
        self.names = names

    def _messages(self, plan: dict[str, Any], options: list[dict[str, Any]] | None, request_text: str | None, language: str) -> tuple[str, str]:
        facts = facts_from(plan, options)
        facts["changeAt"] = [self.names.get(s, s) for s in facts["changeAt"]]
        for leg in facts["legs"]:
            leg["from"], leg["to"] = self.names[leg["from"]], self.names[leg["to"]]
        lang = LANGUAGES.get(language, "English")
        system = (
            "You are RouteMate, a friendly Hyderabad Metro guide. Explain the route in 2-4 short sentences in "
            f"{lang}. Use ONLY the facts in the JSON: never invent stations, lines, times, fares or crowd levels. "
            "Say which line to board and in which direction, where to change (if anywhere), when they arrive and the fare, "
            "and briefly why this route fits what they asked compared with the other options. "
            "For avoided stations: you never board or change there, but if the train passes through, say so honestly. "
            "No greeting, no sign-off, plain text, no markdown."
        )
        prompt = f"User asked: {request_text or '(route form)'}\nRoute facts:\n{json.dumps(facts, ensure_ascii=False)}"
        return system, prompt

    def stream(
        self, plan: dict[str, Any], options: list[dict[str, Any]] | None, request_text: str | None, language: str = "en"
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yields ("explanation_delta", {text}) chunks, then one final ("explanation", {...}) that is authoritative."""
        fallback = template_explanation(plan, self.names)
        if not self.client or not self.client.enabled:
            yield "explanation", {"text": fallback, "source": "template"}
            return
        system, prompt = self._messages(plan, options, request_text, language)
        parts: list[str] = []
        try:
            for chunk in self.client.stream(system=system, prompt=prompt, temperature=0.4):
                parts.append(chunk)
                yield "explanation_delta", {"text": chunk}
        except LLMError as exc:
            yield "explanation", {"text": fallback, "source": "template", "aiError": str(exc)}
            return
        yield "explanation", {"text": "".join(parts).strip(), "source": "gemini", "model": self.client.model}

    def explain(self, plan: dict[str, Any], options: list[dict[str, Any]] | None, request_text: str | None, language: str = "en") -> dict[str, Any]:
        final: dict[str, Any] = {}
        for event, data in self.stream(plan, options, request_text, language):
            if event == "explanation":
                final = data
        return final
