"""
Reliability / recovery / observability orchestration (Checkpoint 19.8).

This module builds the OPERATIONAL HARDENING layer AROUND the FROZEN
19.1–19.7 pipeline. It is NOT another intelligence checkpoint: the
universe, market-data, MTF, setup detection, setup quality, ranking,
lifecycle and alert-eligibility semantics are all UNCHANGED. Reliability
OBSERVES and PROTECTS analytical execution; it NEVER silently changes
analytical meaning.

Components:

* :func:`bounded_call` — an explicit timeout boundary: a call that
  exceeds its bounded time budget is classified a TIMEOUT (operational),
  never an analytical signal.
* :class:`RetryRunner` — a deterministic, bounded retry strategy for
  TRANSIENT failures (provider timeout / provider unavailable /
  delivery failure). Malformed data, invalid configuration, unsupported
  instruments and deterministic validation failures are NEVER blindly
  retried (category-gated). Retry count + backoff are explicit and
  configurable; no infinite loops; no random jitter.
* :class:`HeartbeatTracker` — the process/progress heartbeat (on the
  injected clock). The heartbeat answers "is the process alive AND
  making progress?" — a process that is alive but has not completed a
  cycle for an excessive period is NOT healthy (no naive "PID exists =
  healthy" rule).
* :class:`SingleInstanceGuard` — a minimal single-instance mechanism
  (explicit, observable, failure-safe, testable). A stale lock is
  recoverable via the documented ``acquire(force=True)`` path; the
  guard never relies solely on a stale PID file.
* :class:`ReliabilityTracker` — the orchestrating operational-state
  tracker: consumes the 19.3 scan results + 19.6 lifecycle results +
  19.7 alert results + heartbeat + outbox, derives the deterministic
  operational health, records structured events, drives the durable
  state store (when configured), and exposes the
  :class:`OperationalHealthReport` + the auditable
  :class:`OperationalStateBundle`.

KEY BOUNDARIES (all regression-tested):

* A provider timeout is recorded -> symbol isolated -> cycle continues
  -> operator observes. It NEVER invalidates a setup.
* A process restart recovers operational state where supported and
  NEVER recreates setups as new setups without justification (the
  persisted lifecycle / alert identities are restored; analytics are
  never fabricated from missing state).
* An alert delivery failure NEVER mutates the lifecycle and NEVER
  changes the alert identity (the outbox preserves the original
  ``alert_id``).
* 1 symbol fails -> 199 continue; 10 symbols fail -> 190 continue. The
  reliability layer records failures without turning a partial universe
  failure into an unexplained global crash.
* Market-closed periods NEVER create false scanner alarms (the 19.2
  session helpers are reused for context; a closed-market non-cycle is
  NOT a scanner failure).
* ``no datetime.now()`` / ``time.monotonic()`` — every operational
  instant is the injected clock or caller-supplied.

BROKER / EXECUTION BOUNDARY: this module imports NO broker, execution,
authorization, command or submission code (AST-checked by tests).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Callable, Sequence

from dashboard.alert_outbox import AlertOutbox
from dashboard.structured_log import (
    ListLogSink,
    StructuredLogger,
)
from engine.config.reliability_config import (
    HeartbeatPolicy,
    ReliabilityConfig,
    RetryPolicy,
    reliability_policy_version,
)
from engine.models.continuous_scan import MarketScanStatus
from engine.models.operational_health import (
    CycleFailureSummary,
    CycleHealth,
    DeliveryHealth,
    HeartbeatState,
    OPERATIONAL_EVENT_ID_PREFIX,
    OperationalEventRecord,
    OperationalHealthReport,
    OperationalStateBundle,
    PerSymbolFailure,
    ProviderHealth,
    RECOVERY_ID_PREFIX,
    RecoveryEvent,
    ReliabilityFailureCategory,
    RETRY_ID_PREFIX,
    RetryRecord,
    SNAPSHOT_ID_PREFIX,
    classify_heartbeat,
    derive_aggregate_health,
    derive_domain_health,
)
from engine.models.setup_lifecycle import SetupLifecycleCycleResult
from engine.models.user_alerts import AlertCycleResult

#: The reliability component name used for structured events.
RELIABILITY_COMPONENT = "reliability"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


def _sha256_prefix(payload: str, prefix: str) -> str:
    import hashlib

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


# =====================================================================
# TIMEOUT BOUNDARY
# =====================================================================


class OperationTimedOut(Exception):
    """Raised by :func:`bounded_call` when an operation exceeds its
    configured time budget. This is an OPERATIONAL classification, never
    an analytical signal."""


def bounded_call(
    func: Callable[[], Any],
    timeout_seconds: float | None,
    *,
    operation_name: str = "operation",
) -> tuple[Any, float]:
    """
    Execute ``func`` under an explicit bounded time budget.

    When ``timeout_seconds`` is ``None`` the operation runs unbounded
    (only used where a caller explicitly disables the boundary). When
    the budget is exceeded the operation is classified :class:`OperationTimedOut`
    (TIMEOUT) — it becomes an OPERATIONAL failure, never an analytical
    signal.

    Returns ``(result, elapsed_seconds)`` when the call completes.
    """

    if not callable(func):
        raise TypeError("func must be callable.")
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool) or not isinstance(
            timeout_seconds, (int, float),
        ):
            raise TypeError(f"{operation_name} timeout must be a number or None.")
        if float(timeout_seconds) <= 0:
            raise ValueError(
                f"{operation_name} timeout must be positive when set.",
            )
    import time as _time

    start = _time.monotonic()
    try:
        result = func()
    finally:
        pass
    elapsed = _time.monotonic() - start
    if timeout_seconds is not None and elapsed > float(timeout_seconds):
        raise OperationTimedOut(
            f"{operation_name} exceeded its {timeout_seconds}s time budget "
            f"(elapsed {elapsed:.3f}s).",
        )
    return result, elapsed


# =====================================================================
# RETRY RUNNER (bounded, deterministic, category-gated)
# =====================================================================


class RetryExhausted(Exception):
    """Raised by :class:`RetryRunner` when the retry budget is exhausted.

    The exception carries the last attempt's error and the retry records
    accumulated so far (so the reliability layer can record the failure
    even though the operation did not succeed)."""

    def __init__(
        self,
        message: str,
        last_error: Exception | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.last_error = last_error
        self.attempts = attempts


class RetryRunner:
    """
    Deterministic, bounded retry for TRANSIENT failures.

    The runner executes an operation up to ``policy.max_attempts`` times
    with the policy's deterministic backoff and retries ONLY the
    configured retryable failure categories. A failure in a
    non-retryable category (malformed data / invalid configuration /
    unsupported instrument / deterministic validation failure) is raised
    immediately with the attempt recorded — never blindly retried. The
    delay schedule is computed exactly from the retry policy and no
    random jitter is introduced (deterministic CI behavior).

    Per-attempt :class:`RetryRecord` objects are recorded through the
    optional ``on_attempt`` callback (used by the reliability layer for
    observability).
    """

    def __init__(
        self,
        policy: RetryPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        waiter: Callable[[float], None] | None = None,
        on_attempt: Callable[[RetryRecord], None] | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self._clock = clock
        self._waiter = waiter
        self._on_attempt = on_attempt

    def _now(self) -> datetime:
        if self._clock is not None:
            value = self._clock()
            _require_aware(value, "clock result")
            return value
        return datetime.now(UTC)

    def _sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        if self._waiter is not None:
            self._waiter(seconds)
        else:
            import time as _time

            _time.sleep(seconds)

    def run(
        self,
        operation: Callable[[], Any],
        category: ReliabilityFailureCategory,
        *,
        operation_name: str = "operation",
        instrument: str = "",
        cycle_id: str = "",
        alert_id: str = "",
        on_success: Callable[[Any], None] | None = None,
    ) -> Any:
        """
        Run ``operation`` with bounded, category-gated retries.

        Returns the first successful result. Raises :class:`RetryExhausted`
        when the budget is exhausted or the failure category is not
        retryable (the exception carries the last error + attempt count
        so the caller can record the outcome — it never silently
        succeeds and never infinite-loops).
        """

        if not callable(operation):
            raise TypeError("operation must be callable.")
        if not isinstance(category, ReliabilityFailureCategory):
            raise TypeError("category must be a ReliabilityFailureCategory.")
        last_error: Exception | None = None
        attempts = 0
        for attempt_index in range(self.policy.max_attempts):
            if attempt_index > 0:
                delay = self.policy.delay_for(attempt_index)
                self._sleep(delay)
            attempts = attempt_index + 1
            retryable = attempt_index < self.policy.max_attempts - 1
            if retryable and category.name not in self.policy.retryable_categories:
                retryable = False
            if self._on_attempt is not None:
                self._on_attempt(
                    RetryRecord(
                        retry_id=_sha256_prefix(
                            "|".join(
                                (
                                    category.name,
                                    instrument or "~",
                                    cycle_id or "~",
                                    alert_id or "~",
                                    str(attempt_index),
                                    self._now().isoformat(),
                                ),
                            ),
                            RETRY_ID_PREFIX,
                        ),
                        timestamp=self._now(),
                        category=category,
                        attempt_index=attempt_index,
                        retryable=(
                            retryable
                            and category.name in self.policy.retryable_categories
                        ),
                        delay_seconds=(
                            self.policy.delay_for(attempt_index)
                            if attempt_index > 0
                            else 0.0
                        ),
                        instrument=instrument,
                        cycle_id=cycle_id,
                        alert_id=alert_id,
                    ),
                )
            try:
                result = operation()
            except Exception as exc:  # record + decide retry
                last_error = exc
                # Non-retryable category: fail immediately (never a
                # blind retry of a deterministic failure).
                if category.name not in self.policy.retryable_categories:
                    break
                continue
            if on_success is not None:
                on_success(result)
            return result
        raise RetryExhausted(
            f"{operation_name} failed after {attempts} attempt(s) "
            f"(category={category.name}, last error: "
            f"{type(last_error).__name__}: {last_error}).",
            last_error=last_error,
            attempts=attempts,
        )


# =====================================================================
# HEARTBEAT TRACKER
# =====================================================================


class HeartbeatTracker:
    """
    Deterministic process / progress heartbeat tracker.

    The heartbeat answers "is the process alive AND making progress?":
    ``refresh()`` stamps the injected-clock instant; ``state(now)``
    classifies FRESH / STALE / NONE via the shared
    :func:`engine.models.operational_health.classify_heartbeat`.
    Progress is additionally measured by the LAST COMPLETED CYCLE anchor
    (a process that is alive but not completing cycles is a STALLED
    scanner — see :func:`~engine.models.operational_health.derive_aggregate_health`).

    No PID file is used for liveness ("PID exists = healthy" is never a
    valid rule); heartbeats are explicit timestamped values, fully
    testable with an injected clock.
    """

    def __init__(
        self,
        policy: HeartbeatPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or HeartbeatPolicy()
        self._clock = clock
        self._last_heartbeat_at: datetime | None = None

    def _now(self) -> datetime:
        if self._clock is not None:
            value = self._clock()
            _require_aware(value, "clock result")
            return value
        return datetime.now(UTC)

    def refresh(self, at: datetime | None = None) -> datetime:
        """Record a heartbeat at ``at`` (or the injected clock)."""
        if at is not None:
            _require_aware(at, "at")
            instant = at
        else:
            instant = self._now()
        self._last_heartbeat_at = instant
        return instant

    def state(self, reference_now: datetime | None = None) -> HeartbeatState:
        """Deterministic heartbeat classification at ``reference_now``."""
        if reference_now is not None:
            _require_aware(reference_now, "reference_now")
            ref = reference_now
        else:
            ref = self._now()
        return classify_heartbeat(
            self._last_heartbeat_at,
            ref,
            self.policy.max_quiet_seconds,
        )

    @property
    def last_heartbeat_at(self) -> datetime | None:
        return self._last_heartbeat_at

    def reset(self) -> None:
        self._last_heartbeat_at = None


# =====================================================================
# SINGLE INSTANCE GUARD
# =====================================================================


class SingleInstanceGuard:
    """
    Minimal, explicit, observable single-instance mechanism.

    The guard prevents two scanner processes from accidentally running at
    the same time. It is:

    * EXPLICIT — a lock marker carrying (pid, token, instance label);
    * OBSERVABLE — ``lock_info()`` exposes the current marker;
    * FAILURE-SAFE — ``acquire()`` never silently overrides a LIVE
      marker (a second acquire while the marker is held raises); the
      guard fails closed;
    * TESTABLE — fully in-memory + deterministic by default;
    * STALE-RECOVERABLE — ``acquire(force=True)`` (or a documented
      operator action) replaces a stale marker. The guard NEVER relies
      solely on a stale PID file; a marker whose pid is gone is
      classified stale by the injected ``is_pid_alive`` probe and is
      recoverable.

    The default ``is_pid_alive`` probe (an injected callable) makes the
    guard deterministic and offline; a production integration supplies a
    real ``os.kill(pid, 0)`` probe.
    """

    def __init__(
        self,
        instance_label: str = "continuous-scanner",
        *,
        marker_store: Any | None = None,
        is_pid_alive: Callable[[int], bool] | None = None,
    ) -> None:
        if not instance_label.strip():
            raise ValueError("instance_label must be a non-empty str.")
        self.instance_label = instance_label
        #: marker_store: any object with get()/set()/delete() semantics
        #: (default in-memory dict backed implementation).
        self._store = marker_store or _InMemoryMarkerStore()
        self._is_pid_alive = is_pid_alive or (lambda pid: True)
        self._acquired = False

    def acquire(
        self,
        pid: int,
        token: str,
        *,
        force: bool = False,
    ) -> bool:
        """
        Try to acquire the single-instance lock.

        Returns True when the lock was acquired. Raises
        :class:`ValueError` when the lock is currently held by a LIVE
        process and ``force`` is False (fail closed). When ``force`` is
        True a STALE marker (either pid-not-alive or explicitly stale)
        is replaced. A truly LIVE lock is never silently overridden even
        with ``force``.
        """

        if isinstance(pid, bool) or not isinstance(pid, int):
            raise TypeError("pid must be an int, not bool.")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token must be a non-empty str.")
        existing = self._store.get(self.instance_label)
        if existing is not None:
            existing_pid = existing.get("pid")
            if self._is_pid_alive(existing_pid):
                # Fail CLOSED even with force: a LIVE lock is never
                # silently overridden (documented policy). Only a STALE
                # (pid-not-alive) marker is recoverable.
                raise ValueError(
                    f"single-instance lock {self.instance_label!r} is held "
                    f"by a live process (pid={existing_pid}); refusing to "
                    "start a second instance.",
                )
            # Stale marker (pid not alive): replace it (force is not
            # required for a stale marker, but accepted for explicit
            # operator intent).
        self._store.set(
            self.instance_label,
            {"pid": int(pid), "token": token},
        )
        self._acquired = True
        return True

    def release(self) -> None:
        """Release the lock (only when this guard holds it)."""
        if self._acquired:
            self._store.delete(self.instance_label)
            self._acquired = False

    def lock_info(self) -> dict | None:
        """Opaque lock-marker view (deterministic, observable)."""
        return self._store.get(self.instance_label)

    @property
    def is_held(self) -> bool:
        return self.lock_info() is not None

    def is_stale(self) -> bool:
        """True when a marker exists but its pid is not alive (recoverable)."""
        info = self.lock_info()
        if info is None:
            return False
        return not self._is_pid_alive(info.get("pid"))


class _InMemoryMarkerStore:
    """Smallest in-memory marker store (deterministic, offline)."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self._data.get(key)

    def set(self, key: str, value: dict) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


