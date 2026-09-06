"""
Operational reliability / recovery / observability models (Checkpoint
19.8).

These models describe the OPERATIONAL layer that wraps the FROZEN
19.1–19.7 analytical pipeline. They are OPERATIONAL / RELIABILITY models
and are DELIBERATELY distinct from the analytical vocabularies:

* analytical statuses (``SetupQualityStatus``, ``MarketScanStatus``,
  ``LifecycleObservationStatus``, ``AlertDeliveryStatus``) describe what
  the ENGINE observed about the MARKET / SETUPS / LIFECYCLE / ALERTS;
* the operational vocabularies here (``ReliabilityFailureCategory``,
  ``OperationalHealthState``, ``HeartbeatState``, ``HealthStatus``)
  describe how RELIABLY the system itself is operating.

The distinction is MANDATORY and enforced by the wording of every
member: an analytical ``DATA_UNAVAILABLE`` symbol is NOT a
``PROCESS_FAILURE``; a provider timeout is NOT an analytical signal; a
delivery failure NEVER mutates a lifecycle. The failure taxonomy is the
smallest explicit vocabulary that provides useful diagnosis:

    PROVIDER_TIMEOUT / PROVIDER_UNAVAILABLE  (data-provider failures)
    INVALID_RESPONSE         (malformed / unvalidatable provider data)
    DELIVERY_FAILURE         (alert delivery could not succeed)
    CYCLE_FAILURE            (a scan cycle could not produce results)
    HEARTBEAT_STALE          (the worker stopped refreshing its beacon)
    STALLED_SCANNER          (alive but no completed cycle for too long)
    RECOVERY_FAILURE         (a recovery attempt left the system worse)
    CONFIGURATION_FAILURE    (invalid configuration, fail closed)
    PROCESS_FAILURE          (an unexpected exception escaped a boundary)
    UNKNOWN                  (not otherwise classified)

Reliability failures stay distinct from analytical states; no category
name overlaps any analytical enum member name.

Health is DERIVED from observable operational facts (cycles, provider
results, delivery results, heartbeats) — NEVER from setup quality.

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses; ``__post_init__`` structural validation.
* Optional fields use ``None`` so "unobserved" is never a real value.
* Deterministic ordering everywhere; no unordered iteration.
* No wall-clock dependence: all timestamps are explicit, caller-supplied
  and timezone-aware; operational metadata may use the injected
  operational clock, but analytical timestamps always come from the
  upstream event/data timestamps.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


def _sha256_prefix(payload: str, prefix: str) -> str:
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


def _canonical_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


# =====================================================================
# FAILURE / HEALTH TAXONOMY
# =====================================================================


class ReliabilityFailureCategory(Enum):
    """
    Explicit operational failure vocabulary (Checkpoint 19.8).

    The categories are intentionally DISTINCT from every analytical
    state (a failure here describes the SYSTEM's operation, never the
    market / setup / lifecycle / alert semantics). The member names are
    the canonical retryable category identities (the retry policy names
    them by ``.name``).

    PROVIDER_TIMEOUT
        A provider operation exceeded its bounded time budget. The
        failure is TRANSIENT and retryable under the retry policy; it is
        never an analytical signal.

    PROVIDER_UNAVAILABLE
        The provider is temporarily unavailable (network / transport /
        not-ready). Retryable when so configured; distinct from
        UNSUPPORTED_INSTRUMENT (which is explicit and NOT retried).

    INVALID_RESPONSE
        The provider returned data that could not be validated
        (malformed / impossible OHLC / all-rejected). DETERMINISTIC;
        NEVER blindly retried (retrying cannot repair malformed data).

    DELIVERY_FAILURE
        Alert delivery could not succeed through any configured channel.
        The alert event and its identity are PRESERVED and retried
        according to policy; the lifecycle state is NEVER mutated.

    CYCLE_FAILURE
        A scan cycle could not produce per-symbol results (the whole
        universe assessment failed). Operational; distinct from a
        per-symbol failure (which is isolated and recorded on the cycle
        without a CYCLE_FAILURE).

    HEARTBEAT_STALE
        The worker's heartbeat is older than the configured quiet
        threshold — the process may be alive but is not proving
        progress. Never conflated with an analytical state.

    STALLED_SCANNER
        No scan cycle has COMPLETED within the configured
        ``max_since_cycle_seconds`` even though the process (heartbeat)
        may technically be alive. "Alive" is not "healthy".

    RECOVERY_FAILURE
        A recovery attempt was required (startup / after a restart) but
        the persisted state was unusable and recovery could not
        reconstruct safe operational state.

    CONFIGURATION_FAILURE
        Invalid reliability configuration (fail closed at construction;
        an invalid retry count / timeout / backoff / health threshold /
        persistence location raises before anything starts).

    PROCESS_FAILURE
        An unexpected exception escaped the expected operational
        boundary (defensive). Visible and typed, never silently
        swallowed.

    UNKNOWN
        Not otherwise classified (an unclassified operational anomaly;
        never presented as a success).
    """

    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    INVALID_RESPONSE = "INVALID_RESPONSE"
    DELIVERY_FAILURE = "DELIVERY_FAILURE"
    CYCLE_FAILURE = "CYCLE_FAILURE"
    HEARTBEAT_STALE = "HEARTBEAT_STALE"
    STALLED_SCANNER = "STALLED_SCANNER"
    RECOVERY_FAILURE = "RECOVERY_FAILURE"
    CONFIGURATION_FAILURE = "CONFIGURATION_FAILURE"
    PROCESS_FAILURE = "PROCESS_FAILURE"
    UNKNOWN = "UNKNOWN"


#: The fixed, transient, RETRYABLE category names (the default retry
#: policy retries exactly these; anything else is deterministic and
#: never blindly retried).
_RETRYABLE_CATEGORY_NAMES: frozenset[str] = frozenset({
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "DELIVERY_FAILURE",
})


class OperationalHealthState(Enum):
    """
    Deterministic OPERATIONAL health vocabulary (Checkpoint 19.8).

    Health describes how RELIABLY the system is operating — it is NEVER
    a market / setup verdict. It is always distinct from the analytical
    vocabularies (CONFIRMED / HIGH / ALIGNED describe setups; HEALTHY /
    DEGRADED / RECOVERING / FAILED describe the system).

    HEALTHY
        Every operational domain is functioning within its thresholds:
        cycles completing, provider path healthy, deliveries succeeding,
        heartbeat fresh, no outstanding recovery.

    DEGRADED
        Operational failures have been observed (a provider failing,
        deliveries failing, heartbeats stale) but the system is still
        performing its core observation and is within the degradation
        threshold (not yet FAILED). A degraded system remains
        analytically correct — it is just operating with a known
        reliability caveat.

    RECOVERING
        A prior degraded/failed condition is being actively recovered
        (consecutive clean successes moving the system back to HEALTHY
        under the recovery threshold). Recovery is observable and the
        state is explicit — recovery is never hidden.

    FAILED
        The degradation threshold has been exceeded (repeated
        operational failures, stale heartbeat, stalled scanner) or a
        startup recovery could not safely reconstruct operational state.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"

    @property
    def is_healthy(self) -> bool:
        return self is OperationalHealthState.HEALTHY


