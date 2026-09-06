"""
Validation / forward-testing models (Checkpoint 19.9).

These models implement the FINAL Checkpoint 19 layer: an honest,
descriptive FORWARD-VALIDATION framework that answers:

    "What did the complete 19.x system observe at time T (using only
     information available at T), and what did the market subsequently
     do AFTER T?"

The layer is DOWNSTREAM of the FROZEN 19.1-19.8 pipeline. It consumes
the deterministic OUTPUTS of the upstream layers (19.3 scan cycles,
19.4 MTF analysis, 19.5 setup-quality results, 19.6 lifecycle
observations, 19.7 alert events, 19.8 operational health) and records
them as IMMUTABLE, POINT-IN-TIME OBSERVATIONS. It NEVER re-derives
setup quality, lifecycle state, alert eligibility or ranking, and it
NEVER uses information unavailable at the observation timestamp.

DESIGN PRINCIPLES (documented in the Checkpoint 19.9 audit document):

* OBSERVATION vs OUTCOME are SEPARATE concerns. A
  :class:`ForwardObservation` records exactly what the system knew at
  ``T``. A :class:`ForwardOutcome` is computed ONLY from candles that
  closed strictly after ``T`` (within the configured horizon). The
  original observation is NEVER rewritten with future information
  (proven by byte-identical serialization tests).

* SMALLEST JUSTIFIED VOCABULARY. The observation carries the 19.5
  quality snapshot, the 19.4 MTF classification, the 19.6 lifecycle
  state, the 19.7 alert state and the 19.3 scan-cycle identity BY
  VALUE (immutable projections) — no duplicate analytical models are
  invented. The outcome carries a small set of descriptive,
  direction-aware measurements (see the outcome model).

* DETERMINISTIC IDENTITY. Every observation / outcome / session has a
  deterministic ``"<prefix>-" + sha256[:16]`` id over canonical
  content. No UUIDs, no wall-clock in identity, no object addresses.
  The same observation reprocessed yields the same id (idempotency);
  a different observation yields a different id.

* NO EXECUTION / DELIVERY / RELIABILITY / STRATEGY SEMANTICS. The
  models carry analytical + descriptive content only: no entry/stop/
  target, no position/quantity, no risk/reward, no order, no broker,
  no profitability claim. The user is always the final execution
  boundary.

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses.
* Optional fields use ``None`` so "unobserved" / "unavailable" is never
  silently a real value.
* ``__post_init__`` performs structural validation only; identity /
  classification logic lives in pure exported helpers.
* No wall-clock dependence: timestamps are explicit, caller-supplied,
  timezone-aware values.
* The model layer keeps NO import from ``dashboard`` (dependency
  direction: models <- config <- dashboard).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Sequence

from engine.models.setup_lifecycle import LifecycleObservationStatus
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)

#: Forward-observation id prefix (documented, deterministic).
FORWARD_OBSERVATION_ID_PREFIX = "fobs-"

#: Forward-outcome id prefix (documented, deterministic).
FORWARD_OUTCOME_ID_PREFIX = "fout-"

#: Forward-session id prefix (documented, deterministic).
FORWARD_SESSION_ID_PREFIX = "fsess-"

#: Forward-validation cycle id prefix (documented, deterministic).
FORWARD_CYCLE_ID_PREFIX = "fcycle-"

#: Forward-validation REPORT id prefix (documented, deterministic).
FORWARD_REPORT_ID_PREFIX = "frep-"

#: Forward-validation MODEL structural version (documented; audit doc).
FORWARD_VALIDATION_MODEL_VERSION = 1


def _sha256_prefix(payload: str, prefix: str) -> str:
    """Deterministic ``"<prefix>" + sha256[:16]`` over a canonical string."""

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def _canonical_name(name: str) -> str:
    """Canonicalize + validate a single instrument name."""

    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


# ==================================================================
# OBSERVATION STATE
# ==================================================================


class ForwardObservationStatus(Enum):
    """
    Per-observation record status (Checkpoint 19.9).

    This is a RECORD / INTEGRITY status, DELIBERATELY DISTINCT from the
    19.5 ``SetupQualityStatus`` (evaluation state), the 19.6
    ``LifecycleObservationStatus`` (lifecycle observation status), the
    19.7 ``AlertDeliveryStatus`` (delivery status) and the 19.8
    ``OperationalHealthState`` (operational health). It describes the
    integrity of the recorded forward observation itself.

    RECORDED
        The observation was accepted and persisted.

    DUPLICATE
        An observation with the same deterministic identity was already
        recorded (idempotency). The duplicate is surfaced explicitly,
        never silently dropped.

    OUT_OF_ORDER
        The observation timestamp is strictly older than the session's
        latest recorded observation timestamp (chronological integrity
        violation). Explicitly rejected or recorded per configuration.

    DATA_UNAVAILABLE
        The observation could not be built because the upstream
        analytical inputs were unavailable (no usable setup-quality /
        MTF / lifecycle / alert state). Never a fabricated observation.
    """

    RECORDED = "RECORDED"
    DUPLICATE = "DUPLICATE"
    OUT_OF_ORDER = "OUT_OF_ORDER"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


class OutcomeAvailability(Enum):
    """
    Whether a forward outcome has been measured for an observation.

    OUTCOME_AVAILABLE
        A forward outcome was computed (the forward window had at least
        one completed candle strictly after ``T`` within the horizon).

    OUTCOME_PARTIAL
        A forward outcome was computed over a PARTIAL window (fewer
        forward candles than the configured horizon were available; the
        window is not yet complete). The outcome is descriptive of the
        available window only.

    OUTCOME_UNAVAILABLE
        No forward outcome could be computed yet (no completed candle
        strictly after ``T`` is available at measurement time).

    WINDOW_INCOMPLETE
        The configured horizon has not yet elapsed (fewer forward
        candles than the horizon are available at measurement time); a
        later measurement may produce a full-window outcome. This is
        the "waiting for the window to mature" state.
    """

    OUTCOME_AVAILABLE = "OUTCOME_AVAILABLE"
    OUTCOME_PARTIAL = "OUTCOME_PARTIAL"
    OUTCOME_UNAVAILABLE = "OUTCOME_UNAVAILABLE"
    WINDOW_INCOMPLETE = "WINDOW_INCOMPLETE"


class OutcomeDirection(Enum):
    """
    Descriptive direction-aware outcome classification (Checkpoint 19.9).

    The outcome classification describes whether the measured forward
    price movement was CONSISTENT with the observation's analytical
    direction. It is a DESCRIPTIVE measurement — never a win/loss, never
    a profitability claim, never a prediction.

    FAVORABLE
        The measured movement was consistent with the observation's
        direction (e.g. a BULLISH observation whose forward price rose).

    UNFAVORABLE
        The measured movement was contrary to the observation's
        direction.

    NEUTRAL
        The measured movement was neither clearly favorable nor clearly
        unfavorable (e.g. the forward return was exactly zero, or the
        movement magnitude was below the configured tolerance).

    NOT_EVALUABLE
        No directional measurement could be made (no usable reference
        price / no forward window / non-directional observation).
    """

    FAVORABLE = "FAVORABLE"
    UNFAVORABLE = "UNFAVORABLE"
    NEUTRAL = "NEUTRAL"
    NOT_EVALUABLE = "NOT_EVALUABLE"


class LifecycleResolution(Enum):
    """
    Descriptive lifecycle resolution of an observation's setup
    (Checkpoint 19.9).

    This reuses the FROZEN 19.6 lifecycle vocabulary (DETECTED /
    CONFIRMED / INVALIDATED / EXPIRED) plus the explicit
    DATA_UNAVAILABLE state. It is a lifecycle-state snapshot, never a
    trade outcome.

    DETECTED / CONFIRMED / INVALIDATED / EXPIRED
        The 19.6 lifecycle state at the observation instant.

    DATA_UNAVAILABLE
        No lifecycle state was available at the observation instant.
    """

    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"


# ==================================================================
# OBSERVATION
# ==================================================================


@dataclass(frozen=True, slots=True)
class ForwardObservation:
    """
    ONE immutable, point-in-time forward observation (Checkpoint 19.9).

    The observation records exactly what the system knew at ``T``:
    the 19.5 setup-quality snapshot, the 19.4 MTF classification, the
    19.6 lifecycle state, the 19.7 alert state, the 19.3 scan-cycle
    identity and the configuration / policy versions that produced it.

    The observation is NEVER rewritten with future information. A
    forward outcome is a SEPARATE record (see :class:`ForwardOutcome`).

    Attributes:

    observation_id
        Deterministic ``"fobs-" + sha256[:16]`` identity.

    setup_id
        The FROZEN 19.6 deterministic setup identity (``""`` when no
        directional setup candidate was present).

    lifecycle_id
        The FROZEN 19.6 lifecycle id (``""`` when none).

    instrument
        Canonical instrument name.

    direction
        The 19.5 analytical setup direction (``"BULLISH"`` /
        ``"BEARISH"`` / ``""`` when none).

    setup_type
        The 19.5 setup type (reused 11R taxonomy; ``""`` when none).

    primary_timeframe
        The primary (setup / measurement) timeframe label.

    observation_timestamp
        The analytical observation instant ``T`` (explicit, aware).

    scan_cycle_id
        The FROZEN 19.3 scan-cycle identity that produced the
        observation (``""`` when not applicable).

    lifecycle_state
        The 19.6 lifecycle state at ``T`` (reused enum).

    lifecycle_observation_status
        The 19.6 lifecycle observation status at ``T`` (reused enum).

    quality_status / quality_classification / quality_score
        The 19.5 setup-quality snapshot at ``T`` (reused enums / int).

    mtf_alignment / mtf_completeness
        The 19.4 MTF classification at ``T`` (reused enums).

    alert_state / alert_id
        The 19.7 alert state at ``T`` (``"ALERTED"`` / ``"SUPPRESSED"``
        / ``""``) and the alert id (``""`` when none).

    policy_versions
        Deterministic policy versions recorded at ``T`` (validation /
        setup-quality / lifecycle / alert / reliability / universe /
        measurement window). A later config change can NEVER silently
        reinterpret this observation.

    reference_price
        The observation-time market price anchor (the close of the
        latest completed primary candle at/before ``T``). ``None`` when
        unavailable — never fabricated.

    status
        :class:`ForwardObservationStatus` record / integrity status.

    reason
        Descriptive reason (``""`` when none).
    """

    observation_id: str
    setup_id: str
    lifecycle_id: str
    instrument: str
    direction: str
    setup_type: str
    primary_timeframe: str
    observation_timestamp: datetime
    scan_cycle_id: str
    lifecycle_state: LifecycleResolution
    lifecycle_observation_status: LifecycleObservationStatus
    quality_status: SetupQualityStatus
    quality_classification: SetupQualityClassification
    quality_score: int | None
    mtf_alignment: str
    mtf_completeness: str
    alert_state: str
    alert_id: str
    policy_versions: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    reference_price: float | None = None
    status: ForwardObservationStatus = ForwardObservationStatus.RECORDED
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.observation_id.startswith(FORWARD_OBSERVATION_ID_PREFIX):
            raise ValueError(
                "observation_id must start with "
                f"{FORWARD_OBSERVATION_ID_PREFIX!r}.",
            )
        if not self.instrument:
            raise ValueError("instrument must not be empty.")
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if self.quality_score is not None:
            if isinstance(self.quality_score, bool) or not isinstance(
                self.quality_score, int,
            ):
                raise TypeError("quality_score must be an int or None.")
        if not isinstance(self.policy_versions, tuple):
            raise TypeError("policy_versions must be a tuple of (name, value) pairs.")
        for pair in self.policy_versions:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("policy_versions entries must be (name, value) pairs.")
            if not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise TypeError("policy_versions name and value must be str.")

    @property
    def is_directional(self) -> bool:
        """True when the observation carries a directional setup."""
        return self.direction in ("BULLISH", "BEARISH")

    @property
    def has_reference_price(self) -> bool:
        return self.reference_price is not None

    @property
    def is_qualified(self) -> bool:
        """True when the 19.5 quality snapshot was QUALIFIED."""
        return self.quality_status is SetupQualityStatus.QUALIFIED

    @property
    def is_alerted(self) -> bool:
        return self.alert_state == "ALERTED"


# ==================================================================
# OUTCOME
# ==================================================================


@dataclass(frozen=True, slots=True)
class ForwardOutcome:
    """
    ONE immutable, forward-only outcome measurement (Checkpoint 19.9).

    The outcome is computed ONLY from completed primary-timeframe
    candles that closed STRICTLY AFTER the observation timestamp ``T``
    and within the configured horizon. It NEVER uses information
    available at or before ``T``, and it NEVER rewrites the observation.

    Attributes:

    outcome_id
        Deterministic ``"fout-" + sha256[:16]`` identity.

    observation_id
        The observation this outcome measures.

    instrument / direction / primary_timeframe
        Projected from the observation (by value).

    observation_timestamp
        The observation instant ``T`` (the reference anchor).

    measurement_timestamp
        The instant the outcome was measured (explicit, aware; the
        analytical anchor for the forward window).

    availability
        :class:`OutcomeAvailability` (AVAILABLE / PARTIAL /
        UNAVAILABLE / WINDOW_INCOMPLETE).

    forward_return
        Direction-neutral fractional price return over the measured
        forward window: ``(endpoint_close - reference_price) /
        reference_price``. ``None`` when not measurable.

    max_favorable_movement / max_adverse_movement
        Direction-aware maximum favorable / adverse fractional price
        excursion over the measured window (relative to the reference
        price), measured from the candle highs/lows. ``None`` when not
        measurable.

    direction_consistent
        ``True`` when the measured movement was consistent with the
        observation's analytical direction; ``False`` when contrary;
        ``None`` when not evaluable (non-directional / no window).

    outcome_direction
        :class:`OutcomeDirection` classification (FAVORABLE /
        UNFAVORABLE / NEUTRAL / NOT_EVALUABLE).

    bars_available / bars_used / horizon_bars
        Forward candles available at measurement time / used for the
        measurement / the configured horizon.

    window_complete
        ``True`` when the full configured horizon was available.

    reason
        Descriptive reason (``""`` when none).
    """

    outcome_id: str
    observation_id: str
    instrument: str
    direction: str
    primary_timeframe: str
    observation_timestamp: datetime
    measurement_timestamp: datetime
    availability: OutcomeAvailability
    forward_return: float | None = None
    max_favorable_movement: float | None = None
    max_adverse_movement: float | None = None
    direction_consistent: bool | None = None
    outcome_direction: OutcomeDirection = OutcomeDirection.NOT_EVALUABLE
    bars_available: int = 0
    bars_used: int = 0
    horizon_bars: int = 0
    window_complete: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.outcome_id.startswith(FORWARD_OUTCOME_ID_PREFIX):
            raise ValueError(
                "outcome_id must start with "
                f"{FORWARD_OUTCOME_ID_PREFIX!r}.",
            )
        if not self.observation_id.startswith(FORWARD_OBSERVATION_ID_PREFIX):
            raise ValueError(
                "observation_id must start with "
                f"{FORWARD_OBSERVATION_ID_PREFIX!r}.",
            )
        _require_aware(self.observation_timestamp, "observation_timestamp")
        _require_aware(self.measurement_timestamp, "measurement_timestamp")
        for name in ("bars_available", "bars_used", "horizon_bars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, not bool.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.availability in (
            OutcomeAvailability.OUTCOME_AVAILABLE,
            OutcomeAvailability.OUTCOME_PARTIAL,
        ):
            if self.bars_used < 1:
                raise ValueError(
                    "an available outcome must use at least one forward candle.",
                )
            if self.forward_return is None:
                raise ValueError(
                    "an available outcome must carry a forward return.",
                )
        elif self.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE:
            if self.bars_used != 0:
                raise ValueError(
                    "an unavailable outcome must use zero forward candles.",
                )
        # WINDOW_INCOMPLETE may carry a partial measurement (bars_used >= 1).


# ==================================================================
# SESSION
# ==================================================================


@dataclass(frozen=True, slots=True)
class ForwardSession:
    """
    ONE immutable, auditable forward-validation session (Checkpoint 19.9).

    A session bundles the configuration / policy versions that produced
    its observations, the universe it scanned, the provider + timeframes,
    the start / end instants and the running counts. It is
    reproducible / auditable: a later configuration change can NEVER
    silently reinterpret a session's observations (the session records
    the exact policy versions).

    Attributes:

    session_id
        Deterministic ``"fsess-" + sha256[:16]`` identity.

    provider / timeframes / primary_timeframe / universe
        The configuration that produced the session.

    universe_version
        The 19.1 universe manifest version (``""`` when not supplied).

    policy_versions
        Deterministic policy versions recorded at session start.

    started_at
        The session start instant (explicit, aware).

    ended_at
        The session end instant (``None`` while open).

    status
        ``"OPEN"`` / ``"CLOSED"``.

    counts
        Running observation / outcome / alert / failure counts.

    label / metadata
        Identification for the run.
    """

    session_id: str
    provider: str
    timeframes: tuple[str, ...]
    primary_timeframe: str
    universe: tuple[str, ...]
    universe_version: str
    policy_versions: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    ended_at: datetime | None = None
    status: str = "OPEN"
    counts: "ForwardSessionCounts" = field(default_factory=lambda: ForwardSessionCounts())
    label: str = ""
    metadata: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.session_id.startswith(FORWARD_SESSION_ID_PREFIX):
            raise ValueError(
                "session_id must start with "
                f"{FORWARD_SESSION_ID_PREFIX!r}.",
            )
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ValueError("provider must be a non-empty str.")
        if not isinstance(self.timeframes, tuple) or not self.timeframes:
            raise ValueError("timeframes must be a non-empty tuple.")
        if not self.primary_timeframe:
            raise ValueError("primary_timeframe must not be empty.")
        if not isinstance(self.universe, tuple):
            raise TypeError("universe must be a tuple of str.")
        for name in self.universe:
            if not isinstance(name, str) or not name.strip():
                raise TypeError("universe entries must be non-empty str.")
        _require_aware(self.started_at, "started_at")
        if self.ended_at is not None:
            _require_aware(self.ended_at, "ended_at")
        if self.status not in ("OPEN", "CLOSED"):
            raise ValueError("status must be 'OPEN' or 'CLOSED'.")
        if self.ended_at is not None and self.status != "CLOSED":
            raise ValueError("a session with an ended_at must be CLOSED.")
        if self.status == "CLOSED" and self.ended_at is None:
            raise ValueError("a CLOSED session must carry an ended_at.")
        if not isinstance(self.policy_versions, tuple):
            raise TypeError("policy_versions must be a tuple of (name, value) pairs.")
        for pair in self.policy_versions:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("policy_versions entries must be (name, value) pairs.")
            if not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise TypeError("policy_versions name and value must be str.")

    @property
    def is_open(self) -> bool:
        return self.status == "OPEN"


@dataclass(frozen=True, slots=True)
class ForwardSessionCounts:
    """
    Running forward-validation counts (deterministic; invariants
    enforced by the engine, not here).

    Attributes:

    observations / outcomes / alerts / alerts_delivered /
    alerts_failed / alerts_suppressed
        Running totals.

    failures
        Running per-category operational failure counts (reused 19.8
        vocabulary, by name).

    retries / timeouts / recoveries
        Running reliability counters.
    """

    observations: int = 0
    outcomes: int = 0
    alerts: int = 0
    alerts_delivered: int = 0
    alerts_failed: int = 0
    alerts_suppressed: int = 0
    failures: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    retries: int = 0
    timeouts: int = 0
    recoveries: int = 0

    def __post_init__(self) -> None:
        for name in (
            "observations",
            "outcomes",
            "alerts",
            "alerts_delivered",
            "alerts_failed",
            "alerts_suppressed",
            "retries",
            "timeouts",
            "recoveries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, not bool.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if not isinstance(self.failures, tuple):
            raise TypeError("failures must be a tuple of (name, count) pairs.")
        for pair in self.failures:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("failures entries must be (name, count) pairs.")
            if not isinstance(pair[0], str) or not isinstance(pair[1], int):
                raise TypeError("failures name/count must be str/int.")


# ==================================================================
# CYCLE + REPORT
# ==================================================================


@dataclass(frozen=True, slots=True)
class ForwardCycleResult:
    """
    ONE forward-validation processing cycle (deterministic).

    A cycle consumes one 19.3 scan-cycle identity + the 19.4/19.5/19.6/
    19.7/19.8 outputs produced at that instant and records the
    resulting observations / outcomes / alert counts.
    """

    cycle_id: str
    session_id: str
    scan_cycle_id: str
    reference_now: datetime
    instruments: tuple[str, ...] = field(default_factory=tuple)
    observations: tuple[ForwardObservation, ...] = field(default_factory=tuple)
    outcomes: tuple[ForwardOutcome, ...] = field(default_factory=tuple)
    alerts: int = 0
    alerts_delivered: int = 0
    alerts_failed: int = 0
    alerts_suppressed: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        if not self.cycle_id.startswith(FORWARD_CYCLE_ID_PREFIX):
            raise ValueError(
                "cycle_id must start with "
                f"{FORWARD_CYCLE_ID_PREFIX!r}.",
            )
        _require_aware(self.reference_now, "reference_now")


@dataclass(frozen=True, slots=True)
class ForwardValidationReport:
    """
    The deterministic forward-validation report (Checkpoint 19.9).

    Aggregates one or more sessions into the descriptive engineering
    assessment. All counts are derived from the recorded observations /
    outcomes / alerts / operational state — nothing fabricated.
    """

    report_id: str
    session_ids: tuple[str, ...] = field(default_factory=tuple)
    observations: tuple[ForwardObservation, ...] = field(default_factory=tuple)
    outcomes: tuple[ForwardOutcome, ...] = field(default_factory=tuple)
    setup_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    classification_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    direction_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    setup_type_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    mtf_alignment_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    lifecycle_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    alert_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    outcome_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    outcome_direction_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    outcome_consistency_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    failure_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    universe_instrument_count: int = 0
    instruments_attempted: int = 0
    instruments_with_usable_data: int = 0
    unsupported_instruments: int = 0
    stale_instruments: int = 0
    provider_failures: int = 0
    successful_scans: int = 0
    incomplete_mtf_analyses: int = 0
    setups_detected: int = 0
    alerts_generated: int = 0
    alerts_delivered: int = 0
    alerts_suppressed: int = 0
    sample_size_note: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        if not self.report_id.startswith(FORWARD_REPORT_ID_PREFIX):
            raise ValueError(
                "report_id must start with "
                f"{FORWARD_REPORT_ID_PREFIX!r}.",
            )
        if not isinstance(self.session_ids, tuple):
            raise TypeError("session_ids must be a tuple of str.")
        for sid in self.session_ids:
            if not isinstance(sid, str) or not sid:
                raise TypeError("session_ids entries must be non-empty str.")


# ==================================================================
# IDENTITY HELPERS (pure, exported)
# ==================================================================


def build_observation_id(
    *,
    session_id: str,
    scan_cycle_id: str,
    instrument: str,
    direction: str,
    setup_type: str,
    primary_timeframe: str,
    observation_timestamp: datetime,
    policy_version: str,
) -> str:
    """
    Deterministic forward-observation identity.

    The identity is a function of the session + scan-cycle identity +
    the canonical analytical content (instrument / direction / setup
    type / primary timeframe / observation instant) + the validation
    policy version. The same observation reprocessed yields the same id
    (idempotency); a different observation yields a different id.
    """

    if not session_id or not scan_cycle_id:
        raise ValueError("session_id and scan_cycle_id must be non-empty.")
    _require_aware(observation_timestamp, "observation_timestamp")
    payload = "|".join(
        (
            "session", session_id,
            "scan", scan_cycle_id,
            "instrument", _canonical_name(instrument),
            "direction", direction or "",
            "setup_type", setup_type or "",
            "tf", primary_timeframe or "",
            "at", observation_timestamp.astimezone(UTC).isoformat(),
            "policy", policy_version or "",
        ),
    )
    return _sha256_prefix(payload, FORWARD_OBSERVATION_ID_PREFIX)


def build_outcome_id(
    *,
    observation_id: str,
    measurement_timestamp: datetime,
    horizon_bars: int,
    policy_version: str,
) -> str:
    """
    Deterministic forward-outcome identity.

    A function of the observation id + measurement instant + horizon +
    policy version. Measuring the same observation at the same instant
    under the same policy yields the same outcome id.
    """

    if not observation_id:
        raise ValueError("observation_id must be non-empty.")
    _require_aware(measurement_timestamp, "measurement_timestamp")
    payload = "|".join(
        (
            "observation", observation_id,
            "measured_at", measurement_timestamp.astimezone(UTC).isoformat(),
            "horizon", str(horizon_bars),
            "policy", policy_version or "",
        ),
    )
    return _sha256_prefix(payload, FORWARD_OUTCOME_ID_PREFIX)


def build_session_id(
    *,
    provider: str,
    timeframes: tuple[str, ...],
    universe: tuple[str, ...],
    started_at: datetime,
    policy_version: str,
    label: str = "",
) -> str:
    """
    Deterministic forward-session identity.

    A function of the provider + canonical timeframes + canonical
    universe + session start instant + validation policy version +
    label. Two sessions with identical configuration but different start
    instants are distinct sessions.
    """

    _require_aware(started_at, "started_at")
    payload = "|".join(
        (
            "provider", provider,
            "timeframes", ",".join(timeframes),
            "universe", "|".join(sorted(set(_canonical_name(n) for n in universe))),
            "started", started_at.astimezone(UTC).isoformat(),
            "policy", policy_version,
            "label", label,
        ),
    )
    return _sha256_prefix(payload, FORWARD_SESSION_ID_PREFIX)


def build_forward_cycle_id(
    *,
    session_id: str,
    scan_cycle_id: str,
    reference_now: datetime,
    policy_version: str,
) -> str:
    """Deterministic forward-validation cycle identity."""

    _require_aware(reference_now, "reference_now")
    payload = "|".join(
        (
            "session", session_id,
            "scan", scan_cycle_id,
            "at", reference_now.astimezone(UTC).isoformat(),
            "policy", policy_version,
        ),
    )
    return _sha256_prefix(payload, FORWARD_CYCLE_ID_PREFIX)


def build_report_id(
    *,
    session_ids: Sequence[str],
    policy_version: str,
    label: str = "",
) -> str:
    """Deterministic forward-validation report identity."""

    payload = "|".join(
        (
            "sessions", "|".join(sorted(set(session_ids))),
            "policy", policy_version,
            "label", label,
        ),
    )
    return _sha256_prefix(payload, FORWARD_REPORT_ID_PREFIX)


# ==================================================================
# CLASSIFICATION HELPERS (pure, exported)
# ==================================================================


def classify_outcome_direction(
    *,
    direction: str,
    forward_return: float | None,
    max_favorable: float | None,
    max_adverse: float | None,
    tolerance: float = 0.0,
) -> OutcomeDirection:
    """
    Deterministic descriptive outcome-direction classification.

    A BULLISH observation whose measured movement is positive (forward
    return > tolerance OR max favorable > tolerance) is FAVORABLE; a
    negative movement is UNFAVORABLE; a zero / sub-tolerance movement is
    NEUTRAL. The SHORT mirror applies for BEARISH. Non-directional or
    unmeasurable inputs -> NOT_EVALUABLE. This is DESCRIPTIVE ONLY —
    never a win/loss, never a profitability claim.
    """

    if direction not in ("BULLISH", "BEARISH"):
        return OutcomeDirection.NOT_EVALUABLE
    if forward_return is None and max_favorable is None and max_adverse is None:
        return OutcomeDirection.NOT_EVALUABLE

    # Net signed movement relative to the analytical direction.
    net = forward_return if forward_return is not None else 0.0
    # Direction-aware excursion (favorable positive, adverse negative).
    fav = max_favorable if max_favorable is not None else 0.0
    adv = max_adverse if max_adverse is not None else 0.0
    if direction == "BEARISH":
        net = -net
        fav, adv = adv, fav  # for a SHORT, downward movement is favorable

    # The NET forward return is the primary measurement; excursions are
    # tie-breakers only when the net movement is effectively flat.
    if net > tolerance:
        return OutcomeDirection.FAVORABLE
    if net < -tolerance:
        return OutcomeDirection.UNFAVORABLE
    if fav > tolerance and adv > -tolerance:
        return OutcomeDirection.FAVORABLE
    if adv > tolerance and fav <= tolerance:
        return OutcomeDirection.UNFAVORABLE
    return OutcomeDirection.NEUTRAL


__all__ = [
    "FORWARD_CYCLE_ID_PREFIX",
    "FORWARD_OBSERVATION_ID_PREFIX",
    "FORWARD_OUTCOME_ID_PREFIX",
    "FORWARD_REPORT_ID_PREFIX",
    "FORWARD_SESSION_ID_PREFIX",
    "FORWARD_VALIDATION_MODEL_VERSION",
    "ForwardCycleResult",
    "ForwardObservation",
    "ForwardObservationStatus",
    "ForwardOutcome",
    "ForwardSession",
    "ForwardSessionCounts",
    "ForwardValidationReport",
    "LifecycleResolution",
    "OutcomeAvailability",
    "OutcomeDirection",
    "build_forward_cycle_id",
    "build_observation_id",
    "build_outcome_id",
    "build_report_id",
    "build_session_id",
    "classify_outcome_direction",
]