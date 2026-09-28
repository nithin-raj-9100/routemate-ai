from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterator

AUTH_HINTS = ("api key not valid", "api_key_invalid", "permission_denied", "unauthenticated", "401", "403")
FAILURES_BEFORE_COOLDOWN = 3
COOLDOWN_S = 60.0


class LLMError(Exception):
    pass


class GeminiClient:
    """Thin wrapper over the google-genai Interactions API with a circuit breaker.

    A missing or rejected key disables the client for the process; repeated transient failures
    pause it for a minute. Callers always have an offline path, so the app never blocks on the LLM.
    """

    def __init__(self, api_key: str | None, model: str, timeout_s: float = 45.0) -> None:
        self.api_key = (api_key or "").strip() or None
        self.model = model
        self.timeout_s = timeout_s
        self.last_error: str | None = None
        self._client: Any = None
        self._disabled_reason: str | None = None if self.api_key else "GEMINI_API_KEY is not set"
        self._failures = 0
        self._cooldown_until = 0.0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._disabled_reason is None and time.monotonic() >= self._cooldown_until

    def status(self) -> dict[str, Any]:
        return {
            "provider": "gemini",
            "model": self.model,
            "enabled": self.enabled,
            "reason": self._disabled_reason or (self.last_error if not self.enabled else None),
        }

    def _get(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _body(self, system: str, prompt: str, schema: dict | None, temperature: float) -> dict[str, Any]:
        if not self.enabled:
            raise LLMError(self._disabled_reason or "Gemini is cooling down after repeated failures")
        body: dict[str, Any] = {
            "model": self.model,
            "system_instruction": system,
            "input": prompt,
            "generation_config": {"temperature": temperature, "thinking_level": "minimal"},
        }
        if schema is not None:
            body["response_format"] = {"type": "text", "mime_type": "application/json", "schema": schema}
        return body

    def stream(self, *, system: str, prompt: str, schema: dict | None = None, temperature: float = 0.2) -> Iterator[str]:
        """Yield text chunks as Gemini produces them. Callers may stop iterating early."""
        body = self._body(system, prompt, schema, temperature)
        produced = False
        try:
            for event in self._get().interactions.create(timeout=self.timeout_s, stream=True, **body):
                delta = getattr(event, "delta", None)
                if delta is not None and getattr(delta, "type", None) == "text" and getattr(delta, "text", None):
                    produced = True
                    yield delta.text
            if not produced:
                raise LLMError("Gemini returned an empty response")
        except LLMError as exc:
            self._record_failure(str(exc))
            raise
        except Exception as exc:
            self._record_failure(f"{type(exc).__name__}: {exc}")
            raise LLMError(self.last_error) from exc
        with self._lock:
            self._failures = 0
            self.last_error = None

    def generate(self, *, system: str, prompt: str, schema: dict | None = None, temperature: float = 0.2) -> str:
        return "".join(self.stream(system=system, prompt=prompt, schema=schema, temperature=temperature))

    def generate_json(self, *, system: str, prompt: str, schema: dict, temperature: float = 0.1) -> dict[str, Any]:
        """Returns as soon as the streamed text is a complete JSON object.

        Gemini keeps the stream open for several seconds after the last token; a structured
        answer is usable the moment it parses, so there is no reason to wait for that tail.
        """
        buffer = ""
        for chunk in self.stream(system=system, prompt=prompt, schema=schema, temperature=temperature):
            buffer += chunk
            if "}" in chunk:
                try:
                    return json.loads(_strip_fences(buffer))
                except json.JSONDecodeError:
                    continue
        try:
            return json.loads(_strip_fences(buffer))
        except json.JSONDecodeError as exc:
            self._record_failure(f"invalid JSON from Gemini: {exc}")
            raise LLMError(self.last_error) from exc

    def _record_failure(self, message: str) -> None:
        with self._lock:
            self.last_error = message[:300]
            if any(h in message.lower() for h in AUTH_HINTS):
                self._disabled_reason = "Gemini rejected the API key; check GEMINI_API_KEY"
                return
            self._failures += 1
            if self._failures >= FAILURES_BEFORE_COOLDOWN:
                self._cooldown_until = time.monotonic() + COOLDOWN_S
                self._failures = 0


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text.strip())
