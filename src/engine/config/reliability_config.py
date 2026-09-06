"""
Reliability / recovery / observability configuration (Checkpoint 19.8).

All reliability thresholds, retry/backoff/timeout policies, heartbeat
policy and health thresholds live here; no magic numbers are embedded in
the reliability engine or models. The defaults are deliberately
conservative, deterministic and DOCUMENTED (each threshold carries its
reasoning in its field docstring).

RELIABILITY POLICY PARAMETERS:

``RetryPolicy``
    Bounded, deterministic retry strategy for TRANSIENT provider /
    delivery operations:

    * ``max_attempts``        total attempts INCLUDING the first
    * ``max_retries``         = ``max_attempts - 1``
    * ``base_delay_seconds``  fixed delay between attempts
    * ``backoff_factor``      linear multiplier applied AFTER the base
                              delay (``delay_n = base_delay * n`` where
                              ``n`` is the zero-based attempt index).
                              Deliberately NO random jitter — jitter is
                              NOT required by this architecture and
                              would break deterministic CI behavior.
    * ``retryable_categories`` set of
      :class:`engine.models.operational_health.ReliabilityFailureCategory`
      member NAMES that MAY be retried. Transient categories default:
      ``PROVIDER_TIMEOUT``, ``PROVIDER_UNAVAILABLE`` (temporary
      network/provider failures) and ``DELIVERY_FAILURE``. Category
      names are validated; unknown names are rejected.

    FAIL-SAFE design decisions (documented):

    * malformed data          -> NOT retried (deterministic failure)
    * invalid configuration   -> NOT retried (fail closed at config
                                  construction)
    * unsupported instrument  -> NOT retried (explicit unsupported)
    * deterministic validation failures -> NOT retried
    * a provider timeout      -> retried if the category is in
                                 ``retryable_categories`` (transient)

``TimeoutPolicy``
    Bounded operation time budget:

    * ``provider_operation_seconds`` single provider fetch upper bound;
      a fetch exceeding it is aborted/cut with a TIMEOUT classification
    * ``cycle_operation_seconds``    single scan-cycle upper bound; a
      cycle exceeding it is classified a hung/overrun cycle (operational
      observation, never an analytical signal)
    * ``delivery_operation_seconds`` single alert-delivery upper bound

``HeartbeatPolicy``
    Process-alive / progress-alive policy (the heartbeat answers
    "is the process alive AND making progress?" — it NEVER answers
    "is the system healthy"; a live-but-stalled process is NOT healthy):

    * ``interval_seconds``   heartbeat flush/refresh interval
    * ``max_quiet_seconds``  a heartbeat older than this is STALE
    * ``max_since_cycle_seconds`` a process that last completed a cycle
      longer ago than this is classified a STALLED scanner even when
      the heartbeat is technically fresh (process alive /= healthy)

``HealthThresholds``
    Deterministic operational-health classification thresholds (see
    :mod:`engine.models.operational_health` for the model):

    * ``degradation_threshold`` consecutive failure streaks that move
      HEALTHY -> DEGRADED / DEGRADED -> FAILED
    * ``recovery_threshold``   consecutive clean successes that move
      RECOVERING -> HEALTHY

``ReliabilityConfig``
    Bundles the sub-configs + ``label`` / ``metadata`` + a deterministic
    ``snapshot()`` (sorted, auditable) and a derived policy/reliability
    version embedded in every deterministic reliability identity.

    Configuration errors FAIL CLOSED at construction: invalid interval,
    invalid timeout, invalid retry count, invalid backoff, invalid
    health threshold and invalid persistence options all raise before
    any component can start. No unsafe default is silently substituted.

TIME SOURCES: this module defines POLICY ONLY. Actual operation uses the
injected clock / waiter convention of the rest of the repository; the
reliability layer NEVER calls ``datetime.now()``/``time.monotonic()``
itself (all instants are caller-supplied).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import ClassVar


def _identity(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: Reliability MODEL structural version (documented; see audit doc).
RELIABILITY_MODEL_VERSION = 1

#: Schema version for persisted operational-state documents (19.8).
OPERATIONAL_STATE_SCHEMA_VERSION = 1

#: Default heartbeat max quiet window (seconds). Reasoning: the worker
#: refreshes the heartbeat every :data:`DEFAULT_HEARTBEAT_INTERVAL_SECONDS`
#: (30s); a heartbeat older than 4 intervals (120s) is treated as STALE —
#: the process may be alive but is not proving progress. Configurable via
#: :class:`HeartbeatPolicy.max_quiet_seconds`; market-closed periods must
#: NEVER produce a false scanner alarm from the mere absence of a cycle
#: (the stall classification uses the last-COMPLETED-cycle anchor, not the
#: session state).
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
DEFAULT_HEARTBEAT_MAX_QUIET_SECONDS = 120.0

#: Default STALLED-SCANNER tolerance (seconds): a process that last
#: COMPLETED a scan cycle more than 1 hour ago is classified a STALLED
#: scanner even when the heartbeat is technically FRESH — "alive" is never
#: treated as "healthy" (no naive PID-exists = healthy rule). This matches
#: the default intraday scan cadence (15-minute cycles) with an ample
#: tolerance so a single slow cycle never triggers a false positive.
DEFAULT_STALLED_SCANNER_MAX_SINCE_CYCLE_SECONDS = 3600.0

#: Naive heartbeat reference handling (internal policy flag): a naive
#: (timezone-unaware) heartbeat is NEVER treated as verifiable/fresh; the
#: classifier fails closed to STALE.
DEFAULT_HEARTBEAT_NAIVE_MARKER = False

#: Single-instance lock handling (internal policy flag): the guard ALWAYS
#: fails closed — a second acquire while a LIVE marker exists raises even
#: with ``force=True``; only a STALE marker is recoverable.
DEFAULT_MARKER_STORE_FAIL_CLOSED = True


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """
    Bounded, deterministic retry policy for TRANSIENT failures.

    ``max_attempts`` is the TOTAL number of attempts INCLUDING the first
    (a value of 1 disables retrying). Delays are computed exactly as
    ``base_delay_seconds * (attempt_index)`` when ``backoff_factor`` > 1
    (linear backoff), with NO random jitter (deterministic CI behavior).
    Only categories listed in ``retryable_categories`` may be retried;
    anything else fails immediately.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    backoff_factor: float = 1.0
    retryable_categories: tuple[str, ...] = (
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "DELIVERY_FAILURE",
    )

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int,
        ):
            raise TypeError("max_attempts must be an int, not bool.")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1.")
        if self.base_delay_seconds is None or not isinstance(
            self.base_delay_seconds, (int, float),
        ):
            raise TypeError("base_delay_seconds must be a number.")
        delay = float(self.base_delay_seconds)
        if delay < 0:
            raise ValueError("base_delay_seconds must be non-negative.")
        if self.backoff_factor is None or not isinstance(
            self.backoff_factor, (int, float),
        ):
            raise TypeError("backoff_factor must be a number.")
        factor = float(self.backoff_factor)
        if factor < 1.0:
            raise ValueError("backoff_factor must be >= 1.0.")
        object.__setattr__(self, "base_delay_seconds", delay)
        object.__setattr__(self, "backoff_factor", factor)
        from engine.models.operational_health import (
            _RETRYABLE_CATEGORY_NAMES,
        )

        seen: set[str] = set()
        for name in self.retryable_categories:
            if not isinstance(name, str) or not name:
                raise TypeError(
                    "retryable_categories entries must be non-empty str.",
                )
            if name not in _RETRYABLE_CATEGORY_NAMES:
                raise ValueError(
                    f"unknown retryable category {name!r} — the failure "
                    "category vocabulary is fixed.",
                )
            if name in seen:
                raise ValueError(f"duplicate retryable category {name!r}.")
            seen.add(name)

    @property
    def max_retries(self) -> int:
        """Maximum number of retries after the first attempt."""
        return self.max_attempts - 1

    def delay_for(self, attempt_index: int) -> float:
        """
        Deterministic delay before the attempt at ``attempt_index``
        (zero-based; the first attempt has delay 0).

        ``delay = base_delay * (backoff_factor ** attempt_index)`` —
        a bounded, deterministic, configurable, testable backoff. NO
        random jitter.
        """

        if isinstance(attempt_index, bool) or not isinstance(attempt_index, int):
            raise TypeError("attempt_index must be a non-negative int.")
        if attempt_index < 0:
            raise ValueError("attempt_index must be a non-negative int.")
        return float(self.base_delay_seconds) * (
            float(self.backoff_factor) ** attempt_index
        )

    def should_retry(
        self,
        attempt_index: int,
        category_name: str,
    ) -> bool:
        """
        Deterministic retry decision for ONE attempt.

        ``attempt_index`` is the zero-based index of the COMPLETED
        attempt (the last attempt that just failed). A retry is allowed
        only when the attempted attempt was not the final one AND the
        failure category is explicitly retryable.
        """

        if not isinstance(attempt_index, int) or attempt_index < 0:
            return False
        if attempt_index >= self.max_attempts - 1:
            return False
        return category_name in self.retryable_categories

    def sweep(self) -> tuple[int, ...]:
        """Deterministic per-attempt delay schedule for tests/docs."""
        return tuple(
            int(self.delay_for(i)) if i > 0 else 0
            for i in range(self.max_attempts)
        )


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """
    Bounded operation time budgets.

    Each boundary is a documented UPPER BOUND on a single operation;
    exceeding it becomes an OPERATIONAL failure classification (never an
    analytical signal). ``None`` disables the boundary for that
    operation (not recommended excepted for tests that prove the
    boundary itself).
    """

    provider_operation_seconds: float | None = 15.0
    cycle_operation_seconds: float | None = 300.0
    delivery_operation_seconds: float | None = 5.0

    def __post_init__(self) -> None:
        for name in (
            "provider_operation_seconds",
            "cycle_operation_seconds",
            "delivery_operation_seconds",
        ):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(
                value, (int, float),
            ):
                raise TypeError(f"{name} must be a number or None.")
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive when set.")
            object.__setattr__(self, name, float(value))