class HeartbeatState(Enum):
    """
    Deterministic heartbeat classification (operator-process liveness,
    not system health).

    FRESH
        The heartbeat was refreshed recently (within the configured
        quiet threshold) — the worker is alive AND is refreshing its
        beacon.

    STALE
        The heartbeat is older than the configured quiet threshold —
        the worker may be dead/hung even if a PID file exists. Never a
        naive "PID exists = healthy" proxy.

    NONE
        No heartbeat was ever recorded (worker never started / no data).
    """

    FRESH = "FRESH"
    STALE = "STALE"
    NONE = "NONE"


class HealthStatus(Enum):
    """
    A single operational-domain health verdict (provider / scanner /
    alert-delivery), derived from observable operational facts.

    HEALTHY / DEGRADED / FAILED mirror :class:`OperationalHealthState`
    at the per-domain granularity; RECOVERING is reserved for the
    aggregate state. A domain with insufficient observation history is
    NEVER silently healthy or failed — it is UNKNOWN.
    """

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


# =====================================================================
# DETERMINISTIC HELPERS
# =====================================================================


def classify_heartbeat(
    last_heartbeat_at: datetime | None,
    reference_now: datetime,
    max_quiet_seconds: float,
) -> HeartbeatState:
    """
    Deterministic heartbeat classification.

    * no heartbeat -> ``NONE``;
    * naive timestamps -> ``STALE`` (cannot be verified, never FRESH);
    * heartbeat older than ``max_quiet_seconds`` -> ``STALE``;
    * otherwise -> ``FRESH``.
    """

    if last_heartbeat_at is None:
        return HeartbeatState.NONE
    try:
        _require_aware(last_heartbeat_at, "last_heartbeat_at")
        _require_aware(reference_now, "reference_now")
    except (TypeError, ValueError):
        return HeartbeatState.STALE
    age = (reference_now - last_heartbeat_at).total_seconds()
    if age < 0:
        # Future-dated heartbeat: cannot be verified -> STALE.
        return HeartbeatState.STALE
    if age > max_quiet_seconds:
        return HeartbeatState.STALE
    return HeartbeatState.FRESH


