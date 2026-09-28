from __future__ import annotations

import time
from typing import Any, Iterator

from ..planner.planner import PlanningError, RoutePlanner
from .explain import Explainer
from .gemini import GeminiClient, LLMError
from .llm_parser import LLMParser, combine
from .query import TripQuery
from .rules import RuleParser


class Assistant:
    """Natural-language front door: understand -> plan with real algorithms -> explain.

    Gemini only ever interprets the question and phrases the answer; the route itself always
    comes from the planner, so answers are provably optimal and never made up.
    """

    def __init__(self, planner: RoutePlanner, client: GeminiClient | None) -> None:
        self.planner = planner
        self.client = client
        self.rules = RuleParser(planner.stations)
        self.llm = LLMParser(client, planner.stations, planner.network) if client else None
        names = {sid: st.name for sid, st in planner.network.stations.items()}
        self.explainer = Explainer(client, names)

    def understand(self, text: str, now: float, previous: TripQuery | None) -> TripQuery:
        offline = self.rules.parse(text, now)
        if self.llm and self.client and self.client.enabled:
            try:
                return combine(self.llm.parse(text, now, previous), offline).merged_over(previous)
            except LLMError as exc:
                offline.notes.append(f"AI parser unavailable ({exc}); used the offline parser.")
        return offline.merged_over(previous)

    def ask_stream(
        self, text: str, *, context: dict[str, Any] | None = None, now: float | None = None, language: str | None = None
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Yields (event, data) as each stage finishes: understood -> route -> explanation chunks -> done.

        Raises PlanningError if the understood trip cannot be planned.
        """
        started = time.perf_counter()
        elapsed = lambda: round((time.perf_counter() - started) * 1000, 1)
        now = self.planner.now() if now is None else now
        yield "stage", {"stage": "understanding", "ai": bool(self.client and self.client.enabled)}
        query = self.understand(text, now, TripQuery.from_dict(context))
        if language:
            query.language = language
        query_dict = self._query_dict(query)

        missing = [role for role in ("origin", "destination") if not getattr(query, role)]
        if missing:
            ask_for = (
                "Where are you starting from?" if missing == ["origin"]
                else "Where would you like to go?" if missing == ["destination"]
                else "Which stations are you travelling between?"
            )
            yield "clarify", {"message": f"{ask_for} (e.g. 'from Ameerpet to Hitech City')", "query": query_dict, "elapsedMs": elapsed()}
            return
        yield "query", query_dict

        net = self.planner.network
        plan = self.planner.plan(
            query.origin,
            query.destination,
            mode=query.mode or "fastest",
            depart_at=query.depart_at,
            arrive_by=query.arrive_by,
            avoid=query.avoid,
        )
        plan_dict = plan.to_dict(net, self.planner.tg)
        try:
            options, _ = self.planner.options(query.origin, query.destination, depart_at=plan.itinerary.depart_at, avoid=query.avoid)
            options_dicts = [o.to_dict(net, self.planner.tg) for o in options]
        except PlanningError:
            options_dicts = []
        yield "plan", {"plan": plan_dict, "options": options_dicts, "elapsedMs": elapsed()}

        yield "stage", {"stage": "explaining", "ai": bool(self.client and self.client.enabled)}
        yield from self.explainer.stream(plan_dict, options_dicts, text, query.language)
        yield "done", {"elapsedMs": elapsed()}

    def ask(self, text: str, *, context: dict[str, Any] | None = None, now: float | None = None, language: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "ok"}
        for event, data in self.ask_stream(text, context=context, now=now, language=language):
            if event == "clarify":
                return {"status": "clarify", **data}
            if event == "query":
                result["query"] = data
            elif event == "plan":
                result["plan"], result["options"] = data["plan"], data["options"]
            elif event == "explanation":
                result["explanation"] = data
            elif event == "done":
                result["elapsedMs"] = data["elapsedMs"]
        return result

    def _query_dict(self, q: TripQuery) -> dict[str, Any]:
        d = q.to_dict()
        st = self.planner.network.stations
        d["originName"] = st[q.origin].name if q.origin else None
        d["destinationName"] = st[q.destination].name if q.destination else None
        d["avoidNames"] = [st[a].name for a in q.avoid]
        return d