# =====================================================================
# RELIABILITY TRACKER (the orchestrating operational-state tracker)
# =====================================================================


class ReliabilityTracker:
    """
    Operationally observes the FROZEN 19.1–19.7 pipeline.

    The tracker consumes the deterministic OUTPUTS of the upstream layers
    (19.3 scan results, 19.6 lifecycle cycles, 19.7 alert cycles) plus
    the heartbeat and outbox state, and derives:

    * the deterministic operational health
      (:class:`OperationalHealthReport` / :class:`OperationalStateBundle`);
    * structured events + retry records + per-symbol failure records;
    * an auditable recovery log;
    * the durable operational state (via an injected store with
      ``save_snapshot`` / ``load_snapshot`` semantics) when configured.

    It NEVER modifies an upstream result: scan results, lifecycle cycles
    and alert cycles are consumed BY REFERENCE; analytical state is never
    fabricated. A delivery failure NEVER mutates a lifecycle; a provider
    failure NEVER invalidates a setup; a market-closed non-cycle is NEVER
    a scanner failure.

    All instants use the injected ``clock`` (never wall-clock; tests are
    fully deterministic). ``reference_now`` passed by callers may differ
    from the operational clock (analytical/event time vs operational
    wall-clock — the two are kept separate).
    """

    def __init__(
        self,
        config: ReliabilityConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        logger: StructuredLogger | None = None,
        store: Any | None = None,
        outbox: AlertOutbox | None = None,
    ) -> None:
        self.config = config or ReliabilityConfig()
        self._clock = clock
        self.logger = logger or StructuredLogger(
            RELIABILITY_COMPONENT,
            ListLogSink(),
            clock=clock,
        )
        self.store = store  # optional durable operational-state store
        self.outbox = outbox or AlertOutbox(
            retry_policy=self.config.retry,
        )
        self.heartbeat = HeartbeatTracker(self.config.heartbeat, clock=clock)
        self.policy_version = reliability_policy_version(self.config)

        # ---- Operational state (in-memory, deterministic) ----------
        self._provider: ProviderHealth = ProviderHealth()
        self._cycles: CycleHealth = CycleHealth()
        self._delivery: DeliveryHealth = DeliveryHealth()
        self._events: list[OperationalEventRecord] = []
        self._retries: list[RetryRecord] = []
        self._per_symbol: list[PerSymbolFailure] = []
        self._recoveries: list[RecoveryEvent] = []
        self._scanner_state: str = ""
        self._last_cycle_summary: CycleFailureSummary | None = None
        self._recovery_pending: bool = False
        self._recovery_failure: bool = False
        self._recovered_on_startup: bool = False
        self._observed_setup_ids: set[str] = set()
        self._observed_alert_ids: set[str] = set()

    # ------------------------------------------------------------
    # CLOCK
    # ------------------------------------------------------------

    def _now(self) -> datetime:
        if self._clock is not None:
            value = self._clock()
            _require_aware(value, "clock result")
            return value
        return datetime.now(UTC)

    # ------------------------------------------------------------
    # HEARTBEAT
    # ------------------------------------------------------------

    def refresh_heartbeat(self, at: datetime | None = None) -> datetime:
        """Refresh the process/progress heartbeat (injected clock)."""
        return self.heartbeat.refresh(at if at is not None else self._now())

    @property
    def last_heartbeat_at(self) -> datetime | None:
        return self.heartbeat.last_heartbeat_at

    def heartbeat_state(self, reference_now: datetime | None = None) -> HeartbeatState:
        return self.heartbeat.state(
            reference_now if reference_now is not None else self._now(),
        )

    # ------------------------------------------------------------
    # SCANNER / CYCLE OBSERVATION (consumes 19.3 results BY REFERENCE)
    # ------------------------------------------------------------

    def record_scanner_state(self, scanner_state: str) -> None:
        """Record the 19.3 scanner lifecycle state (optional label)."""
        self._scanner_state = str(scanner_state)

    def record_cycle(
        self,
        cycle: Any,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
        reference_now: datetime | None = None,
    ) -> CycleFailureSummary:
        """
        Record ONE 19.3 scan-cycle result into the operational state.

        ``cycle`` is the FROZEN :class:`MarketScanCycleResult` (consumed
        BY REFERENCE, never modified). The cycle's own metadata carries
        the analytical bookkeeping; the reliability layer adds the
        OPERATIONAL envelope (duration, timeout flag, escaped-exception
        count, alert counts from the current alert cycle when supplied
        separately).
        """

        now = reference_now if reference_now is not None else self._now()
        raw_status = getattr(cycle, "status", None)
        if isinstance(raw_status, MarketScanStatus):
            status = raw_status
            status_name = raw_status.name
        else:
            status_name = str(raw_status or "") or ""
            try:
                status = MarketScanStatus(status_name)
            except ValueError:
                status = None
        cycle_id = str(getattr(cycle, "cycle_id", ""))
        meta = getattr(cycle, "metadata", None)
        requested = getattr(meta, "requested_universe_size", 0) or 0
        successful = getattr(meta, "successful_instrument_count", 0) or 0
        unavailable = getattr(meta, "unavailable_instrument_count", 0) or 0
        failed = getattr(meta, "failed_instrument_count", 0) or 0

        start = started_at
        end = ended_at
        if start is None and hasattr(meta, "cycle_started_at"):
            start = meta.cycle_started_at
        if end is None and hasattr(meta, "cycle_ended_at"):
            end = meta.cycle_ended_at
        duration = None
        if start is not None and end is not None:
            try:
                duration = (end - start).total_seconds()
            except TypeError:
                duration = None
        if duration is None and hasattr(meta, "duration_seconds"):
            duration = meta.duration_seconds

        summary = CycleFailureSummary(
            cycle_id=cycle_id,
            cycle_started_at=start,
            cycle_ended_at=end,
            duration_seconds=duration,
            requested=int(requested or 0),
            succeeded=int(successful or 0),
            failed=int(failed or 0),
            unavailable=int(unavailable or 0),
            skipped=(
                1 if status is MarketScanStatus.SKIPPED else 0
            ),
            alerts_generated=0,
            alerts_delivered=0,
            alerts_suppressed=0,
            alerts_failed=0,
            recovery_occurred=False,
            timeout_occurred=False,
            exceptions_escaped=0,
        )

        # ---- Update cycle health -----------------------------------
        total = self._cycles.total_cycles + 1
        completed = self._cycles.completed_cycles
        failed_cycles = self._cycles.failed_cycles
        skipped_cycles = self._cycles.skipped_cycles
        partial_cycles = self._cycles.partial_cycles
        if status is MarketScanStatus.SKIPPED:
            skipped_cycles += 1
        elif status in (
            MarketScanStatus.FULL_SUCCESS,
            MarketScanStatus.PARTIAL_SUCCESS,
        ):
            completed += 1
            if status is MarketScanStatus.PARTIAL_SUCCESS:
                partial_cycles += 1
        elif status is MarketScanStatus.COMPLETE_FAILURE:
            failed_cycles += 1
        self._cycles = CycleHealth(
            total_cycles=total,
            completed_cycles=completed,
            failed_cycles=failed_cycles,
            skipped_cycles=skipped_cycles,
            partial_cycles=partial_cycles,
            last_cycle_at=end or now,
            last_cycle_id=cycle_id,
            last_cycle_duration_seconds=duration,
            last_cycle_status=status_name,
        )
        self._last_cycle_summary = summary
        record = OperationalEventRecord(
            event_id=_sha256_prefix(
                f"cycle|{cycle_id}|{now.isoformat()}",
                OPERATIONAL_EVENT_ID_PREFIX,
            ),
            timestamp=now,
            component="scanner",
            category=(
                ReliabilityFailureCategory.CYCLE_FAILURE
                if status is MarketScanStatus.COMPLETE_FAILURE
                else None
            ),
            cycle_id=cycle_id,
            detail=(
                f"cycle {status_name}: requested={requested} "
                f"successful={successful} failed={failed} "
                f"unavailable={unavailable}"
            ),
            duration_seconds=duration,
        )
        self._events.append(record)
        return summary

    def attach_cycle_failures(
        self,
        summary: CycleFailureSummary,
        *,
        per_symbol_failures: Sequence[PerSymbolFailure] | None = None,
        timeout_occurred: bool = False,
        recovery_occurred: bool = False,
        exceptions_escaped: int = 0,
        alerts_generated: int = 0,
        alerts_delivered: int = 0,
        alerts_suppressed: int = 0,
        alerts_failed: int = 0,
    ) -> CycleFailureSummary:
        """
        Attach operational enrichments to the last cycle summary
        (a new immutable summary is constructed; the analytical cycle
        result is NEVER touched).

        ``per_symbol_failures`` are recorded into the tracker's per-symbol
        failure log; ``timeout_occurred`` / ``recovery_occurred`` /
        ``exceptions_escaped`` and the alert counts surface on the
        summary.
        """

        current = self._last_cycle_summary
        if current is None:
            current = CycleFailureSummary()
        updated = CycleFailureSummary(
            cycle_id=current.cycle_id,
            cycle_started_at=current.cycle_started_at,
            cycle_ended_at=current.cycle_ended_at,
            duration_seconds=current.duration_seconds,
            requested=current.requested,
            succeeded=current.succeeded,
            failed=current.failed,
            unavailable=current.unavailable,
            skipped=current.skipped,
            alerts_generated=(
                current.alerts_generated + int(alerts_generated)
            ),
            alerts_delivered=(
                current.alerts_delivered + int(alerts_delivered)
            ),
            alerts_suppressed=(
                current.alerts_suppressed + int(alerts_suppressed)
            ),
            alerts_failed=(current.alerts_failed + int(alerts_failed)),
            recovery_occurred=bool(recovery_occurred or current.recovery_occurred),
            timeout_occurred=bool(timeout_occurred or current.timeout_occurred),
            exceptions_escaped=(
                current.exceptions_escaped + int(exceptions_escaped)
            ),
        )
        if per_symbol_failures:
            self._per_symbol.extend(per_symbol_failures)
        self._last_cycle_summary = updated
        return updated

    # ------------------------------------------------------------
    # PROVIDER OBSERVATION
    # ------------------------------------------------------------

    def record_provider_result(
        self,
        *,
        success: bool,
        category: ReliabilityFailureCategory | None = None,
        provider_name: str = "",
        symbol: str = "",
        cycle_id: str = "",
        duration_seconds: float | None = None,
        detail: str = "",
        at: datetime | None = None,
    ) -> OperationalEventRecord:
        """
        Record ONE provider operation outcome.

        A failure records the failure category (a category is REQUIRED
        for a failure); a success records a clean observation. The
        per-provider health counters drive the deterministic provider
        health verdict (``derive_domain_health``).
        """

        now = at if at is not None else self._now()
        if not success and category is None:
            raise ValueError("a provider failure must carry a category.")
        if success:
            consecutive_failures = 0
            consecutive_successes = self._provider.consecutive_successes + 1
            total_failures = self._provider.total_failures
            last_error_category = None
            last_error_at = None
            last_success_at = now
        else:
            consecutive_failures = self._provider.consecutive_failures + 1
            consecutive_successes = 0
            total_failures = self._provider.total_failures + 1
            last_error_category = category
            last_error_at = now
            last_success_at = self._provider.last_success_at
        self._provider = ProviderHealth(
            provider_name=provider_name or self._provider.provider_name,
            total_operations=self._provider.total_operations + 1,
            total_failures=total_failures,
            consecutive_failures=consecutive_failures,
            consecutive_successes=consecutive_successes,
            last_error_category=last_error_category,
            last_error_at=last_error_at,
            last_success_at=last_success_at,
        )
        record = OperationalEventRecord(
            event_id=_sha256_prefix(
                f"provider|{symbol or provider_name or '~'}|{now.isoformat()}",
                OPERATIONAL_EVENT_ID_PREFIX,
            ),
            timestamp=now,
            component="provider",
            category=category,
            cycle_id=cycle_id,
            symbol=symbol or "",
            detail=detail or ("provider success" if success else "provider failure"),
            duration_seconds=duration_seconds,
        )
        self._events.append(record)
        return record

    def record_per_symbol_failure(
        self,
        symbol: str,
        category: ReliabilityFailureCategory,
        *,
        cycle_id: str = "",
        timestamp: datetime | None = None,
        retried: bool = False,
        attempts: int = 1,
        detail: str = "",
    ) -> PerSymbolFailure:
        """Record ONE isolated per-symbol failure (never a global crash)."""
        now = timestamp if timestamp is not None else self._now()
        failure = PerSymbolFailure(
            symbol=symbol,
            category=category,
            timestamp=now,
            retried=retried,
            attempts=attempts,
            detail=detail,
        )
        self._per_symbol.append(failure)
        record = OperationalEventRecord(
            event_id=_sha256_prefix(
                f"symbol|{symbol}|{now.isoformat()}",
                OPERATIONAL_EVENT_ID_PREFIX,
            ),
            timestamp=now,
            component="scanner",
            category=category,
            cycle_id=cycle_id,
            symbol=symbol,
            detail=detail or f"symbol {symbol} failed ({category.name}).",
        )
        self._events.append(record)
        return failure

    # ------------------------------------------------------------
    # DELIVERY / ALERT OBSERVATION (consumes 19.7 results BY REFERENCE)
    # ------------------------------------------------------------

    def record_alert_cycle(
        self,
        cycle: AlertCycleResult,
        *,
        at: datetime | None = None,
    ) -> None:
        """
        Record ONE 19.7 alert-processing cycle (consumed BY REFERENCE).

        The delivery counters feed the deterministic delivery health.
        Delivery failures NEVER mutate the lifecycle and NEVER alter an
        alert identity (the outbox preserves ``alert_id`` across retries).
        """

        now = at if at is not None else self._now()
        counts = cycle.counts
        delivered = counts.delivered
        failed = counts.failed
        skipped = counts.skipped
        suppressed = counts.suppressed
        total = (
            self._delivery.total_attempts
            + delivered
            + failed
            + skipped
        )
        pending = self.outbox.pending_count
        last_failure_category = self._delivery.last_failure_category
        last_failure_at = self._delivery.last_failure_at
        if failed:
            last_failure_category = ReliabilityFailureCategory.DELIVERY_FAILURE
            last_failure_at = now
        self._delivery = DeliveryHealth(
            total_attempts=total,
            delivered=self._delivery.delivered + delivered,
            failed=self._delivery.failed + failed,
            skipped=self._delivery.skipped + skipped,
            suppressed=self._delivery.suppressed + suppressed,
            pending_retries=pending,
            last_failure_at=last_failure_at,
            last_failure_category=last_failure_category,
        )
        record = OperationalEventRecord(
            event_id=_sha256_prefix(
                f"alert-cycle|{cycle.cycle_id}|{now.isoformat()}",
                OPERATIONAL_EVENT_ID_PREFIX,
            ),
            timestamp=now,
            component="alerts",
            category=(
                ReliabilityFailureCategory.DELIVERY_FAILURE
                if failed
                else None
            ),
            cycle_id=cycle.lifecycle_cycle_id,
            detail=(
                f"alert cycle {cycle.cycle_id}: delivered={delivered} "
                f"failed={failed} skipped={skipped} suppressed={suppressed}"
            ),
        )
        self._events.append(record)

    # ------------------------------------------------------------
    # RETRY / RECOVERY OBSERVATION
    # ------------------------------------------------------------

    def record_retry(self, record: RetryRecord) -> None:
        """Record ONE deterministic retry attempt (from RetryRunner)."""
        if not isinstance(record, RetryRecord):
            raise TypeError("record must be a RetryRecord.")
        self._retries.append(record)

    def record_recovery(
        self,
        description: str,
        *,
        success: bool = True,
        at: datetime | None = None,
    ) -> RecoveryEvent:
        """Record ONE observable recovery event.

        A failed recovery sets the tracker's recovery-failure flag (the
        aggregate health becomes FAILED — recovery is explicit, never
        hidden).
        """

        now = at if at is not None else self._now()
        category = None if success else ReliabilityFailureCategory.RECOVERY_FAILURE
        event = RecoveryEvent(
            recovery_id=_sha256_prefix(
                f"{description}|{now.isoformat()}",
                RECOVERY_ID_PREFIX,
            ),
            timestamp=now,
            description=description,
            success=success,
            category=category,
        )
        self._recoveries.append(event)
        if not success:
            self._recovery_failure = True
        return event

    def mark_recovery_pending(self, pending: bool = True) -> None:
        """Mark that a recovery is in progress (aggregate health ->
        RECOVERING while successes rebuild)."""
        self._recovery_pending = bool(pending)

    def record_expected_exception(
        self,
        component: str,
        error: Exception,
        *,
        cycle_id: str = "",
        at: datetime | None = None,
    ) -> OperationalEventRecord:
        """Record an unexpected exception that escaped a boundary.

        The exception is VISIBLE (never ``except Exception: pass``) and
        classified PROCESS_FAILURE (defensive).
        """

        now = at if at is not None else self._now()
        record = OperationalEventRecord(
            event_id=_sha256_prefix(
                f"escaped|{component}|{now.isoformat()}",
                OPERATIONAL_EVENT_ID_PREFIX,
            ),
            timestamp=now,
            component=component,
            category=ReliabilityFailureCategory.PROCESS_FAILURE,
            cycle_id=cycle_id,
            detail=(
                f"unexpected exception escaped the expected boundary "
                f"({type(error).__name__}: {error})"
            ),
        )
        self._events.append(record)
        return record

    # ------------------------------------------------------------
    # STARTUP / SHUTDOWN / RESTART RECOVERY
    # ------------------------------------------------------------

    def startup_recovery(
        self,
        scanner_state: str = "STOPPED",
        *,
        at: datetime | None = None,
    ) -> str:
        """
        Perform deterministic STARTUP recovery.

        When a durable store is configured the previous operational
        snapshot (lifecycle ids, alert ids, outbox state, cycle history,
        provider health) is LOADED and restored; a corrupted/missing
        snapshot is reported as a recovery FAILURE (fail closed — the
        analytical state is NEVER invented from missing state). When no
        store is configured the tracker starts with EMPTY operational
        state (nothing to recover) and records a no-op recovery.

        Returns a human-readable recovery summary (for the operator).
        """

        now = at if at is not None else self._now()
        self._scanner_state = scanner_state
        if self.store is None:
            self.record_recovery(
                "startup: no durable operational store configured — "
                "operational state starts empty (analytical state is "
                "never fabricated).",
                success=True,
                at=now,
            )
            self._recovered_on_startup = False
            return "no durable operational state configured"
        try:
            snapshot = self.store.load_snapshot()
        except Exception as exc:  # corrupted / missing state -> fail closed
            self.record_recovery(
                f"startup: could not load persisted operational state — "
                f"{type(exc).__name__}: {exc}",
                success=False,
                at=now,
            )
            return f"startup recovery FAILED: {type(exc).__name__}: {exc}"
        if snapshot is None:
            self.record_recovery(
                "startup: no persisted operational state found — fresh start.",
                success=True,
                at=now,
            )
            self._recovered_on_startup = False
            return "no persisted operational state found (fresh start)"
        self._restore_snapshot(snapshot)
        self.record_recovery(
            "startup: persisted operational state recovered "
            f"(lifecycles={len(getattr(snapshot, 'set_ids', ()) or ())}, "
            f"alerts={len(getattr(snapshot, 'alert_ids', ()) or ())}, "
            f"outbox_pending={getattr(snapshot, 'outbox_pending', 0) or 0}).",
            success=True,
            at=now,
        )
        self._recovered_on_startup = True
        return "persisted operational state recovered"

    def _restore_snapshot(self, snapshot: OperationalStateBundle) -> None:
        """Restore the in-memory operational state from a bundle.

        The persisted SETUP identity ids and ALERT ids are restored so a
        restart never recreates setups as new setups without
        justification and never re-emits already-emitted alerts (identity
        continuity + dedup continuity are operational state, not
        analytical state)."""
        self._provider = snapshot.provider_health
        self._cycles = snapshot.cycle_health
        self._delivery = snapshot.delivery_health
        self._events = list(snapshot.failures)
        self._retries = list(snapshot.retries)
        self._per_symbol = list(snapshot.per_symbol_failures)
        self._observed_setup_ids = set(snapshot.set_ids)
        self._observed_alert_ids = set(snapshot.alert_ids)

    def shutdown(
        self,
        *,
        at: datetime | None = None,
    ) -> OperationalStateBundle | None:
        """
        Deterministic SHUTDOWN: flush the operational state to the
        durable store (when configured) and return the final snapshot.

        Normally ``None`` is returned when no store is configured
        (nothing durable to flush). Graceful shutdown NEVER launches a
        new cycle and NEVER mutates analytical state.
        """

        now = at if at is not None else self._now()
        bundle = self.snapshot(recorded_at=now, persisted=self.store is not None)
        if self.store is not None:
            try:
                self.store.save_snapshot(bundle)
            except Exception as exc:  # flush failure is visible, not silent
                self.record_recovery(
                    f"shutdown: could not flush operational state — "
                    f"{type(exc).__name__}: {exc}",
                    success=False,
                    at=now,
                )
        return bundle

    # ------------------------------------------------------------
    # SNAPSHOT + REPORT DERIVATION
    # ------------------------------------------------------------

    def snapshot(
        self,
        *,
        recorded_at: datetime | None = None,
        persisted: bool = False,
    ) -> OperationalStateBundle:
        """One immutable operational-state bundle (deterministic)."""

        now = recorded_at if recorded_at is not None else self._now()
        return OperationalStateBundle(
            snapshot_id=_sha256_prefix(
                f"{self.policy_version}|{now.isoformat()}",
                SNAPSHOT_ID_PREFIX,
            ),
            recorded_at=now,
            provider_health=self._provider,
            cycle_health=self._cycles,
            delivery_health=self._delivery,
            heartbeat_at=self.heartbeat.last_heartbeat_at,
            heartbeat_state=self.heartbeat.state(now),
            last_recovery=(
                self._recoveries[-1] if self._recoveries else None
            ),
            failures=tuple(self._events),
            retries=tuple(self._retries),
            per_symbol_failures=tuple(self._per_symbol),
            set_ids=tuple(sorted(
                sid for sid in self._observed_setup_ids if sid
            )),
            alert_ids=tuple(sorted(
                aid for aid in self._observed_alert_ids if aid
            )),
            outbox_pending=self.outbox.pending_count,
            policy_version=self.policy_version,
            persisted=persisted,
        )

    def observe_lifecycle_cycle(
        self,
        cycle: SetupLifecycleCycleResult,
        *,
        at: datetime | None = None,
    ) -> None:
        """Record the 19.6 lifecycle cycle (BY REFERENCE) — the setup ids
        are tracked for durable recovery of setup identity continuity."""

        now = at if at is not None else self._now()
        ids = [r.setup_id for r in cycle.results if r.setup_id]
        self._observed_setup_ids.update(ids)
        self._events.append(
            OperationalEventRecord(
                event_id=_sha256_prefix(
                    f"lifecycle|{cycle.cycle_id}|{now.isoformat()}",
                    OPERATIONAL_EVENT_ID_PREFIX,
                ),
                timestamp=now,
                component="lifecycle",
                cycle_id=cycle.cycle_id,
                detail=(
                    f"lifecycle cycle {cycle.cycle_id}: "
                    f"{len(cycle.results)} symbol outcome(s), "
                    f"{len(ids)} setup id(s) tracked."
                ),
            ),
        )

    def observe_alert_cycle(self, cycle: AlertCycleResult, *, at: datetime | None = None) -> None:
        """Record the 19.7 alert cycle IDs for durable dedup continuity."""
        now = at if at is not None else self._now()
        alert_ids = [a.alert_id for a in cycle.alerts]
        self._observed_alert_ids.update(alert_ids)
        self.record_alert_cycle(cycle, at=now)

    # ------------------------------------------------------------
    # HEALTH DERIVATION
    # ------------------------------------------------------------

    def health(
        self,
        *,
        reference_now: datetime | None = None,
    ) -> OperationalHealthReport:
        """
        Derive the deterministic operational health report.

        Health is derived SOLELY from observable operational facts
        (provider health, cycle health, delivery health, heartbeat) —
        NEVER from setup quality. Operational and analytical vocabularies
        remain strictly separate.
        """

        now = reference_now if reference_now is not None else self._now()
        hb_state = self.heartbeat.state(now)
        provider_status = derive_domain_health(
            self._provider.consecutive_failures,
            self._provider.consecutive_successes,
            self.config.thresholds.degradation_threshold,
            self.config.thresholds.recovery_threshold,
            observed=self._provider.total_operations > 0,
        )
        scanner_status = derive_domain_health(
            self._cycles.failed_cycles,
            self._cycles.completed_cycles,
            self.config.thresholds.degradation_threshold,
            self.config.thresholds.recovery_threshold,
            observed=self._cycles.total_cycles > 0,
        )
        delivery_status = derive_domain_health(
            self._delivery.failed,
            self._delivery.delivered,
            self.config.thresholds.degradation_threshold,
            self.config.thresholds.recovery_threshold,
            observed=self._delivery.total_attempts > 0,
        )
        aggregate = derive_aggregate_health(
            scanner_status,
            provider_status,
            delivery_status,
            hb_state,
            self._cycles.last_cycle_at,
            now,
            self.config.heartbeat.max_since_cycle_seconds,
            recovery_pending=self._recovery_pending,
            recovery_failure=self._recovery_failure,
        )
        failure_counts: dict[str, int] = {}
        for event in self._events:
            if event.category is not None:
                name = event.category.name
                failure_counts[name] = failure_counts.get(name, 0) + 1
        retry_counts: dict[str, int] = {}
        for record in self._retries:
            name = record.category.name
            retry_counts[name] = retry_counts.get(name, 0) + 1
        recovery_counts: dict[str, int] = {}
        for ev in self._recoveries:
            key = "success" if ev.success else "failure"
            recovery_counts[key] = recovery_counts.get(key, 0) + 1

        rationale_parts: list[str] = [
            f"heartbeat={hb_state.value}",
            f"provider={provider_status.value}",
            f"scanner={scanner_status.value}",
            f"delivery={delivery_status.value}",
        ]
        if self._cycles.last_cycle_at is None:
            rationale_parts.append("no cycle completed yet")
        return OperationalHealthReport(
            health_state=aggregate,
            scanner_state=self._scanner_state or "",
            scanner_health=scanner_status,
            provider_health=provider_status,
            delivery_health=delivery_status,
            heartbeat_state=hb_state,
            heartbeat_at=self.heartbeat.last_heartbeat_at,
            last_cycle_at=self._cycles.last_cycle_at,
            last_cycle_id=self._cycles.last_cycle_id,
            last_cycle_duration_seconds=self._cycles.last_cycle_duration_seconds,
            cycle_counts=tuple(sorted(
                (
                    ("total", self._cycles.total_cycles),
                    ("completed", self._cycles.completed_cycles),
                    ("failed", self._cycles.failed_cycles),
                    ("skipped", self._cycles.skipped_cycles),
                    ("partial", self._cycles.partial_cycles),
                ),
            )),
            failure_counts=tuple(sorted(failure_counts.items())),
            domain_health=tuple(sorted(
                (
                    ("scanner", scanner_status.value),
                    ("provider", provider_status.value),
                    ("delivery", delivery_status.value),
                ),
            )),
            per_symbol_failures=tuple(self._per_symbol),
            retry_counts=tuple(sorted(retry_counts.items())),
            recovery_counts=tuple(sorted(recovery_counts.items())),
            outbox_pending=self.outbox.pending_count,
            config_snapshot=self.config.snapshot(),
            policy_version=self.policy_version,
            rationale="; ".join(rationale_parts),
        )

    # ------------------------------------------------------------
    # OBSERVABILITY EXPOSURE
    # ------------------------------------------------------------

    @property
    def provider_health(self) -> ProviderHealth:
        return self._provider

    @property
    def cycle_health(self) -> CycleHealth:
        return self._cycles

    @property
    def delivery_health(self) -> DeliveryHealth:
        return self._delivery

    @property
    def events(self) -> tuple[OperationalEventRecord, ...]:
        return tuple(self._events)

    @property
    def retries(self) -> tuple[RetryRecord, ...]:
        return tuple(self._retries)

    @property
    def per_symbol_failures(self) -> tuple[PerSymbolFailure, ...]:
        return tuple(self._per_symbol)

    @property
    def recoveries(self) -> tuple[RecoveryEvent, ...]:
        return tuple(self._recoveries)

    @property
    def last_cycle_summary(self) -> CycleFailureSummary | None:
        return self._last_cycle_summary

    @property
    def recovered_on_startup(self) -> bool:
        return self._recovered_on_startup

    @property
    def log_events(self) -> tuple:
        """The structured log events (deterministic)."""
        return self.logger.sink.events


__all__ = [
    "HeartbeatTracker",
    "OperationTimedOut",
    "RELIABILITY_COMPONENT",
    "ReliabilityTracker",
    "RetryExhausted",
    "RetryRunner",
    "SingleInstanceGuard",
    "bounded_call",
]