# =====================================================================
# MODELS
# =====================================================================


@dataclass(frozen=True, slots=True)
class OperationalEventRecord:
    """
    One immutable operational event (failure / retry / recovery /
    timeout / cycle / delivery observation).

    ``event_id`` is a deterministic id (``"opevt-" + sha256[:16]``); the
    category may be ``None`` for a clean success observation (a success
    is not a failure). ``duration_seconds`` is the operation duration
    (for timeout detection), or ``None`` when not measured.
    """

    event_id: str
    timestamp: datetime
    component: str
    category: ReliabilityFailureCategory | None = None
    cycle_id: str = ""
    setup_id: str = ""
    alert_id: str = ""
    symbol: str = ""
    detail: str = ""
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        if not self.event_id.strip():
            raise ValueError("event_id must not be empty.")
        if not self.component.strip():
            raise ValueError("component must not be empty.")
        if self.category is not None and not isinstance(
            self.category, ReliabilityFailureCategory,
        ):
            raise TypeError("category must be a ReliabilityFailureCategory or None.")
        if self.symbol:
            object.__setattr__(self, "symbol", _canonical_name(self.symbol))
        if self.duration_seconds is not None:
            if isinstance(self.duration_seconds, bool) or not isinstance(
                self.duration_seconds, (int, float),
            ):
                raise TypeError("duration_seconds must be a number or None.")
            if float(self.duration_seconds) < 0:
                raise ValueError("duration_seconds must be non-negative.")