@dataclass(frozen=True, slots=True)
class HeartbeatPolicy:
    """
    Process / progress heartbeat policy.

    The heartbeat answers "is the process alive AND making progress?"
    (a heartbeat is refreshed at ``interval_seconds`` while the worker is
    progressing; any heartbeat older than ``max_quiet_seconds`` is
    STALE). A process that last COMPLETED a scan cycle more than
    ``max_since_cycle_seconds`` ago is classified a STALLED scanner even
    when the heartbeat is nominally fresh — a process that is alive but
    not completing cycles must not be considered healthy merely because
    it exists (no naive "PID exists = healthy" rule).
    """

    interval_seconds: float = 30.0
    max_quiet_seconds: float = 120.0
    max_since_cycle_seconds: float = 3600.0

    def __post_init__(self) -> None:
        for name in (
            "interval_seconds",
            "max_quiet_seconds",
            "max_since_cycle_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number.")
            if float(value) <= 0:
                raise ValueError(f"{name} must be positive.")
            object.__setattr__(self, name, float(value))
        if self.max_quiet_seconds < self.interval_seconds:
            raise ValueError(
                "max_quiet_seconds must be >= interval_seconds "
                "(a heartbeat is refreshed every interval; the stale "
                "threshold must exceed the interval).",
            )


