"""
Shared deterministic fixtures for Checkpoint 19.8 (demo + tests).

Builds a REAL deterministic 19.6 lifecycle cycle (through the FROZEN
19.5/19.6 layers) and a REAL 19.7 alert engine + alert cycle, so the
19.8 reliability layer is exercised against genuine upstream artifacts —
never hand-built fake alert objects.

All helpers are deterministic and offline (injected clock; no network;
no broker/execution).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from dashboard.setup_lifecycle import (
    SetupLifecycleEngine,
    SetupLifecycleStore,
)
from dashboard.user_alerts import (
    AlertEngine,
    AlertStore,
    ConsoleAlertSink,
)
from engine.config.reliability_config import RetryPolicy
from engine.config.setup_lifecycle_config import SetupLifecycleConfig
from engine.config.user_alerts_config import UserAlertConfig
from engine.models.setup_lifecycle import (
    LIFECYCLE_CYCLE_ID_PREFIX,
    SetupLifecycleCycleResult,
)


def T(i: int, base: datetime | None = None) -> datetime:
    """Deterministic 15-minute-stepped instant from ``base`` (or the
    canonical Friday 11:00 IST reference)."""
    anchor = base if base is not None else datetime(2026, 9, 4, 5, 30, tzinfo=UTC)
    return anchor + timedelta(minutes=15 * i)


def _bullish(instrument: str = "RELIANCE"):
    from tests.test_setup_lifecycle import _bullish

    return _bullish(instrument)


def _bearish(instrument: str = "RELIANCE"):
    from tests.test_setup_lifecycle import _bearish

    return _bearish(instrument)


def _no_setup(instrument: str = "RELIANCE"):
    from tests.test_setup_lifecycle import _no_setup

    return _no_setup(instrument)


def build_deterministic_lifecycle_cycle(
    reference_now: datetime | None = None,
    *,
    scan_cycle_id: str = "c1",
    instruments: tuple[str, ...] = ("RELIANCE", "TCS"),
) -> tuple[SetupLifecycleCycleResult, datetime]:
    """
    Build a REAL 19.6 lifecycle-processing cycle over two instruments:

    * RELIANCE  -> a BULLISH qualified assessment (confirmation event);
    * TCS       -> a clean NO_SETUP assessment (absent, no event).

    Returns ``(lifecycle_cycle, reference_now)``.
    """

    now = reference_now if reference_now is not None else T(0)
    eng = SetupLifecycleEngine(
        SetupLifecycleConfig(), SetupLifecycleStore(),
    )
    results = []
    results.append(
        eng.process(
            _bullish("RELIANCE"), scan_cycle_id, timestamp=T(0, now),
        ),
    )
    results.append(
        eng.process(
            _no_setup("TCS"), scan_cycle_id, timestamp=T(0, now),
        ),
    )
    cycle = SetupLifecycleCycleResult(
        cycle_id=LIFECYCLE_CYCLE_ID_PREFIX + "19_8_fixture",
        scan_cycle_id=scan_cycle_id,
        reference_now=now,
        instruments=tuple(sorted(instruments)),
        results=tuple(results),
    )
    return cycle, now


def build_alert_engine(
    retry_policy: RetryPolicy | None = None,
    *,
    channels=None,
) -> AlertEngine:
    """
    Build a REAL 19.7 alert engine with a fresh in-memory store and the
    default console sink (offline) unless channels are supplied.

    ``retry_policy`` is accepted for API symmetry (the alert engine
    itself has no retry concept — retries live in the 19.8 outbox).
    """

    del retry_policy
    return AlertEngine(
        UserAlertConfig(),
        AlertStore(),
        channels if channels is not None else [ConsoleAlertSink()],
    )


__all__ = [
    "T",
    "build_alert_engine",
    "build_deterministic_lifecycle_cycle",
]