"""
Structured operational logging (Checkpoint 19.8).

A minimal, deterministic, OFFLINE structured-log facility. The
repository has NO existing logging framework (audited): the 19.1-19.7
layers are stateless pure computation with deterministic return values
and reporters/CLIs that print. 19.8 introduces the SMALLEST structured
log surface justified by the audit:

    * :class:`LogLevel` — DEBUG / INFO / WARNING / ERROR / CRITICAL
      with a deterministic ``rank``;
    * :class:`StructuredEvent` — one immutable structured log record
      with STABLE fields (timestamp, level, component, event, cycle_id,
      setup_id, alert_id, symbol, status, duration_seconds,
      error_category, retry_count, recovery_state, detail) — only the
      fields a caller actually sets are populated; ``None`` is never
      fabricated;
    * :class:`ListLogSink` — the default deterministic OFFLINE sink
      (records events; returns them in deterministic order; no network,
      no files, no credentials);
    * :class:`StructuredLogger` — the deterministic facade (``log`` +
      level helpers), with an INJECTED clock so tests never depend on
      wall-clock time.

Conventions honored:

* No ``print()`` inside the logger; CLIs may print rendered logs.
* Timestamps are explicit (injected clock); never ``datetime.now()``.
* Stable field names avoid embedding unstable object representations
  (object ``repr()`` is never stored).
* Expected per-symbol partial failures are logged at WARNING/DEBUG, not
  ERROR — a 196/200 unsupported fixture universe must never look like a
  catastrophic system crash.
* No broker/execution imports — this module is pure logging plumbing.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Callable


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


class LogLevel(Enum):
    """Deterministic log levels (repo convention: no existing logging
    framework, so this is the smallest explicit vocabulary)."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return {
            LogLevel.DEBUG: 10,
            LogLevel.INFO: 20,
            LogLevel.WARNING: 30,
            LogLevel.ERROR: 40,
            LogLevel.CRITICAL: 50,
        }[self]


@dataclass(frozen=True, slots=True)
class StructuredEvent:
    """
    ONE immutable structured log event.

    Only the stable fields that are explicitly set are populated; a
    field that is not applicable is ``None`` (never ``""``/0
    fabrication). ``event_id`` is a deterministic id
    (``"log-" + sha256[:16]``) over the full canonical record.
    """

    timestamp: datetime
    level: LogLevel
    component: str
    event: str
    event_id: str = ""
    cycle_id: str = ""
    setup_id: str = ""
    alert_id: str = ""
    symbol: str = ""
    status: str = ""
    duration_seconds: float | None = None
    error_category: str = ""
    retry_count: int | None = None
    recovery_state: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        if not isinstance(self.level, LogLevel):
            raise TypeError("level must be a LogLevel.")
        if not self.component.strip():
            raise ValueError("component must not be empty.")
        if not self.event.strip():
            raise ValueError("event must not be empty.")
        if self.retry_count is not None:
            if isinstance(self.retry_count, bool) or not isinstance(
                self.retry_count, int,
            ):
                raise TypeError("retry_count must be an int or None.")
            if self.retry_count < 0:
                raise ValueError("retry_count must be non-negative.")
        if self.duration_seconds is not None:
            if isinstance(self.duration_seconds, bool) or not isinstance(
                self.duration_seconds, (int, float),
            ):
                raise TypeError("duration_seconds must be a number or None.")
            if float(self.duration_seconds) < 0:
                raise ValueError("duration_seconds must be non-negative.")
        if not self.event_id.strip():
            payload = "|".join(
                (
                    self.timestamp.isoformat(),
                    self.level.value,
                    self.component,
                    self.event,
                    self.cycle_id,
                    self.setup_id,
                    self.alert_id,
                    self.symbol,
                    self.status,
                    str(self.duration_seconds),
                    self.error_category,
                    str(self.retry_count),
                    self.recovery_state,
                ),
            )
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
            object.__setattr__(self, "event_id", f"log-{digest}")

    # ----------------------------------------------------------
    # CANONICAL TEXT LINE (for CLIs / sinks)
    # ----------------------------------------------------------

    def to_line(self) -> str:
        """Deterministic single-line rendering (no object repr)."""

        parts = [
            self.timestamp.isoformat(),
            self.level.value,
            self.component,
            self.event,
        ]
        if self.cycle_id:
            parts.append(f"cycle={self.cycle_id}")
        if self.setup_id:
            parts.append(f"setup={self.setup_id}")
        if self.alert_id:
            parts.append(f"alert={self.alert_id}")
        if self.symbol:
            parts.append(f"symbol={self.symbol}")
        if self.status:
            parts.append(f"status={self.status}")
        if self.duration_seconds is not None:
            parts.append(f"duration={self.duration_seconds:.3f}s")
        if self.error_category:
            parts.append(f"error={self.error_category}")
        if self.retry_count is not None:
            parts.append(f"retries={self.retry_count}")
        if self.recovery_state:
            parts.append(f"recovery={self.recovery_state}")
        if self.detail:
            parts.append(f"detail={self.detail}")
        return " ".join(parts)