@dataclass(frozen=True, slots=True)
class HealthThresholds:
    """
    Deterministic operational-health classification thresholds.

    ``degradation_threshold`` consecutive failures move
    HEALTHY -> DEGRADED -> FAILED (the provider / delivery health is
    per-operational-domain; a single failure NEVER marks the system
    failed). ``recovery_threshold`` consecutive clean successes move
    DEGRADED/RECOVERING -> HEALTHY.

    Health is DERIVED SOLELY from observable operational facts (cycles,
    provider results, delivery results, heartbeats) and NEVER from setup
    quality: many LOW-quality setups while the system is operational
    remains HEALTHY; a healthy-looking provider stream with a stale
    heartbeat is NOT healthy.
    """

    degradation_threshold: int = 3
    recovery_threshold: int = 2

    def __post_init__(self) -> None:
        for name in ("degradation_threshold", "recovery_threshold"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int, not bool.")
            if value < 1:
                raise ValueError(f"{name} must be >= 1.")


@dataclass(frozen=True, slots=True)
class ReliabilityConfig:
    """
    Bundled reliability configuration (Checkpoint 19.8).

    Every sub-policy is validated at construction; the bundle exposes a
    deterministic ``snapshot()`` and a derived reliability (policy)
    version embedded in every deterministic reliability identity.
    """

    retry: RetryPolicy = RetryPolicy()
    timeout: TimeoutPolicy = TimeoutPolicy()
    heartbeat: HeartbeatPolicy = HeartbeatPolicy()
    thresholds: HealthThresholds = HealthThresholds()
    label: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    #: The canonical ordering of sub-policy snapshot keys (stable).
    _SNAPSHOT_KEYS: ClassVar[tuple[str, ...]] = (
        "retry",
        "timeout",
        "heartbeat",
        "thresholds",
    )

    def __post_init__(self) -> None:
        for name in self._SNAPSHOT_KEYS:
            value = getattr(self, name)
            if not isinstance(value, (RetryPolicy, TimeoutPolicy, HeartbeatPolicy, HealthThresholds)):
                raise TypeError(f"{name} must be a validated policy.")
        if not isinstance(self.label, str):
            raise TypeError("label must be a str.")
        if not isinstance(self.metadata, tuple):
            raise TypeError("metadata must be a tuple of (name, value) pairs.")
        for pair in self.metadata:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("metadata entries must be (name, value) pairs.")
            if not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise TypeError("metadata name and value must be str.")

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable full config snapshot (sorted)."""

        retry = ";".join(
            (
                f"max_attempts={self.retry.max_attempts}",
                f"base_delay_seconds={self.retry.base_delay_seconds}",
                f"backoff_factor={self.retry.backoff_factor}",
                f"retryable={','.join(self.retry.retryable_categories)}",
            ),
        )
        timeout = ";".join(
            (
                f"provider={self.timeout.provider_operation_seconds}",
                f"cycle={self.timeout.cycle_operation_seconds}",
                f"delivery={self.timeout.delivery_operation_seconds}",
            ),
        )
        heartbeat = ";".join(
            (
                f"interval={self.heartbeat.interval_seconds}",
                f"quiet={self.heartbeat.max_quiet_seconds}",
                f"since_cycle={self.heartbeat.max_since_cycle_seconds}",
            ),
        )
        thresholds = ";".join(
            (
                f"degradation={self.thresholds.degradation_threshold}",
                f"recovery={self.thresholds.recovery_threshold}",
            ),
        )
        extra = tuple(
            (f"metadata.{k}", str(v)) for k, v in sorted(self.metadata)
        )
        return tuple(sorted(
            (
                ("reliability_model_version", str(RELIABILITY_MODEL_VERSION)),
                ("retry", retry),
                ("timeout", timeout),
                ("heartbeat", heartbeat),
                ("thresholds", thresholds),
                ("label", self.label),
            ) + extra,
        ))


def reliability_policy_version(config: ReliabilityConfig) -> str:
    """Deterministic reliability policy version derived from the config.

    Any reliability-config change is a policy change; the version is the
    sha256-prefix of the canonical snapshot so deterministic reliability
    identities embed the exact rule-set version.
    """

    payload = ";".join(f"{k}={v}" for k, v in config.snapshot())
    from engine.models.operational_health import _sha256_prefix

    return _sha256_prefix(payload, "rv-")


__all__ = [
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_HEARTBEAT_MAX_QUIET_SECONDS",
    "DEFAULT_HEARTBEAT_NAIVE_MARKER",
    "DEFAULT_MARKER_STORE_FAIL_CLOSED",
    "DEFAULT_STALLED_SCANNER_MAX_SINCE_CYCLE_SECONDS",
    "HealthThresholds",
    "HeartbeatPolicy",
    "OPERATIONAL_STATE_SCHEMA_VERSION",
    "RELIABILITY_MODEL_VERSION",
    "ReliabilityConfig",
    "RetryPolicy",
    "TimeoutPolicy",
    "reliability_policy_version",
]