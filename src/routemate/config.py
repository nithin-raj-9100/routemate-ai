from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_MODEL = "gemini-3.1-flash-lite"


@dataclass(frozen=True)
class Settings:
    db_path: str
    gemini_api_key: str | None
    gemini_model: str
    ai_enabled: bool
    host: str
    port: int


def load_settings(**overrides) -> Settings:
    load_dotenv(Path.cwd() / ".env", override=True)
    values = dict(
        db_path=os.getenv("ROUTEMATE_DB", "var/routemate.db"),
        gemini_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        gemini_model=os.getenv("ROUTEMATE_GEMINI_MODEL", DEFAULT_MODEL),
        ai_enabled=os.getenv("ROUTEMATE_AI", "on").lower() not in ("off", "0", "false", "no"),
        host=os.getenv("ROUTEMATE_HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", "5050")),
    )
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**values)