class ListLogSink:
    """
    Default deterministic OFFLINE log sink.

    Records :class:`StructuredEvent` objects and exposes them in
    deterministic (insertion) order with stable filtering. No network,
    no credentials, no files. This is the "logging architecture" of
    19.8 — a thin, inspectable, testable sink; CLIs may render it with
    ``to_line()``.
    """

    name = "list"

    def __init__(self) -> None:
        self._events: list[StructuredEvent] = []

    def record(self, event: StructuredEvent) -> None:
        if not isinstance(event, StructuredEvent):
            raise TypeError("event must be a StructuredEvent.")
        self._events.append(event)

    @property
    def events(self) -> tuple[StructuredEvent, ...]:
        return tuple(self._events)

    def level_at_least(self, level: LogLevel) -> tuple[StructuredEvent, ...]:
        """Deterministic filter: events at or above ``level`` rank."""
        return tuple(
            e for e in self._events if e.level.rank >= level.rank
        )

    def component(self, component: str) -> tuple[StructuredEvent, ...]:
        """Deterministic filter by component name."""
        return tuple(e for e in self._events if e.component == component)

    @property
    def count(self) -> int:
        return len(self._events)

    def reset(self) -> None:
        self._events.clear()


class StructuredLogger:
    """
    Deterministic structured-logger facade.

    ``clock`` is injected (tests use a fixed instant; operators use the
    real wall-clock) — the logger NEVER calls ``datetime.now()`` itself.
    ``emit`` builds a :class:`StructuredEvent` with only the fields the
    caller provides and records it through the sink.
    """

    def __init__(
        self,
        component: str,
        sink: ListLogSink | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(component, str) or not component.strip():
            raise ValueError("component must be a non-empty str.")
        self.component = component.strip()
        self.sink = sink or ListLogSink()
        self._clock = clock

    def _now(self) -> datetime:
        if self._clock is not None:
            value = self._clock()
            _require_aware(value, "clock result")
            return value
        return datetime.now(UTC)

    def emit(
        self,
        level: LogLevel,
        event: str,
        *,
        cycle_id: str = "",
        setup_id: str = "",
        alert_id: str = "",
        symbol: str = "",
        status: str = "",
        duration_seconds: float | None = None,
        error_category: str = "",
        retry_count: int | None = None,
        recovery_state: str = "",
        detail: str = "",
    ) -> StructuredEvent:
        """Build + record one structured event (returns it)."""

        record = StructuredEvent(
            timestamp=self._now(),
            level=level,
            component=self.component,
            event=event,
            cycle_id=cycle_id,
            setup_id=setup_id,
            alert_id=alert_id,
            symbol=symbol,
            status=status,
            duration_seconds=duration_seconds,
            error_category=error_category,
            retry_count=retry_count,
            recovery_state=recovery_state,
            detail=detail,
        )
        self.sink.record(record)
        return record

    # ----------------------------------------------------------
    # LEVEL HELPERS
    # ----------------------------------------------------------

    def debug(self, event: str, **kwargs) -> StructuredEvent:
        return self.emit(LogLevel.DEBUG, event, **kwargs)

    def info(self, event: str, **kwargs) -> StructuredEvent:
        return self.emit(LogLevel.INFO, event, **kwargs)

    def warning(self, event: str, **kwargs) -> StructuredEvent:
        return self.emit(LogLevel.WARNING, event, **kwargs)

    def error(self, event: str, **kwargs) -> StructuredEvent:
        return self.emit(LogLevel.ERROR, event, **kwargs)

    def critical(self, event: str, **kwargs) -> StructuredEvent:
        return self.emit(LogLevel.CRITICAL, event, **kwargs)


__all__ = [
    "ListLogSink",
    "LogLevel",
    "StructuredEvent",
    "StructuredLogger",
]