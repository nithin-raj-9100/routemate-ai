from __future__ import annotations

import argparse

from .config import load_settings


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
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
