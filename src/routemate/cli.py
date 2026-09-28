from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .config import load_settings
from .network.model import parse_hhmm
from .planner.planner import MODES, PlanningError

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
LINE_COLORS = {"red": "\033[31m", "blue": "\033[34m", "green": "\033[32m"}


def _time(value: str | None) -> float | None:
    return parse_hhmm(value) if value else None


def _print_plan(plan: dict[str, Any]) -> None:
    s = plan["summary"]
    title = f"{plan['origin']['name']} → {plan['destination']['name']}"
    print(f"\n{BOLD}{title}{RESET}  {DIM}({plan['mode']}){RESET}")
    print(
        f"  leave {s['leaveAt']} · board {s['boardAt']} · arrive {BOLD}{s['arriveAt']}{RESET} · "
        f"{s['totalMinutes']:.0f} min · {s['transfers']} change(s) · {s['stops']} stops · "
        f"{s['distanceKm']} km · ₹{s['fare']:g}"
    )
    for step in plan["steps"]:
        color = LINE_COLORS.get(step.get("line", ""), "")
        if step["type"] == "ride":
            print(f"  {color}■{RESET} {step['text']}  {DIM}{step['departAt']}–{step['arriveAt']}{RESET}")
        else:
            print(f"  {DIM}·{RESET} {step['text']}")
    for note in plan["notes"]:
        print(f"  {DIM}note: {note}{RESET}")


def cmd_init_db(args) -> None:
    from . import db

    settings = load_settings(db_path=args.db)
    conn = db.connect(settings.db_path)
    changed = db.seed(conn, force=args.force)
    net = db.load_network(conn)
    print(f"{'Seeded' if changed else 'Already up to date:'} {settings.db_path} — {len(net.stations)} stations, {len(net.lines)} lines")


def cmd_serve(args) -> None:
    from .web.app import create_app

    settings = load_settings(db_path=args.db, port=args.port, host=args.host)
    app = create_app(settings)
    ai = app.extensions["routemate"].gemini
    status = "Gemini " + settings.gemini_model if ai and ai.enabled else "offline rules (set GEMINI_API_KEY to enable Gemini)"
    print(f"RouteMate AI on http://{settings.host}:{settings.port}  ·  AI: {status}")
    app.run(host=settings.host, port=settings.port, debug=args.debug)


def _services(args):
    from .services import build_services

    return build_services(load_settings(db_path=args.db))


def cmd_route(args) -> None:
    s = _services(args)
    plan = s.planner.plan(args.origin, args.destination, mode=args.mode, depart_at=_time(args.at),
                          arrive_by=_time(args.arrive_by), avoid=args.avoid or [])
    d = plan.to_dict(s.network, s.planner.tg)
    if args.json:
        print(json.dumps(d, indent=2, ensure_ascii=False))
        return
    _print_plan(d)
    print(f"  {DIM}search: {plan.stats.algorithm}, {plan.stats.settled} nodes settled in {plan.stats.elapsed_ms:.2f} ms{RESET}")


def cmd_options(args) -> None:
    s = _services(args)
    plans, stats = s.planner.options(args.origin, args.destination, depart_at=_time(args.at))
    for p in plans:
        print(f"\n{BOLD}[{p.label}]{RESET}", end="")
        _print_plan(p.to_dict(s.network, s.planner.tg))
    print(f"\n{DIM}Pareto search: {stats.settled} labels settled in {stats.elapsed_ms:.2f} ms{RESET}")


def cmd_ask(args) -> None:
    s = _services(args)
    result = s.assistant.ask(" ".join(args.text), now=_time(args.at))
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    q = result["query"]
    print(f"{DIM}understood ({q['parser']}): {q['originName']} → {q['destinationName']}, mode={q['mode'] or 'fastest'}, "
          f"depart={q['departAt'] or 'now'}, arriveBy={q['arriveBy'] or '-'}{RESET}")
    if result["status"] != "ok":
        print(result["message"])
        return
    _print_plan(result["plan"])
    print(f"\n{BOLD}RouteMate:{RESET} {result['explanation']['text']}  {DIM}[{result['explanation']['source']}]{RESET}")


def cmd_compare(args) -> None:
    s = _services(args)
    report = s.planner.compare(args.origin, args.destination, depart_at=_time(args.at), include_trace=False)
    print(f"{BOLD}{args.origin} → {args.destination}{RESET} at {report['departAt']}  "
          f"{DIM}(graph: {report['graph']['nodes']} nodes, {report['graph']['edges']} edges){RESET}\n")
    print(f"  {'algorithm':42} {'result':28} {'settled':>8} {'relaxed':>8} {'ms':>8}")
    for a in report["algorithms"]:
        res = ", ".join(f"{k}={v}" for k, v in (a["result"] or {}).items())
        st = a["stats"]
        print(f"  {a['title']:42} {res:28} {st['settled']:>8} {st['relaxed']:>8} {st['elapsedMs']:>8.3f}")
    print()
    for line in report["insights"]:
        print(f"  • {line}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="routemate", description="RouteMate AI — smart Hyderabad Metro route planner")
    p.add_argument("--db", help="SQLite path (default: $ROUTEMATE_DB or var/routemate.db)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="create and seed the SQLite database")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("serve", help="run the web app")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s.add_argument("--debug", action="store_true")
    s.set_defaults(func=cmd_serve)

    for name, func, help_ in (("route", cmd_route, "plan one route"), ("options", cmd_options, "Pareto trade-offs"), ("compare", cmd_compare, "benchmark all algorithms")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("origin")
        s.add_argument("destination")
        s.add_argument("--at", help="departure time HH:MM (default now)")
        if name == "route":
            s.add_argument("--mode", default="fastest", choices=list(MODES))
            s.add_argument("--arrive-by", help="deadline HH:MM")
            s.add_argument("--avoid", nargs="*")
            s.add_argument("--json", action="store_true")
        s.set_defaults(func=func)

    s = sub.add_parser("ask", help="ask in plain English")
    s.add_argument("text", nargs="+")
    s.add_argument("--at", help="pretend the current time is HH:MM")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_ask)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except PlanningError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        sys.exit(2)
