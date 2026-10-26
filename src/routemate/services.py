from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from typing import Any

from . import db
from .config import Settings
from .network.model import Network
from .nlp.assistant import Assistant
from .nlp.gemini import GeminiClient
from .planner.planner import RoutePlanner


@dataclass
class Services:
    settings: Settings
    conn: sqlite3.Connection
    network: Network
    planner: RoutePlanner
    assistant: Assistant
    gemini: GeminiClient | None
    db_lock: threading.Lock = field(default_factory=threading.Lock)

    def log(self, **fields: Any) -> None:
        with self.db_lock:
            db.log_search(self.conn, **fields)

    def history(self, limit: int) -> list[dict[str, Any]]:
        with self.db_lock:
            return db.recent_searches(self.conn, limit)

    def save_trip(self, **fields: Any) -> int:
        with self.db_lock:
            return db.save_trip(self.conn, **fields)

    def trips(self) -> list[dict[str, Any]]:
        with self.db_lock:
            return db.saved_trips(self.conn)

    def delete_trip(self, trip_id: int) -> bool:
        with self.db_lock:
            return db.delete_trip(self.conn, trip_id)


def build_services(settings: Settings) -> Services:
    conn, network = db.open_network(settings.db_path)
    planner = RoutePlanner(network)
    gemini = GeminiClient(settings.gemini_api_key, settings.gemini_model) if settings.ai_enabled else None
    return Services(settings, conn, network, planner, Assistant(planner, gemini), gemini)