@dataclass(frozen=True, slots=True)
class RetryRecord:
    """
    One deterministic retry attempt / outcome.

    ``attempt_index`` is zero-based (0 = the first attempt of a retry
    sequence). ``retryable`` records whether the retry policy decided a
    retry was permitted; ``delay_seconds`` is the delay that preceded
    this attempt (0 for the first). ``alert_id`` is preserved across
    retries so a retried alert keeps its ORIGINAL identity.
    """

    retry_id: str
    timestamp: datetime
    category: ReliabilityFailureCategory
    attempt_index: int
    retryable: bool
    delay_seconds: float = 0.0
    instrument: str = ""
    cycle_id: str = ""
    alert_id: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        if not self.retry_id.strip():
            raise ValueError("retry_id must not be empty.")
        if not isinstance(self.category, ReliabilityFailureCategory):
            raise TypeError("category must be a ReliabilityFailureCategory.")
        if isinstance(self.attempt_index, bool) or not isinstance(
            self.attempt_index, int,
        ):
            raise TypeError("attempt_index must be an int, not bool.")
        if self.attempt_index < 0:
            raise ValueError("attempt_index must be >= 0.")
        if isinstance(self.delay_seconds, bool) or not isinstance(
            self.delay_seconds, (int, float),
        ):
            raise TypeError("delay_seconds must be a number.")
        if float(self.delay_seconds) < 0:
            raise ValueError("delay_seconds must be non-negative.")
        if self.instrument:
            object.__setattr__(self, "instrument", _canonical_name(self.instrument))


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """
    Per-provider operational health, derived from observable events.

    ``last_error_category`` is ``None`` when the last observation was a
    clean success. ``consecutive_failures`` / ``consecutive_successes``
    drive the deterministic health verdict via the shared derivation
    helper (:func:`derive_domain_health`).
    """

    provider_name: str = ""
    total_operations: int = 0
    total_failures: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_error_category: ReliabilityFailureCategory | None = None
    last_error_at: datetime | None = None
    last_success_at: datetime | None = None

    def __post_init__(self) -> None:
        if isinstance(self.total_operations, bool) or not isinstance(
            self.total_operations, int,
        ):
            raise TypeError("total_operations must be an int.")
        for name in ("total_failures", "consecutive_failures", "consecutive_successes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.consecutive_failures > self.total_failures:
            raise ValueError("consecutive_failures must not exceed total_failures.")
        if self.last_error_at is not None:
            _require_aware(self.last_error_at, "last_error_at")
        if self.last_success_at is not None:
            _require_aware(self.last_success_at, "last_success_at")
        if self.last_error_category is not None and not isinstance(
            self.last_error_category, ReliabilityFailureCategory,
        ):
            raise TypeError(
                "last_error_category must be a ReliabilityFailureCategory or None.",
            )


@dataclass(frozen=True, slots=True)
class CycleHealth:
    """
    Operational health of the scan-cycle stream (19.3 envelope).

    ``total_cycles`` / ``completed_cycles`` / ``failed_cycles`` /
    ``skipped_cycles`` / ``partial_cycles`` answer "did cycles start and
    finish and how did they go?". ``last_cycle_at`` is the last COMPLETED
    cycle instant (the stall detection anchor: no completed cycle within
    the heartbeat policy's ``max_since_cycle_seconds`` => stalled).
    """

    total_cycles: int = 0
    completed_cycles: int = 0
    failed_cycles: int = 0
    skipped_cycles: int = 0
    partial_cycles: int = 0
    last_cycle_at: datetime | None = None
    last_cycle_id: str = ""
    last_cycle_duration_seconds: float | None = None
    last_cycle_status: str = ""

    def __post_init__(self) -> None:
        for name in (
            "total_cycles",
            "completed_cycles",
            "failed_cycles",
            "skipped_cycles",
            "partial_cycles",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.last_cycle_at is not None:
            _require_aware(self.last_cycle_at, "last_cycle_at")
        if self.last_cycle_duration_seconds is not None:
            if isinstance(self.last_cycle_duration_seconds, bool) or not isinstance(
                self.last_cycle_duration_seconds, (int, float),
            ):
                raise TypeError("last_cycle_duration_seconds must be a number or None.")
            if float(self.last_cycle_duration_seconds) < 0:
                raise ValueError("last_cycle_duration_seconds must be non-negative.")


@dataclass(frozen=True, slots=True)
class DeliveryHealth:
    """
    Operational health of the alert-delivery path (19.7 envelope).

    Delivery health NEVER alters the lifecycle or the alert identity: a
    delivery failure is recorded here + retried via the outbox with the
    SAME ``alert_id``; the analytical alert event is preserved verbatim.
    """

    total_attempts: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    suppressed: int = 0
    pending_retries: int = 0
    last_failure_at: datetime | None = None
    last_failure_category: ReliabilityFailureCategory | None = None

    def __post_init__(self) -> None:
        for name in (
            "total_attempts",
            "delivered",
            "failed",
            "skipped",
            "suppressed",
            "pending_retries",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.last_failure_at is not None:
            _require_aware(self.last_failure_at, "last_failure_at")
        if self.last_failure_category is not None and not isinstance(
            self.last_failure_category, ReliabilityFailureCategory,
        ):
            raise TypeError("last_failure_category must be a category or None.")


@dataclass(frozen=True, slots=True)
class PerSymbolFailure:
    """
    One explicit per-symbol operational failure record.

    Isolated per-symbol failures are recorded and surface in the
    operational report WITHOUT turning a partial universe failure into
    a global crash (the other symbols continue and remain accounted).
    ``retried`` records whether the retry policy attempted this symbol
    again. ``category`` may be ``None`` for a defensive unexpected
    exception (classified ``PROCESS_FAILURE`` at construction).
    """

    symbol: str
    category: ReliabilityFailureCategory
    timestamp: datetime
    retried: bool = False
    attempts: int = 1
    detail: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _canonical_name(self.symbol))
        _require_aware(self.timestamp, "timestamp")
        if not isinstance(self.category, ReliabilityFailureCategory):
            raise TypeError("category must be a ReliabilityFailureCategory.")
        if not isinstance(self.retried, bool):
            raise TypeError("retried must be a bool.")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise TypeError("attempts must be an int, not bool.")
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1.")


@dataclass(frozen=True, slots=True)
class RecoveryEvent:
    """
    One observable recovery event.

    A recovery is explicit and observable: every recovery attempt is
    recorded (supply restart / persisted-state recovery / retry
    recovery) with its outcome. ``success`` is True when the recovery
    restored safe operational state; ``detail`` describes what was
    recovered (or why recovery failed — see
    :class:`ReliabilityFailureCategory.RECOVERY_FAILURE`).
    """

    recovery_id: str
    timestamp: datetime
    description: str
    success: bool = True
    category: ReliabilityFailureCategory | None = None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "timestamp")
        if not self.recovery_id.strip():
            raise ValueError("recovery_id must not be empty.")
        if not self.description.strip():
            raise ValueError("description must not be empty.")
        if not isinstance(self.success, bool):
            raise TypeError("success must be a bool.")
        if self.category is not None and not isinstance(
            self.category, ReliabilityFailureCategory,
        ):
            raise TypeError("category must be a ReliabilityFailureCategory or None.")
        if not self.success and self.category is None:
            object.__setattr__(
                self,
                "category",
                ReliabilityFailureCategory.RECOVERY_FAILURE,
            )


@dataclass(frozen=True, slots=True)
class CycleFailureSummary:
    """
    Per-cycle operational accounting (the "cycle health" envelope).

    Answers: did the cycle start / finish / when / how long /
    requested / succeeded / failed / unavailable / skipped / alerts
    generated/delivered/suppressed / recovery / timeout / escape.

    All counts are explicit and NEVER inferred from analytical results.
    ``exceptions_escaped`` is the number of defensive exceptions that
    escaped the expected boundary and were captured by the reliability
    layer (visible, never silent).
    """

    cycle_id: str = ""
    cycle_started_at: datetime | None = None
    cycle_ended_at: datetime | None = None
    duration_seconds: float | None = None
    requested: int = 0
    succeeded: int = 0
    failed: int = 0
    unavailable: int = 0
    skipped: int = 0
    alerts_generated: int = 0
    alerts_delivered: int = 0
    alerts_suppressed: int = 0
    alerts_failed: int = 0
    recovery_occurred: bool = False
    timeout_occurred: bool = False
    exceptions_escaped: int = 0

    def __post_init__(self) -> None:
        if self.cycle_started_at is not None:
            _require_aware(self.cycle_started_at, "cycle_started_at")
        if self.cycle_ended_at is not None:
            _require_aware(self.cycle_ended_at, "cycle_ended_at")
        if (
            self.cycle_started_at is not None
            and self.cycle_ended_at is not None
            and self.cycle_ended_at < self.cycle_started_at
        ):
            raise ValueError("cycle_ended_at must not be before cycle_started_at")
        for name in (
            "requested",
            "succeeded",
            "failed",
            "unavailable",
            "skipped",
            "alerts_generated",
            "alerts_delivered",
            "alerts_suppressed",
            "alerts_failed",
            "exceptions_escaped",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if self.duration_seconds is not None:
            if isinstance(self.duration_seconds, bool) or not isinstance(
                self.duration_seconds, (int, float),
            ):
                raise TypeError("duration_seconds must be a number or None.")
            if float(self.duration_seconds) < 0:
                raise ValueError("duration_seconds must be non-negative.")
        if self.failed > self.unavailable:
            raise ValueError("failed must be a subset of unavailable.")


@dataclass(frozen=True, slots=True)
class OperationalStateBundle:
    """
    ONE immutable operational-state snapshot (the durable/observable
    operator state; SEPARATE from analytical state).

    ``persisted`` is ``True`` when this snapshot was loaded from (or
    written through) the durable reliability store — a snapshot with
    ``persisted=False`` is an in-memory-only operational view (identity
    recovery is not possible; analytical state is never fabricated).
    """

    snapshot_id: str
    recorded_at: datetime
    provider_health: ProviderHealth = field(default_factory=ProviderHealth)
    cycle_health: CycleHealth = field(default_factory=CycleHealth)
    delivery_health: DeliveryHealth = field(default_factory=DeliveryHealth)
    heartbeat_at: datetime | None = None
    heartbeat_state: HeartbeatState = HeartbeatState.NONE
    last_recovery: RecoveryEvent | None = None
    failures: tuple[OperationalEventRecord, ...] = field(default_factory=tuple)
    retries: tuple[RetryRecord, ...] = field(default_factory=tuple)
    per_symbol_failures: tuple[PerSymbolFailure, ...] = field(default_factory=tuple)
    set_ids: tuple[str, ...] = field(default_factory=tuple)
    alert_ids: tuple[str, ...] = field(default_factory=tuple)
    outbox_pending: int = 0
    policy_version: str = ""
    persisted: bool = False

    def __post_init__(self) -> None:
        _require_aware(self.recorded_at, "recorded_at")
        if not self.snapshot_id.strip():
            raise ValueError("snapshot_id must not be empty.")
        for name in ("provider_health", "cycle_health", "delivery_health"):
            value = getattr(self, name)
            if not isinstance(
                value, (ProviderHealth, CycleHealth, DeliveryHealth),
            ):
                raise TypeError(f"{name} must be a validated health model.")
        if self.heartbeat_at is not None:
            _require_aware(self.heartbeat_at, "heartbeat_at")
        if not isinstance(self.heartbeat_state, HeartbeatState):
            raise TypeError("heartbeat_state must be a HeartbeatState.")
        if not isinstance(self.outbox_pending, int) or isinstance(
            self.outbox_pending, bool,
        ):
            raise TypeError("outbox_pending must be an int, not bool.")
        if self.outbox_pending < 0:
            raise ValueError("outbox_pending must be non-negative.")


@dataclass(frozen=True, slots=True)
class OperationalHealthReport:
    """
    The deterministic operational health + diagnostics report.

    ``health_state`` is the aggregate health derived by the reliability
    engine from the operational facts below it (never from setup
    quality). ``failure_counts`` is a sorted tuple of
    ``(category_name, count)`` pairs; ``domain_health`` is a sorted
    tuple of ``(domain_name, HealthStatus)`` pairs.

    ``config_snapshot`` is the auditable reliability-config snapshot;
    ``policy_version`` is the deterministic config-derived policy
    version.
    """

    health_state: OperationalHealthState
    scanner_state: str = ""
    scanner_health: HealthStatus = HealthStatus.UNKNOWN
    provider_health: HealthStatus = HealthStatus.UNKNOWN
    delivery_health: HealthStatus = HealthStatus.UNKNOWN
    heartbeat_state: HeartbeatState = HeartbeatState.NONE
    heartbeat_at: datetime | None = None
    last_cycle_at: datetime | None = None
    last_cycle_id: str = ""
    last_cycle_duration_seconds: float | None = None
    cycle_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    failure_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    domain_health: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    per_symbol_failures: tuple[PerSymbolFailure, ...] = field(default_factory=tuple)
    retry_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    recovery_counts: tuple[tuple[str, int], ...] = field(default_factory=tuple)
    outbox_pending: int = 0
    config_snapshot: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    policy_version: str = ""
    rationale: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.health_state, OperationalHealthState):
            raise TypeError("health_state must be an OperationalHealthState.")
        if self.heartbeat_at is not None:
            _require_aware(self.heartbeat_at, "heartbeat_at")
        if self.last_cycle_at is not None:
            _require_aware(self.last_cycle_at, "last_cycle_at")
        for name in ("scanner_health", "provider_health", "delivery_health"):
            value = getattr(self, name)
            if not isinstance(value, HealthStatus):
                raise TypeError(f"{name} must be a HealthStatus.")
        if not isinstance(self.heartbeat_state, HeartbeatState):
            raise TypeError("heartbeat_state must be a HeartbeatState.")
        if isinstance(self.outbox_pending, bool) or not isinstance(
            self.outbox_pending, int,
        ):
            raise TypeError("outbox_pending must be an int, not bool.")
        if self.outbox_pending < 0:
            raise ValueError("outbox_pending must be non-negative.")

    @property
    def is_healthy(self) -> bool:
        return self.health_state is OperationalHealthState.HEALTHY


# =====================================================================
# PURE DERIVATION HELPERS (deterministic, shared by engine + tests)
# =====================================================================


def derive_domain_health(
    consecutive_failures: int,
    consecutive_successes: int,
    degradation_threshold: int,
    recovery_threshold: int,
    observed: bool = True,
) -> HealthStatus:
    """
    Deterministic per-domain health derivation.

    * no observations -> UNKNOWN (never silently healthy/failed);
    * consecutive failures >= degradation threshold -> FAILED;
    * any failure (below threshold) -> DEGRADED;
    * consecutive successes >= recovery threshold -> HEALTHY;
    * otherwise (some recent success, below recovery) -> HEALTHY when
      there was no failure streak, else DEGRADED.
    """

    if not observed:
        return HealthStatus.UNKNOWN
    if consecutive_failures >= degradation_threshold:
        return HealthStatus.FAILED
    if consecutive_failures > 0:
        return HealthStatus.DEGRADED
    if consecutive_successes >= recovery_threshold:
        return HealthStatus.HEALTHY
    return HealthStatus.HEALTHY


def derive_aggregate_health(
    scanner_status: HealthStatus,
    provider_status: HealthStatus,
    delivery_status: HealthStatus,
    heartbeat_state: HeartbeatState,
    last_cycle_at: datetime | None,
    reference_now: datetime,
    max_since_cycle_seconds: float,
    recovery_pending: bool = False,
    recovery_failure: bool = False,
) -> OperationalHealthState:
    """
    Deterministic AGGREGATE operational health derivation (the smallest
    vocabulary justified by the audit):

    * a recovery failure (corrupt/missing persisted state that could not
      be safely recovered, or any RECOVERY_FAILURE) -> FAILED;
    * a STALE heartbeat OR an absent last cycle beyond
      ``max_since_cycle_seconds`` (STALLED_SCANNER) -> FAILED;
    * ANY domain FAILED -> FAILED;
    * ``recovery_pending`` (a recovery is in progress / healthy
      successes are building up) -> RECOVERING;
    * ANY domain DEGRADED -> DEGRADED;
    * otherwise -> HEALTHY.

    Health NEVER derives from setup quality. A market with many
    LOW-quality setups while the system operates cleanly is HEALTHY; a
    system with many HIGH-quality setups but a failing provider is
    DEGRADED/FAILED.
    """

    try:
        _require_aware(reference_now, "reference_now")
    except (TypeError, ValueError):
        return OperationalHealthState.FAILED
    if recovery_failure:
        return OperationalHealthState.FAILED
    if heartbeat_state is HeartbeatState.STALE:
        return OperationalHealthState.FAILED
    if last_cycle_at is not None:
        try:
            _require_aware(last_cycle_at, "last_cycle_at")
            stall_age = (reference_now - last_cycle_at).total_seconds()
            if stall_age > max_since_cycle_seconds:
                return OperationalHealthState.FAILED
        except (TypeError, ValueError):
            return OperationalHealthState.FAILED
    for status in (scanner_status, provider_status, delivery_status):
        if status is HealthStatus.FAILED:
            return OperationalHealthState.FAILED
    if recovery_pending:
        return OperationalHealthState.RECOVERING
    for status in (scanner_status, provider_status, delivery_status):
        if status is HealthStatus.DEGRADED:
            return OperationalHealthState.DEGRADED
    return OperationalHealthState.HEALTHY


def operational_snapshot_id(
    recorded_at: datetime,
    provider_label: str,
    cycle_id: str,
    alert_cycle_id: str,
    policy_version: str,
) -> str:
    """Deterministic snapshot id (``"ops-" + sha256[:16]``)."""

    _require_aware(recorded_at, "recorded_at")
    parts = [
        provider_label or "~",
        cycle_id or "~",
        alert_cycle_id or "~",
        policy_version or "~",
        recorded_at.isoformat(),
    ]
    return _sha256_prefix("|".join(parts), "ops-")


#: Deterministic id prefixes (documented).
OPERATIONAL_EVENT_ID_PREFIX = "opevt-"
RETRY_ID_PREFIX = "retry-"
RECOVERY_ID_PREFIX = "recover-"
SNAPSHOT_ID_PREFIX = "ops-"


__all__ = [
    "CycleFailureSummary",
    "CycleHealth",
    "DeliveryHealth",
    "HealthStatus",
    "HeartbeatState",
    "OPERATIONAL_EVENT_ID_PREFIX",
    "OperationalEventRecord",
    "OperationalHealthReport",
    "OperationalHealthState",
    "OperationalStateBundle",
    "PerSymbolFailure",
    "ProviderHealth",
    "RECOVERY_ID_PREFIX",
    "RecoveryEvent",
    "ReliabilityFailureCategory",
    "RETRY_ID_PREFIX",
    "RetryRecord",
    "SNAPSHOT_ID_PREFIX",
    "classify_heartbeat",
    "derive_aggregate_health",
    "derive_domain_health",
    "operational_snapshot_id",
]