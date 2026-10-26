from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..network.model import fmt_hhmm, parse_hhmm


@dataclass
class TripQuery:
    """A structured trip request, whether it came from a form, the rule parser or the LLM."""

    origin: str | None = None
    destination: str | None = None
    mode: str | None = None
    depart_at: float | None = None
    arrive_by: float | None = None
    avoid: list[str] = field(default_factory=list)
    buses: bool | None = None
    language: str = "en"
    parser: str = "rules"
    notes: list[str] = field(default_factory=list)

    def merged_over(self, previous: "TripQuery | None") -> "TripQuery":
        """Follow-up questions ("what about the cheapest?") keep whatever the new text did not say."""
        if previous is None:
            return self
        return TripQuery(
            origin=self.origin or previous.origin,
            destination=self.destination or previous.destination,
            mode=self.mode or previous.mode,
            depart_at=self.depart_at if (self.depart_at is not None or self.arrive_by is not None) else previous.depart_at,
            arrive_by=self.arrive_by if (self.depart_at is not None or self.arrive_by is not None) else previous.arrive_by,
            avoid=self.avoid or previous.avoid,
            buses=self.buses if self.buses is not None else previous.buses,
            language=self.language,
            parser=self.parser,
            notes=self.notes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "mode": self.mode,
            "departAt": fmt_hhmm(self.depart_at) if self.depart_at is not None else None,
            "arriveBy": fmt_hhmm(self.arrive_by) if self.arrive_by is not None else None,
            "avoid": self.avoid,
            "buses": self.buses,
            "language": self.language,
            "parser": self.parser,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "TripQuery | None":
        if not data:
            return None

        def t(key: str) -> float | None:
            v = data.get(key)
            try:
                return parse_hhmm(v) if isinstance(v, str) and v else None
            except ValueError:
                return None

        return cls(
            origin=data.get("origin") or None,
            destination=data.get("destination") or None,
            mode=data.get("mode") or None,
            depart_at=t("departAt"),
            arrive_by=t("arriveBy"),
            avoid=list(data.get("avoid") or []),
            buses=data.get("buses") if isinstance(data.get("buses"), bool) else None,
            language=data.get("language") or "en",
        )
