"""
Checkpoint 19.9 shared test fixtures (deterministic, offline).

Deterministic helper builders for the forward-validation test suites:

* scripted OHLCV candles at 15-minute intervals (completed vs future
  respecting an explicit reference instant);
* duck-typed 19.5 setup-quality, 19.6 lifecycle, 19.7 alert and 19.8
  reliability preview readers;
* deterministic fixed reference instants (a Friday 11:00 IST start
  sentinel and a weekend sentinel) reused across tests so sessions /
  observations / outcomes are reproducible.

No network, no credentials, no broker execution imports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Sequence

from engine.models.forward_validation import (
    ForwardObservation,
    ForwardObservationStatus,
    ForwardOutcome,
    build_outcome_id,
    build_observation_id,
)
from engine.models.ohlcv import OHLCVCandle
from engine.models.setup_lifecycle import (
    LifecycleObservationStatus,
    SetupLifecycleState,
)
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)

#: Friday 2026-09-04 11:00 IST = 05:30 UTC (trading-weekday OPEN).
FIXTURE_START = datetime(2026, 9, 4, 5, 30, tzinfo=UTC)

#: Saturday 2026-09-05 05:30 UTC (WEEKEND).
FIXTURE_WEEKEND = datetime(2026, 9, 5, 5, 30, tzinfo=UTC)


def t(index: int, base: datetime = FIXTURE_START) -> datetime:
    """Deterministic 15-minute step from ``base`` (fixture convention)."""
    return base + timedelta(minutes=15 * index)


def candle(
    index: int,
    *,
    close: float | None = None,
    base: datetime = FIXTURE_START,
    duration_minutes: int = 15,
    volume: int = 1000,
) -> OHLCVCandle:
    """One deterministic OHLCV candle at 15-minute step ``index``."""
    ts = t(index, base=base)
    close_v = close if close is not None else 100.0 + 2.0 * index
    return OHLCVCandle(
        timestamp=ts,
        open=close_v - 1.0,
        high=close_v + 2.0,
        low=close_v - 2.0,
        close=close_v,
        volume=volume,
    )


def scripted_candles(
    count: int,
    *,
    base: datetime = FIXTURE_START,
    closes: Sequence[float] | None = None,
) -> tuple[OHLCVCandle, ...]:
    """A deterministic, chronologically ordered candle series."""
    return tuple(
        candle(
            i,
            close=closes[i] if closes is not None and i < len(closes) else None,
            base=base,
        )
        for i in range(count)
    )


def forward_candles_after(
    observation_timestamp: datetime,
    count: int,
    *,
    base: datetime | None = None,
    horizon_start_offset: int = 1,
) -> tuple[OHLCVCandle, ...]:
    """Deterministic candles starting strictly after the observation."""
    anchor = base or observation_timestamp
    return tuple(
        candle(
            horizon_start_offset + i,
            base=anchor,
        )
        for i in range(count)
    )


# ==================================================================
# DUCK-TYPED UPSTREAM OUTPUTS (19.5 / 19.6 / 19.7)
# ==================================================================


def quality(
    instrument: str,
    *,
    direction: str = "BULLISH",
    setup_type: str = "TREND_CONTINUATION",
    classification: SetupQualityClassification = (
        SetupQualityClassification.EXCELLENT
    ),
    status: SetupQualityStatus = SetupQualityStatus.QUALIFIED,
    score: int = 88,
    mtf_alignment: str = "ALIGNED",
    mtf_completeness: str = "COMPLETE",
    primary_timeframe: str = "15m",
) -> Any:
    """A duck-typed 19.5 setup-quality result (build-friendly)."""
    return type(  # noqa: B009 - dynamic duck-typed upstream preview
        "_Quality",
        (),
        {
            "instrument": instrument,
            "primary_timeframe": primary_timeframe,
            "status": status,
            "classification": classification,
            "score": score,
            "setup_direction": direction,
            "setup_type": setup_type,
            "mtf_alignment": mtf_alignment,
            "mtf_completeness": mtf_completeness,
        },
    )()


def lifecycle(
    instrument: str,
    *,
    setup_id: str = "",
    lifecycle_id: str = "",
    state: SetupLifecycleState = SetupLifecycleState.CONFIRMED,
    observation_status: LifecycleObservationStatus = (
        LifecycleObservationStatus.OBSERVED
    ),
    observation_id: str = "",
    scan_cycle_id: str = "",
) -> Any:
    """A duck-typed 19.6 lifecycle observation result (build-friendly)."""
    return type(  # noqa: B009 - dynamic duck-typed upstream preview
        "_Lifecycle",
        (),
        {
            "instrument": instrument,
            "setup_id": setup_id or f"setup-{instrument.lower()}",
            "lifecycle_id": lifecycle_id or f"lc-{instrument.lower()}",
            "state": state,
            "observation_status": observation_status,
            "observation_id": observation_id or f"obs-{instrument.lower()}",
            "scan_cycle_id": scan_cycle_id or f"scan-{instrument.lower()}",
        },
    )()


def alert(
    instrument: str,
    *,
    setup_id: str = "",
    alert_id: str = "",
    observation_id: str = "",
    scan_cycle_id: str = "",
) -> Any:
    """A duck-typed 19.7 alert event (build-friendly)."""
    return type(  # noqa: B009 - dynamic duck-typed upstream preview
        "_Alert",
        (),
        {
            "instrument": instrument,
            "setup_id": setup_id or f"setup-{instrument.lower()}",
            "alert_id": alert_id or f"alert-{instrument.lower()}",
            "observation_id": observation_id or f"obs-{instrument.lower()}",
            "scan_cycle_id": scan_cycle_id or f"scan-{instrument.lower()}",
        },
    )()


def make_observation(
    session_id: str,
    *,
    scan_cycle_id: str,
    instrument: str,
    reference_now: datetime,
    policy_version: str = "p",
    direction: str = "BULLISH",
    setup_type: str = "TREND_CONTINUATION",
    setup_id: str = "",
    lifecycle_id: str = "",
    lifecycle_state: Any = None,
    lifecycle_observation_status: Any = None,
    quality_status: Any = None,
    quality_classification: Any = None,
    quality_score: int | None = 88,
    mtf_alignment: str = "ALIGNED",
    mtf_completeness: str = "COMPLETE",
    alert_state: str = "ALERTED",
    alert_id: str = "alert-x",
    reference_price: float | None = 100.0,
) -> ForwardObservation:
    """Deterministic :class:`ForwardObservation` for tests."""
    from engine.models.forward_validation import LifecycleResolution

    oid = build_observation_id(
        session_id=session_id,
        scan_cycle_id=scan_cycle_id,
        instrument=instrument,
        direction=direction,
        setup_type=setup_type,
        primary_timeframe="15m",
        observation_timestamp=reference_now,
        policy_version=policy_version,
    )
    return ForwardObservation(
        observation_id=oid,
        setup_id=setup_id or f"setup-{instrument.lower()}",
        lifecycle_id=lifecycle_id or f"lc-{instrument.lower()}",
        instrument=instrument,
        direction=direction,
        setup_type=setup_type,
        primary_timeframe="15m",
        observation_timestamp=reference_now,
        scan_cycle_id=scan_cycle_id,
        lifecycle_state=lifecycle_state or LifecycleResolution.CONFIRMED,
        lifecycle_observation_status=(
            lifecycle_observation_status
            or LifecycleObservationStatus.OBSERVED
        ),
        quality_status=quality_status or SetupQualityStatus.QUALIFIED,
        quality_classification=(
            quality_classification
            or SetupQualityClassification.EXCELLENT
        ),
        quality_score=quality_score,
        mtf_alignment=mtf_alignment,
        mtf_completeness=mtf_completeness,
        alert_state=alert_state,
        alert_id=alert_id,
        policy_versions=(("validation_policy_version", policy_version),),
        reference_price=reference_price,
        status=ForwardObservationStatus.RECORDED,
        reason="recorded",
    )


def make_outcome(
    observation: ForwardObservation,
    *,
    measurement_timestamp: datetime | None = None,
    horizon_bars: int = 5,
    policy_version: str = "p",
    availability: Any = None,
    forward_return: float | None = 0.02,
    outcome_direction: Any = None,
    window_complete: bool = True,
) -> ForwardOutcome:
    """Deterministic :class:`ForwardOutcome` for tests."""
    from engine.models.forward_validation import (
        OutcomeAvailability,
        OutcomeDirection,
    )

    measured = measurement_timestamp or (
        observation.observation_timestamp + timedelta(minutes=45)
    )
    oid = build_outcome_id(
        observation_id=observation.observation_id,
        measurement_timestamp=measured,
        horizon_bars=horizon_bars,
        policy_version=policy_version,
    )
    return ForwardOutcome(
        outcome_id=oid,
        observation_id=observation.observation_id,
        instrument=observation.instrument,
        direction=observation.direction,
        primary_timeframe=observation.primary_timeframe,
        observation_timestamp=observation.observation_timestamp,
        measurement_timestamp=measured,
        availability=availability or OutcomeAvailability.OUTCOME_AVAILABLE,
        forward_return=forward_return,
        max_favorable_movement=0.04 if forward_return is not None else None,
        max_adverse_movement=-0.01 if forward_return is not None else None,
        direction_consistent=(
            forward_return > 0 if forward_return is not None else None
        ),
        outcome_direction=(
            outcome_direction
            or (
                OutcomeDirection.FAVORABLE
                if forward_return is not None and forward_return > 0
                else OutcomeDirection.NOT_EVALUABLE
            )
        ),
        bars_available=horizon_bars,
        bars_used=horizon_bars if forward_return is not None else 0,
        horizon_bars=horizon_bars,
        window_complete=window_complete,
        reason="",
    )


__all__ = [
    "FIXTURE_START",
    "FIXTURE_WEEKEND",
    "alert",
    "candle",
    "forward_candles_after",
    "lifecycle",
    "make_observation",
    "make_outcome",
    "quality",
    "scripted_candles",
    "t",
]