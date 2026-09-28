from __future__ import annotations

import json
import time
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context

from ..network.model import fmt_hhmm, parse_hhmm
from ..planner.planner import MODES, PlanningError
from ..services import Services

api = Blueprint("api", __name__, url_prefix="/api")


def services() -> Services:
    return current_app.extensions["routemate"]


def _time_arg(name: str) -> float | None:
    raw = (request.args.get(name) or "").strip()
    if not raw or raw.lower() == "now":
        return None
    try:
        return parse_hhmm(raw)
    except ValueError as exc:
        raise PlanningError("BAD_TIME", f"'{name}' must be HH:MM (24h), got '{raw}'") from exc


def _required(name: str) -> str:
    value = (request.args.get(name) or "").strip()
    if not value:
        raise PlanningError("MISSING_PARAM", f"Query parameter '{name}' is required")
    return value


def _avoid_arg() -> list[str]:
    return [a.strip() for a in (request.args.get("avoid") or "").split(",") if a.strip()]


@api.errorhandler(PlanningError)
def _planning_error(exc: PlanningError):
    return jsonify({"error": {"code": exc.code, "message": exc.message}}), exc.status


@api.get("/health")
def health():
    s = services()
    return jsonify(
        {
            "status": "ok",
            "network": {"city": s.network.city, "stations": len(s.network.stations), "lines": len(s.network.lines)},
            "graph": {"nodes": len(s.planner.tg.graph), "edges": s.planner.tg.graph.edge_count},
            "ai": s.gemini.status() if s.gemini else {"provider": "gemini", "enabled": False, "reason": "disabled by ROUTEMATE_AI"},
        }
    )


@api.get("/network")
def network():
    s = services()
    net = s.network
    return jsonify(
        {
            "city": net.city,
            "system": net.system,
            "service": {
                "firstDeparture": fmt_hhmm(net.service.first_departure),
                "lastDeparture": fmt_hhmm(net.service.last_departure),
            },
            "fare": {"currency": net.fares.currency, "source": net.fares.source},
            "modes": [{"id": k, "description": v} for k, v in MODES.items()],
            "lines": [
                {
                    "id": l.id,
                    "name": l.name,
                    "corridor": l.corridor,
                    "color": l.color,
                    "lengthKm": l.length_km,
                    "stations": list(l.stations),
                    "coords": [[net.stations[sid].lat, net.stations[sid].lon] for sid in l.stations],
                }
                for l in net.lines.values()
            ],
            "stations": [
                {
                    "id": st.id,
                    "name": st.name,
                    "lat": st.lat,
                    "lon": st.lon,
                    "lines": net.lines_at(st.id),
                    "interchange": net.is_interchange(st.id),
                    "aliases": list(st.aliases),
                }
                for st in net.stations.values()
            ],
        }
    )


@api.get("/stations/suggest")
def suggest():
    s = services()
    q = request.args.get("q", "")
    limit = min(max(int(request.args.get("limit", 8)), 1), 20)
    net = s.network
    return jsonify(
        {
            "query": q,
            "results": [
                {"id": sid, "name": net.stations[sid].name, "lines": net.lines_at(sid)}
                for sid in s.planner.stations.suggest(q, limit)
            ],
        }
    )


@api.get("/route")
def route():
    s = services()
    started = time.perf_counter()
    origin, destination = _required("from"), _required("to")
    mode = request.args.get("mode") or "fastest"
    trace = request.args.get("trace") in ("1", "true")
    try:
        plan = s.planner.plan(
            origin,
            destination,
            mode=mode,
            depart_at=_time_arg("departAt"),
            arrive_by=_time_arg("arriveBy"),
            avoid=_avoid_arg(),
            trace=trace,
        )
    except PlanningError:
        s.log(channel="form", origin=origin, destination=destination, mode=mode, found=False,
              elapsed_ms=(time.perf_counter() - started) * 1000)
        raise
    body = plan.to_dict(s.network, s.planner.tg, include_trace=trace)
    s.log(channel="form", origin=plan.itinerary.origin, destination=plan.itinerary.destination, mode=plan.mode,
          found=True, total_min=body["summary"]["totalMinutes"], elapsed_ms=(time.perf_counter() - started) * 1000)
    return jsonify(body)


@api.get("/routes/options")
def options():
    s = services()
    plans, stats = s.planner.options(_required("from"), _required("to"), depart_at=_time_arg("departAt"), avoid=_avoid_arg())
    return jsonify({"options": [p.to_dict(s.network, s.planner.tg) for p in plans], "search": stats.as_dict()})


@api.get("/routes/alternatives")
def alternatives():
    s = services()
    k = min(max(int(request.args.get("k", 3)), 1), 6)
    plans, stats = s.planner.alternatives(_required("from"), _required("to"), k=k, depart_at=_time_arg("departAt"))
    return jsonify({"alternatives": [p.to_dict(s.network, s.planner.tg) for p in plans], "search": stats.as_dict()})


@api.get("/algorithms/compare")
def compare():
    s = services()
    return jsonify(s.planner.compare(_required("from"), _required("to"), depart_at=_time_arg("departAt")))


def _ask_payload() -> tuple[str, dict[str, Any]]:
    payload: dict[str, Any] = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        raise PlanningError("MISSING_TEXT", "Say where you want to go, e.g. 'Ameerpet to Hitech City before 10'")
    if len(text) > 500:
        raise PlanningError("TEXT_TOO_LONG", "Please keep the request under 500 characters")
    return text, payload


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@api.post("/ask/stream")
def ask_stream():
    """Server-sent events: stage, query | clarify, plan, explanation_delta*, explanation, done (or error)."""
    s = services()
    text, payload = _ask_payload()
    started = time.perf_counter()

    def events():
        query: dict[str, Any] = {}
        total = None
        found = False
        try:
            for event, data in s.assistant.ask_stream(text, context=payload.get("context"), language=payload.get("language")):
                if event in ("query", "clarify"):
                    query = data if event == "query" else data["query"]
                elif event == "plan":
                    found, total = True, data["plan"]["summary"]["totalMinutes"]
                yield _sse(event, data)
        except PlanningError as exc:
            yield _sse("error", {"code": exc.code, "message": exc.message})
        s.log(channel="ask-stream", query=text, origin=query.get("origin"), destination=query.get("destination"),
              mode=query.get("mode"), parser=query.get("parser"), found=found, total_min=total,
              elapsed_ms=(time.perf_counter() - started) * 1000)

    return Response(
        stream_with_context(events()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@api.post("/ask")
def ask():
    s = services()
    started = time.perf_counter()
    text, payload = _ask_payload()
    try:
        result = s.assistant.ask(text, context=payload.get("context"), language=payload.get("language"))
    except PlanningError as exc:
        s.log(channel="ask", query=text, found=False, elapsed_ms=(time.perf_counter() - started) * 1000)
        return jsonify({"status": "error", "error": {"code": exc.code, "message": exc.message}}), exc.status
    q = result["query"]
    s.log(
        channel="ask",
        query=text,
        origin=q.get("origin"),
        destination=q.get("destination"),
        mode=q.get("mode"),
        parser=q.get("parser"),
        found=result["status"] == "ok",
        total_min=result.get("plan", {}).get("summary", {}).get("totalMinutes"),
        elapsed_ms=(time.perf_counter() - started) * 1000,
    )
    return jsonify(result)


@api.get("/history")
def history():
    limit = min(max(int(request.args.get("limit", 20)), 1), 100)
    return jsonify({"searches": services().history(limit)})
