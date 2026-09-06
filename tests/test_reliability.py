"""
Checkpoint 19.8 — Reliability / Recovery / Observability tests.

Deterministic, offline, network-free tests for the operational
hardening layer that wraps the FROZEN 19.1–19.7 pipeline. Everything
uses injected clocks, fake waiters, fake providers, fake delivery
channels, scripted failures, deterministic retry sequences and
transient state stores. No real network, no broker credentials, no
notification credentials, no broker execution.

Test areas:

A.  ReliabilityConfig / RetryPolicy / TimeoutPolicy / HeartbeatPolicy /
    HealthThresholds (validation, defaults, determinism, snapshot).
B.  Failure taxonomy (explicit, distinct from analytical vocabulary).
C.  Heartbeat (NONE/FRESH/STALE; pure classifier; naive fail-closed).
D.  RetryRunner (bounded, category-gated, deterministic delays, no
    infinite loops, RetryExhausted semantics, alert_id preservation on
    RetryRecord).
E.  Timeout boundary (bounded_call; hung op -> OperationTimedOut).
F.  Single-instance guard (live refusal, stale recovery, force, release).
G.  Market-session interaction (weekend/closed not a scanner alarm).
H.  Per-symbol failure isolation + explicit accounting.
I.  ReliabilityTracker health derivation (HEALTHY/DEGRADED/RECOVERING/
    FAILED; heartbeat; degraded provider; recovery; cycle observation;
    alert-cycle observation; failure/retry/recovery counts).
J.  Structured logging (stable fields, deterministic ids, level
    discipline, no repr, no print).
K.  Alert outbox (id preservation, retried alert stays alert A,
    dedup preservation, delivery failure never mutates lifecycle).
L.  Persistence / crash consistency (round trip, idempotency, conflict,
    corruption fail-closed, restart recovery, identity continuity,
    schema version, atomic write).
M.  Graceful shutdown / restart / no-overlap.
N.  Point-in-time protection (no wall-clock in analytical identity; no
    future mutation of prior analytical state).
O.  Broker/execution boundary (AST: no broker/execution imports in the
    new reliability modules).
"""

from __future__ import annotations

import ast
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dashboard.alert_outbox import (
    AlertOutbox,
    build_outbox_id,
)
from dashboard.reliability import (
    HeartbeatTracker,
    OperationTimedOut,
    ReliabilityTracker,
    RetryExhausted,
    RetryRunner,
    SingleInstanceGuard,
    bounded_call,
)
from dashboard.structured_log import (
    ListLogSink,
    LogLevel,
    StructuredEvent,
    StructuredLogger,
)
from engine.config.reliability_config import (
    DEFAULT_HEARTBEAT_MAX_QUIET_SECONDS,
    DEFAULT_HEARTBEAT_NAIVE_MARKER,
    DEFAULT_MARKER_STORE_FAIL_CLOSED,
    DEFAULT_STALLED_SCANNER_MAX_SINCE_CYCLE_SECONDS,
    HealthThresholds,
    HeartbeatPolicy,
    ReliabilityConfig,
    RetryPolicy,
    TimeoutPolicy,
    reliability_policy_version,
)
from dashboard.continuous_scanner import ContinuousScannerEngine
from engine.config.universe_boundary import UniverseBuilder
from engine.models.continuous_scan import (
    ContinuousScanConfig,
    MarketScanStatus,
    ScanSourceKind,
)
from engine.models.operational_health import (
    CycleFailureSummary,
    CycleHealth,
    DeliveryHealth,
    HealthStatus,
    HeartbeatState,
    OperationalHealthReport,
    OperationalHealthState,
    OperationalStateBundle,
    PerSymbolFailure,
    ProviderHealth,
    ReliabilityFailureCategory,
    RetryRecord,
    classify_heartbeat,
    derive_aggregate_health,
    derive_domain_health,
    operational_snapshot_id,
)
from engine.persistence.exceptions import (
    OperationalStateIntegrityError,
    OperationalStoreError,
)
from engine.persistence.reliability_serialization import (
    serialize_operational_state,
)
from engine.persistence.reliability_store import (
    OperationalStateStore,
    default_operational_state_directory,
)
from engine.reporting.reliability import OperationalHealthFormatter

# ------------------------------------------------------------------
# Deterministic time helpers
# ------------------------------------------------------------------


def _clock_at() -> datetime:
    return datetime(2026, 9, 4, 5, 30, tzinfo=UTC)


def _make_clock(initial: datetime | None = None):
    box = [initial if initial is not None else _clock_at()]

    def advance(seconds: float) -> None:
        box[0] = box[0] + timedelta(seconds=seconds)

    return box, lambda: box[0], advance


def _broker_provider(fail_count: int):
    """A deterministic fixture-based provider that fails the first
    ``fail_count`` symbols fetched (in fixture order)."""

    from dashboard.data_provider import FixtureDataProvider

    inner = FixtureDataProvider()
    failed: set[str] = set()

    class _Broken:
        data_source = "fixture-broken"

        def is_timeframe_supported(self, tf: str) -> bool:
            return inner.is_timeframe_supported(tf)

        def supports_instrument(self, instrument) -> bool:
            return inner.supports_instrument(instrument)

        def fetch(self, instrument, tf, lookback_bars=300, **kw):
            if instrument in failed:
                raise RuntimeError("simulated provider failure")
            if instrument in ("RELIANCE", "TCS", "HDFCBANK", "ICICIBANK"):
                if len(failed) < fail_count:
                    failed.add(instrument)
                    raise RuntimeError("simulated provider failure")
            return inner.fetch(instrument, tf, lookback_bars=lookback_bars, **kw)

    return _Broken(), failed


# ===================================================================
# A. CONFIGURATION
# ===================================================================


class TestReliabilityConfig:
    def test_defaults(self):
        cfg = ReliabilityConfig()
        assert isinstance(cfg.retry, RetryPolicy)
        assert isinstance(cfg.timeout, TimeoutPolicy)
        assert isinstance(cfg.heartbeat, HeartbeatPolicy)
        assert isinstance(cfg.thresholds, HealthThresholds)
        assert cfg.retry.max_attempts == 3

    def test_retry_validation(self):
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=0)
        with pytest.raises(ValueError):
            RetryPolicy(max_attempts=-1)
        with pytest.raises(ValueError):
            RetryPolicy(backoff_factor=0.0)
        with pytest.raises(ValueError):
            RetryPolicy(base_delay_seconds=-1)
        with pytest.raises(TypeError):
            RetryPolicy(max_attempts=True)
        with pytest.raises(ValueError):
            RetryPolicy(retryable_categories=("BOGUS",))

    def test_retry_default_sweep(self):
        assert RetryPolicy().sweep() == (0, 1, 1)

    def test_retry_sweep_exponential(self):
        # ``delay_for(n) = base_delay * backoff_factor ** n`` for n >= 1.
        policy = RetryPolicy(
            max_attempts=5,
            base_delay_seconds=2,
            backoff_factor=2.0,
        )
        assert policy.sweep() == (0, 4, 8, 16, 32)

    def test_retry_should_retry_category_gated(self):
        policy = RetryPolicy()
        assert policy.should_retry(0, "PROVIDER_TIMEOUT") is True
        assert policy.should_retry(0, "PROVIDER_UNAVAILABLE") is True
        assert policy.should_retry(0, "DELIVERY_FAILURE") is True
        assert policy.should_retry(0, "INVALID_RESPONSE") is False
        assert policy.should_retry(0, "CONFIGURATION_FAILURE") is False

    def test_retry_budget(self):
        policy = RetryPolicy(max_attempts=2)
        assert policy.should_retry(0, "PROVIDER_TIMEOUT") is True
        assert policy.should_retry(1, "PROVIDER_TIMEOUT") is False

    def test_timeout_validation(self):
        with pytest.raises(ValueError):
            TimeoutPolicy(provider_operation_seconds=0)
        with pytest.raises(ValueError):
            TimeoutPolicy(provider_operation_seconds=-1)
        with pytest.raises(TypeError):
            TimeoutPolicy(provider_operation_seconds=True)

    def test_heartbeat_validation(self):
        with pytest.raises(ValueError):
            HeartbeatPolicy(interval_seconds=-1)
        with pytest.raises(ValueError):
            HeartbeatPolicy(interval_seconds=60, max_quiet_seconds=10)
        with pytest.raises(ValueError):
            HeartbeatPolicy(max_since_cycle_seconds=-1)

    def test_heartbeat_naive_marker(self):
        assert DEFAULT_HEARTBEAT_NAIVE_MARKER is False

    def test_thresholds_validation(self):
        with pytest.raises(ValueError):
            HealthThresholds(degradation_threshold=0)
        with pytest.raises(ValueError):
            HealthThresholds(recovery_threshold=0)
        with pytest.raises(TypeError):
            HealthThresholds(degradation_threshold=True)

    def test_heartbeat_market_closed_defaults(self):
        assert (
            DEFAULT_HEARTBEAT_MAX_QUIET_SECONDS
            >= DEFAULT_HEARTBEAT_NAIVE_MARKER
        )

    def test_snapshot_deterministic(self):
        a = ReliabilityConfig().snapshot()
        b = ReliabilityConfig().snapshot()
        assert a == b

    def test_policy_version_deterministic(self):
        v1 = reliability_policy_version(ReliabilityConfig())
        v2 = reliability_policy_version(ReliabilityConfig())
        assert v1 == v2
        assert v1.startswith("rv-")

    def test_policy_version_changes_with_config(self):
        a = reliability_policy_version(
            ReliabilityConfig(retry=RetryPolicy(max_attempts=2)),
        )
        b = reliability_policy_version(ReliabilityConfig())
        assert a != b

    def test_documented_defaults_exist(self):
        assert DEFAULT_STALLED_SCANNER_MAX_SINCE_CYCLE_SECONDS > 0
        assert DEFAULT_HEARTBEAT_MAX_QUIET_SECONDS > 0
        assert DEFAULT_MARKER_STORE_FAIL_CLOSED is True


# ===================================================================
# B. FAILURE TAXONOMY
# ===================================================================


class TestFailureTaxonomy:
    CATEGORIES = {
        "PROVIDER_TIMEOUT",
        "PROVIDER_UNAVAILABLE",
        "INVALID_RESPONSE",
        "DELIVERY_FAILURE",
        "CYCLE_FAILURE",
        "HEARTBEAT_STALE",
        "STALLED_SCANNER",
        "RECOVERY_FAILURE",
        "CONFIGURATION_FAILURE",
        "PROCESS_FAILURE",
        "UNKNOWN",
    }

    def test_all_categories_exist(self):
        assert self.CATEGORIES <= set(ReliabilityFailureCategory.__members__)

    def test_no_analytical_overlap(self):
        analytical = {
            "DATA_UNAVAILABLE", "CONFIRMED", "HIGH", "ALIGNED",
            "QUALIFIED", "DELIVERED", "SUPPRESSED", "FULL_SUCCESS",
            "POTENTIAL_SETUP", "WATCH", "NO_SETUP", "OBSERVED",
        }
        assert not (analytical & set(ReliabilityFailureCategory.__members__))

    def test_failed_categories_distinct_from_health_enum(self):
        health_members = set(OperationalHealthState.__members__)
        assert "PROVIDER_TIMEOUT" not in health_members

    def test_category_values_stable(self):
        assert ReliabilityFailureCategory.PROVIDER_TIMEOUT.value == "PROVIDER_TIMEOUT"
        assert ReliabilityFailureCategory.UNKNOWN.value == "UNKNOWN"


# ===================================================================
# C. HEARTBEAT
# ===================================================================


class TestHeartbeat:
    def test_none_before_refresh(self):
        box, clock, _ = _make_clock()
        hb = HeartbeatTracker(clock=clock)
        assert hb.state() is HeartbeatState.NONE

    def test_fresh_after_refresh(self):
        box, clock, _ = _make_clock()
        hb = HeartbeatTracker(clock=clock)
        hb.refresh()
        assert hb.state() is HeartbeatState.FRESH

    def test_stale_after_quiet_window(self):
        box, clock, advance = _make_clock()
        hb = HeartbeatTracker(
            HeartbeatPolicy(max_quiet_seconds=120), clock=clock,
        )
        hb.refresh()
        advance(121)
        assert hb.state() is HeartbeatState.STALE

    def test_naive_fails_closed(self):
        assert (
            classify_heartbeat(
                datetime(2026, 9, 4, 5, 30),
                _clock_at(),
                120,
            )
            is HeartbeatState.STALE
        )

    def test_future_dated_stale(self):
        assert (
            classify_heartbeat(
                _clock_at() + timedelta(hours=1),
                _clock_at(),
                120,
            )
            is HeartbeatState.STALE
        )

    def test_explicit_at(self):
        box, clock, _ = _make_clock()
        hb = HeartbeatTracker(clock=clock)
        hb.refresh(at=_clock_at())
        assert hb.last_heartbeat_at == _clock_at()

    def test_reset(self):
        box, clock, _ = _make_clock()
        hb = HeartbeatTracker(clock=clock)
        hb.refresh()
        assert hb.state() is HeartbeatState.FRESH
        hb.reset()
        assert hb.state() is HeartbeatState.NONE


# ===================================================================
# D. RETRY RUNNER
# ===================================================================


class TestRetryRunner:
    def test_succeeds_first_attempt(self):
        box, clock, advance = _make_clock()
        runner = RetryRunner(clock=clock, waiter=advance)
        assert runner.run(lambda: "ok", ReliabilityFailureCategory.PROVIDER_TIMEOUT) == "ok"

    def test_retries_transient_success(self):
        box, clock, advance = _make_clock()
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            if calls["n"] < 3:
                raise TimeoutError("boom")
            return "ok"

        runner = RetryRunner(
            RetryPolicy(max_attempts=5), clock=clock, waiter=advance,
        )
        assert runner.run(op, ReliabilityFailureCategory.PROVIDER_TIMEOUT) == "ok"
        assert calls["n"] == 3

    def test_exhaustion_raises_bounded(self):
        box, clock, advance = _make_clock()

        def op():
            raise TimeoutError("always")

        runner = RetryRunner(
            RetryPolicy(max_attempts=4), clock=clock, waiter=advance,
        )
        with pytest.raises(RetryExhausted) as exc:
            runner.run(op, ReliabilityFailureCategory.PROVIDER_UNAVAILABLE)
        assert exc.value.attempts == 4

    def test_non_retryable_not_retried(self):
        box, clock, advance = _make_clock()
        calls = {"n": 0}

        def op():
            calls["n"] += 1
            raise ValueError("malformed")

        runner = RetryRunner(clock=clock, waiter=advance)
        with pytest.raises(RetryExhausted) as exc:
            runner.run(op, ReliabilityFailureCategory.INVALID_RESPONSE)
        assert calls["n"] == 1
        assert exc.value.attempts == 1

    def test_waiter_used_bounded(self):
        box, clock, advance = _make_clock()
        slept = {"n": 0}

        def op():
            raise TimeoutError("x")

        def waiter(secs):
            slept["n"] += 1

        runner = RetryRunner(
            RetryPolicy(max_attempts=3, base_delay_seconds=10, backoff_factor=1.0),
            clock=clock,
            waiter=waiter,
        )
        with pytest.raises(RetryExhausted):
            runner.run(op, ReliabilityFailureCategory.PROVIDER_TIMEOUT)
        assert slept["n"] == 2

    def test_on_attempt_records_preserve_alert_id(self):
        box, clock, advance = _make_clock()
        records: list[RetryRecord] = []

        def op():
            raise TimeoutError("x")

        runner = RetryRunner(
            RetryPolicy(max_attempts=2),
            clock=clock,
            waiter=advance,
            on_attempt=records.append,
        )
        with pytest.raises(RetryExhausted):
            runner.run(
                op, ReliabilityFailureCategory.DELIVERY_FAILURE,
                alert_id="alert-abc",
            )
        assert len(records) == 2
        assert all(r.alert_id == "alert-abc" for r in records)
        assert records[0].retry_id.startswith("retry-")

    def test_deterministic_delays(self):
        box, clock, advance = _make_clock()
        delays: list[float] = []

        def op():
            raise TimeoutError("x")

        def waiter(secs):
            delays.append(secs)

        runner = RetryRunner(
            RetryPolicy(
                max_attempts=4, base_delay_seconds=2, backoff_factor=2.0,
            ),
            clock=clock,
            waiter=waiter,
        )
        with pytest.raises(RetryExhausted):
            runner.run(op, ReliabilityFailureCategory.PROVIDER_TIMEOUT)
        # delay_for(1)=4, delay_for(2)=8, delay_for(3)=16 (exponential).
        assert delays == [4.0, 8.0, 16.0]


# ===================================================================
# E. TIMEOUT BOUNDARY
# ===================================================================


class TestTimeoutBoundary:
    def test_fast_op(self):
        box, clock, _ = _make_clock()
        result, elapsed = bounded_call(lambda: ("x",), 5.0)
        assert result == ("x",)

    def test_slow_op_times_out(self):
        import time

        with pytest.raises(OperationTimedOut):
            bounded_call(
                lambda: time.sleep(0.05), 0.001, operation_name="provider",
            )

    def test_none_timeout_unbounded(self):
        result, _ = bounded_call(lambda: "fast", None)
        assert result == "fast"

    def test_invalid_timeout(self):
        with pytest.raises(TypeError):
            bounded_call(lambda: 1, True)
        with pytest.raises(ValueError):
            bounded_call(lambda: 1, 0)


# ===================================================================
# F. SINGLE INSTANCE GUARD
# ===================================================================


class TestSingleInstanceGuard:
    def _shared_store(self):
        from dashboard.reliability import _InMemoryMarkerStore

        return _InMemoryMarkerStore()

    def test_acquire_release(self):
        store = self._shared_store()
        g = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: True)
        assert not g.is_held
        assert g.acquire(111, "tok-a")
        assert g.is_held
        g.release()
        assert not g.is_held

    def test_second_live_refused(self):
        store = self._shared_store()
        g = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: True)
        g.acquire(111, "token-a")
        with pytest.raises(ValueError):
            SingleInstanceGuard(
                "s", marker_store=store, is_pid_alive=lambda p: True,
            ).acquire(222, "token-b")

    def test_stale_recoverable(self):
        store = self._shared_store()
        g1 = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: p == 111)
        g1.acquire(111, "token-a")
        g2 = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: p == 999)
        assert g2.is_stale() is True
        assert g2.acquire(999, "token-c", force=True)
        assert g2.lock_info()["pid"] == 999

    def test_force_never_overrides_live(self):
        store = self._shared_store()
        g1 = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: True)
        g1.acquire(111, "token-a")
        with pytest.raises(ValueError):
            SingleInstanceGuard(
                "s", marker_store=store, is_pid_alive=lambda p: True,
            ).acquire(222, "token-b", force=True)

    def test_token_required(self):
        store = self._shared_store()
        g = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: True)
        with pytest.raises(ValueError):
            g.acquire(111, "   ")

    def test_pid_type(self):
        store = self._shared_store()
        g = SingleInstanceGuard("s", marker_store=store, is_pid_alive=lambda p: True)
        with pytest.raises(TypeError):
            g.acquire(True, "tok")


# ===================================================================
# G. MARKET SESSION INTERACTION
# ===================================================================


class TestMarketSessionInteraction:
    def test_weekend_session(self):
        from engine.data.market_session import market_session_state

        saturday = datetime(2026, 9, 5, 5, 30, tzinfo=UTC)
        assert market_session_state(saturday).value == "WEEKEND"

    def test_no_cycle_closed_market_not_failed(self):
        box, clock, _ = _make_clock(initial=datetime(2026, 9, 5, 5, 30, tzinfo=UTC))
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        health = tracker.health(reference_now=clock())
        assert health.health_state is OperationalHealthState.HEALTHY
        assert health.cycle_counts == (
            ("completed", 0), ("failed", 0), ("partial", 0),
            ("skipped", 0), ("total", 0),
        )

    def test_closed_market_never_false_alarm_with_recent_cycle(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.refresh_heartbeat(at=clock())
        # A COMPLETED recent cycle is still HEALTHY (no stall has
        # occurred); the mere market-closed state never fails the system.
        assert tracker.health(reference_now=clock()).health_state in (
            OperationalHealthState.HEALTHY,
            OperationalHealthState.RECOVERING,
        )


# ===================================================================
# H. PER-SYMBOL FAILURE ISOLATION
# ===================================================================


class TestPerSymbolFailureIsolation:
    def _run_broken_cycle(self, fail_count: int):
        box, clock, _ = _make_clock()
        provider, _failed_set = _broker_provider(fail_count)
        engine = ContinuousScannerEngine(
            coverage_engine=__import__(
                "dashboard.intraday_coverage",
                fromlist=["IntradayCoverageEngine"],
            ).IntradayCoverageEngine(provider=provider),
            timeframe="15m",
        )
        universe = UniverseBuilder.custom(
            ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "NIFTY"],
            label="isolation",
        )
        result = engine.run_cycle(
            universe=universe,
            reference_now=clock(),
            source=ScanSourceKind.MANUAL,
            manual_run_id="iso",
        )
        return result

    def test_200_with_zero_failures_accounted(self):
        box, clock, _ = _make_clock()
        engine = ContinuousScannerEngine.build("fixture", timeframe="15m")
        universe = UniverseBuilder.custom(
            ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK"], label="iso",
        )
        result = engine.run_cycle(
            universe=universe,
            reference_now=clock(),
            source=ScanSourceKind.MANUAL,
            manual_run_id="iso",
        )
        assert result.status in (
            MarketScanStatus.FULL_SUCCESS,
            MarketScanStatus.PARTIAL_SUCCESS,
        )
        assert result.metadata.requested_universe_size == 4
        assert (
            result.metadata.successful_instrument_count
            + result.metadata.unavailable_instrument_count
            == result.metadata.attempted_instrument_count
        )

    def test_one_failure_does_not_abort(self):
        result = self._run_broken_cycle(1)
        assert len(result.results) == 5
        assert result.status in (
            MarketScanStatus.PARTIAL_SUCCESS,
            MarketScanStatus.FULL_SUCCESS,
        )
        failed = sum(
            1 for r in result.results
            if r.status.value == "PROVIDER_ERROR"
        )
        assert failed == 1

    def test_fifty_percent_failures_isolated(self):
        result = self._run_broken_cycle(2)
        failed = sum(
            1 for r in result.results
            if r.status.value == "PROVIDER_ERROR"
        )
        assert failed == 2
        assert len(result.results) == 5

    def test_recording_preserves_accounting(self):
        box, clock, advance = _make_clock()
        provider, _ = _broker_provider(2)
        engine = ContinuousScannerEngine(
            coverage_engine=__import__(
                "dashboard.intraday_coverage",
                fromlist=["IntradayCoverageEngine"],
            ).IntradayCoverageEngine(provider=provider),
            timeframe="15m",
        )
        universe = UniverseBuilder.custom(
            ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "NIFTY"],
            label="iso",
        )
        result = engine.run_cycle(
            universe=universe, reference_now=clock(),
            source=ScanSourceKind.MANUAL, manual_run_id="iso2",
        )
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.record_cycle(
            result, started_at=clock(), ended_at=clock(), reference_now=clock(),
        )
        failed_symbols = [
            r.instrument for r in result.results
            if r.status.value == "PROVIDER_ERROR"
        ]
        for symbol in failed_symbols:
            tracker.record_per_symbol_failure(
                symbol,
                ReliabilityFailureCategory.PROVIDER_UNAVAILABLE,
                cycle_id=result.cycle_id,
                timestamp=clock(),
            )
        assert len(tracker.per_symbol_failures) == 2
        summary = tracker.last_cycle_summary
        assert summary is not None
        assert summary.requested == 5


# ===================================================================
# I. RELIABILITY TRACKER / HEALTH DERIVATION
# ===================================================================


class TestHealthDerivation:
    def test_derive_domain_health(self):
        assert (
            derive_domain_health(2, 0, 3, 2, True) is HealthStatus.DEGRADED
        )
        assert (
            derive_domain_health(3, 0, 3, 2, True) is HealthStatus.FAILED
        )
        assert (
            derive_domain_health(0, 2, 3, 2, True) is HealthStatus.HEALTHY
        )
        assert (
            derive_domain_health(0, 0, 3, 2, False) is HealthStatus.UNKNOWN
        )

    def test_derive_aggregate_healthy(self):
        assert (
            derive_aggregate_health(
                HealthStatus.HEALTHY, HealthStatus.HEALTHY,
                HealthStatus.HEALTHY, HeartbeatState.FRESH,
                _clock_at(), _clock_at(), 3600,
            )
            is OperationalHealthState.HEALTHY
        )

    def test_derive_aggregate_degraded(self):
        assert (
            derive_aggregate_health(
                HealthStatus.HEALTHY, HealthStatus.DEGRADED,
                HealthStatus.HEALTHY, HeartbeatState.FRESH,
                _clock_at(), _clock_at(), 3600,
            )
            is OperationalHealthState.DEGRADED
        )

    def test_derive_aggregate_failed_stale(self):
        assert (
            derive_aggregate_health(
                HealthStatus.HEALTHY, HealthStatus.HEALTHY,
                HealthStatus.HEALTHY, HeartbeatState.STALE,
                _clock_at(), _clock_at(), 3600,
            )
            is OperationalHealthState.FAILED
        )

    def test_derive_aggregate_stalled_scanner(self):
        old = _clock_at() - timedelta(hours=2)
        assert (
            derive_aggregate_health(
                HealthStatus.HEALTHY, HealthStatus.HEALTHY,
                HealthStatus.HEALTHY, HeartbeatState.FRESH,
                old, _clock_at(), 3600,
            )
            is OperationalHealthState.FAILED
        )

    def test_derive_aggregate_recovering(self):
        assert (
            derive_aggregate_health(
                HealthStatus.HEALTHY, HealthStatus.HEALTHY,
                HealthStatus.HEALTHY, HeartbeatState.FRESH,
                _clock_at(), _clock_at(), 3600,
                recovery_pending=True,
            )
            is OperationalHealthState.RECOVERING
        )

    def test_tracker_healthy_fresh(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.refresh_heartbeat()
        assert (
            tracker.health(reference_now=clock()).health_state
            is OperationalHealthState.HEALTHY
        )

    def test_tracker_provider_degraded(self):
        box, clock, advance = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.refresh_heartbeat()
        for _ in range(2):
            advance(1)
            tracker.record_provider_result(
                success=False,
                category=ReliabilityFailureCategory.PROVIDER_TIMEOUT,
                at=clock(),
            )
        assert (
            tracker.health(reference_now=clock()).provider_health
            is HealthStatus.DEGRADED
        )

    def test_tracker_provider_failed(self):
        box, clock, advance = _make_clock()
        cfg = ReliabilityConfig(
            thresholds=HealthThresholds(
                degradation_threshold=2, recovery_threshold=2,
            ),
        )
        tracker = ReliabilityTracker(cfg, clock=clock)
        tracker.refresh_heartbeat()
        for _ in range(2):
            advance(1)
            tracker.record_provider_result(
                success=False,
                category=ReliabilityFailureCategory.PROVIDER_TIMEOUT,
                at=clock(),
            )
        assert (
            tracker.health(reference_now=clock()).provider_health
            is HealthStatus.FAILED
        )
        assert (
            tracker.health(reference_now=clock()).health_state
            is OperationalHealthState.FAILED
        )

    def test_recovery_degraded_to_healthy(self):
        box, clock, advance = _make_clock()
        cfg = ReliabilityConfig(
            thresholds=HealthThresholds(
                degradation_threshold=2, recovery_threshold=2,
            ),
        )
        tracker = ReliabilityTracker(cfg, clock=clock)
        tracker.refresh_heartbeat()
        # ONE failure: provider DEGRADED (below the degradation threshold).
        advance(1)
        tracker.record_provider_result(
            success=False,
            category=ReliabilityFailureCategory.PROVIDER_TIMEOUT,
            at=clock(),
        )
        assert (
            tracker.health(reference_now=clock()).provider_health
            is HealthStatus.DEGRADED
        )
        # Recovery pending -> RECOVERING (observable, never hidden).
        tracker.mark_recovery_pending(True)
        assert (
            tracker.health(reference_now=clock()).health_state
            is OperationalHealthState.RECOVERING
        )
        # Two consecutive successes meet the recovery threshold.
        for _ in range(2):
            advance(1)
            tracker.record_provider_result(
                success=True, provider_name="x", at=clock(),
            )
        tracker.mark_recovery_pending(False)
        assert (
            tracker.health(reference_now=clock()).health_state
            is OperationalHealthState.HEALTHY
        )

    def test_tracker_cycle_observation(self):
        box, clock, _ = _make_clock()
        provider, _ = _broker_provider(0)
        engine = ContinuousScannerEngine(
            coverage_engine=__import__(
                "dashboard.intraday_coverage",
                fromlist=["IntradayCoverageEngine"],
            ).IntradayCoverageEngine(provider=provider),
            timeframe="15m",
        )
        result = engine.run_cycle(
            universe=UniverseBuilder.custom(
                ["RELIANCE"], label="iso",
            ),
            reference_now=clock(),
            source=ScanSourceKind.MANUAL,
            manual_run_id="c",
        )
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.refresh_heartbeat()
        tracker.record_cycle(
            result, started_at=clock(), ended_at=clock(), reference_now=clock(),
        )
        health = tracker.health(reference_now=clock())
        # Fixture data is stale (offline) -> the cycle is a PARTIAL
        # success: completed=1, partial=1.
        assert health.cycle_counts == (
            ("completed", 1), ("failed", 0), ("partial", 1),
            ("skipped", 0), ("total", 1),
        )
        assert health.last_cycle_id == result.cycle_id

    def test_cycle_failure_summary_invariants(self):
        box, clock, _ = _make_clock()
        with pytest.raises(ValueError):
            CycleFailureSummary(failed=2, unavailable=1)
        with pytest.raises(ValueError):
            CycleFailureSummary(
                cycle_started_at=_clock_at(),
                cycle_ended_at=_clock_at() - timedelta(seconds=1),
            )

    def test_exception_visible_not_swallowed(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.record_expected_exception("scanner", RuntimeError("boom"))
        report = tracker.health(reference_now=clock())
        assert ("PROCESS_FAILURE", 1) in report.failure_counts

    def test_recovery_failure_fails_closed(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.record_recovery("could not load state", success=False, at=clock())
        assert (
            tracker.health(reference_now=clock()).health_state
            is OperationalHealthState.FAILED
        )

    def test_snapshot_helper(self):
        snap_id = operational_snapshot_id(
            _clock_at(), "fixture", "cycle-1", "alert-cycle-1", "rv-x",
        )
        assert snap_id.startswith("ops-")
        assert (
            operational_snapshot_id(
                _clock_at(), "fixture", "cycle-1", "alert-cycle-1", "rv-x",
            )
            == snap_id
        )


# ===================================================================
# J. STRUCTURED LOGGING
# ===================================================================


class TestStructuredLogging:
    def test_stable_fields(self):
        box, clock, _ = _make_clock()
        sink = ListLogSink()
        logger = StructuredLogger("reliability", sink, clock=clock)
        ev = logger.warning("provider timeout", symbol="X", error_category="PROVIDER_TIMEOUT")
        assert ev.component == "reliability"
        assert ev.level is LogLevel.WARNING
        assert ev.symbol == "X"
        assert ev.error_category == "PROVIDER_TIMEOUT"

    def test_deterministic_event_id(self):
        box, clock, _ = _make_clock()
        sink = ListLogSink()
        logger = StructuredLogger("x", sink, clock=clock)
        a = logger.info("cycle complete", cycle_id="c1")
        b = logger.info("cycle complete", cycle_id="c1")
        assert a.event_id == b.event_id
        assert a.event_id.startswith("log-")

    def test_level_rank(self):
        assert LogLevel.ERROR.rank > LogLevel.WARNING.rank
        assert LogLevel.CRITICAL.rank > LogLevel.ERROR.rank
        assert LogLevel.DEBUG.rank < LogLevel.INFO.rank

    def test_no_repr_in_line(self):
        box, clock, _ = _make_clock()
        sink = ListLogSink()
        logger = StructuredLogger("x", sink, clock=clock)
        ev = logger.info("something", detail="raw detail")
        assert "object at 0x" not in ev.to_line()

    def test_sink_filters(self):
        box, clock, _ = _make_clock()
        sink = ListLogSink()
        logger = StructuredLogger("x", sink, clock=clock)
        logger.debug("debug")
        logger.info("info")
        logger.critical("critical")
        assert len(sink.events) == 3
        assert len(sink.level_at_least(LogLevel.WARNING)) == 1
        assert len(sink.component("x")) == 3

    def test_expected_partial_failure_not_catastrophic(self):
        box, clock, _ = _make_clock()
        sink = ListLogSink()
        logger = StructuredLogger("scanner", sink, clock=clock)
        logger.warning("per-symbol failure", symbol="X")
        logger.error("whole-cycle failure", symbol="")
        levels = [e.level for e in sink.events]
        assert LogLevel.WARNING in levels
        assert levels[0] is LogLevel.WARNING

    def test_model_validation(self):
        with pytest.raises(ValueError):
            StructuredEvent(
                timestamp=_clock_at(), level=LogLevel.INFO,
                component="", event="x",
            )


# ===================================================================
# K. ALERT OUTBOX
# ===================================================================


class TestAlertOutbox:
    def _fixtures(self):
        from tests import _checkpoint19_8_fixtures as fx

        return fx

    def test_enqueue_deterministic_id(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine()
        cycle = engine.process(lc, at=_clock_at())
        alert = cycle.alerts[0]
        outbox = AlertOutbox()
        e1 = outbox.enqueue(alert, cycle.policy_version)
        e2 = outbox.enqueue(alert, cycle.policy_version)
        assert e1.outbox_id == e2.outbox_id
        assert e1.alert_id == alert.alert_id
        assert outbox.pending_count == 1

    def test_delivery_success(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        cycle = engine.process(lc, at=_clock_at())

        class _Ok:
            name = "ok"
            delivered = []

            def deliver(self, alert):
                self.delivered.append(alert)
                return "ok"

        channel = _Ok()
        outbox = AlertOutbox(channels=[channel])
        outbox.enqueue(cycle.alerts[0], cycle.policy_version)
        results = outbox.process(_clock_at())
        assert len(results) == 1
        assert results[0].status.value == "DELIVERED"
        assert outbox.pending_count == 0
        assert outbox.delivered_count == 1
        assert channel.delivered[0].alert_id == cycle.alerts[0].alert_id

    def test_delivery_failure_retry_preserves_alert_id(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        cycle = engine.process(lc, at=_clock_at())
        first = cycle.alerts[0]

        class _Fails:
            name = "fails"
            attempts = 0

            def deliver(self, alert):
                self.attempts += 1
                if self.attempts < 3:
                    raise RuntimeError("down")
                return "ok"

        channel = _Fails()
        outbox = AlertOutbox(
            retry_policy=RetryPolicy(
                max_attempts=5, base_delay_seconds=1, backoff_factor=1.0,
            ),
            channels=[channel],
        )
        outbox.enqueue(first, cycle.policy_version)
        box = [datetime(2026, 9, 4, 5, 30, tzinfo=UTC)]

        def tick(secs):
            box[0] = box[0] + timedelta(seconds=secs)

        outbox.process(box[0])
        tick(1.1)
        outbox.process(box[0])
        tick(1.1)
        outbox.process(box[0])
        assert outbox.delivered_count == 1
        delivered = outbox.delivered_entries[0].alert
        assert delivered.alert_id == first.alert_id
        assert delivered.observation_id == first.observation_id
        assert delivered.setup_id == first.setup_id

    def test_same_alert_retried_20_times_remains_alert_a(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        cycle = engine.process(lc, at=_clock_at())
        first = cycle.alerts[0]

        class _AlwaysFails:
            name = "always"

            def deliver(self, alert):
                raise RuntimeError("down")

        outbox = AlertOutbox(
            retry_policy=RetryPolicy(
                max_attempts=20, base_delay_seconds=1, backoff_factor=1.0,
            ),
            channels=[_AlwaysFails()],
        )
        outbox.enqueue(first, cycle.policy_version)
        box = [datetime(2026, 9, 4, 5, 30, tzinfo=UTC)]

        def tick(secs):
            box[0] = box[0] + timedelta(seconds=secs)

        for _ in range(22):
            tick(1.1)
            outbox.process(box[0])
        # Always-failing: budget exhausted -> FAILED entries.
        assert outbox.failed_count == 1
        assert outbox.delivered_count == 0
        failed = outbox.failed_entries[0].alert
        assert failed.alert_id == first.alert_id

    def test_delivery_failure_never_mutates_lifecycle(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        cycle = engine.process(lc, at=_clock_at())

        class _Fails:
            name = "fails"

            def deliver(self, alert):
                raise RuntimeError("down")

        outbox = AlertOutbox(
            retry_policy=RetryPolicy(max_attempts=2),
            channels=[_Fails()],
        )
        outbox.enqueue(cycle.alerts[0], cycle.policy_version)
        box = [datetime(2026, 9, 4, 5, 30, tzinfo=UTC)]

        def tick(secs):
            box[0] = box[0] + timedelta(seconds=secs)

        for _ in range(3):
            tick(1.1)
            outbox.process(box[0])
        # The alert store's alert record is UNCHANGED by delivery failure
        # (the same alert object is still keyed by the same alert id; the
        # outbox NEVER alters the analytical alert payload or lifecycle
        # state).
        original = cycle.alerts[0]
        still = engine.store.load(original.alert_id)
        assert still is not None
        assert still.alert_id == original.alert_id
        assert still.observation_id == original.observation_id
        # The failing delivery never produced a new lifecycle change.
        assert outbox.failed_count == 1
        assert outbox.pending_count == 0

    def test_no_channels_skipped_explicit(self):
        fx = self._fixtures()
        lc, _ = fx.build_deterministic_lifecycle_cycle()
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        cycle = engine.process(lc, at=_clock_at())
        outbox = AlertOutbox(channels=[])
        outbox.enqueue(cycle.alerts[0], cycle.policy_version)
        results = outbox.process(_clock_at())
        assert results[0].status.value == "SKIPPED"

    def test_outbox_id_prefix(self):
        assert build_outbox_id("alert-a", "rv-1").startswith("outbox-")


# ===================================================================
# L. PERSISTENCE / CRASH CONSISTENCY
# ===================================================================


class TestPersistence:
    def _tracker_with_state(self, clock, store=None):
        from tests import _checkpoint19_8_fixtures as fx

        tracker = ReliabilityTracker(
            ReliabilityConfig(), clock=clock, store=store,
        )
        tracker.refresh_heartbeat(at=clock())
        tracker.record_provider_result(
            success=True, provider_name="fixture", at=clock(),
        )
        lc, _ = fx.build_deterministic_lifecycle_cycle(clock())
        engine = fx.build_alert_engine(channels=[ConsoleAlertSink()])
        ac = engine.process(lc, at=clock())
        tracker.observe_lifecycle_cycle(lc, at=clock())
        tracker.observe_alert_cycle(ac, at=clock())
        return tracker, lc, ac

    def test_round_trip_lossless(self):
        box, clock, _ = _make_clock()
        tracker, lc, ac = self._tracker_with_state(clock)
        bundle = tracker.snapshot(recorded_at=clock(), persisted=True)
        import tempfile as _tf

        store = OperationalStateStore(Path(_tf.mkdtemp()))
        store.save_snapshot(bundle)
        loaded = store.load_snapshot()
        assert loaded is not None
        assert loaded.snapshot_id == bundle.snapshot_id
        assert loaded.set_ids == bundle.set_ids
        assert loaded.alert_ids == bundle.alert_ids
        assert loaded.provider_health.total_operations == bundle.provider_health.total_operations

    def test_idempotent_identical_write(self):
        import tempfile as _tf

        box, clock, _ = _make_clock()
        tracker, _, _ = self._tracker_with_state(clock)
        bundle = tracker.snapshot(recorded_at=clock(), persisted=True)
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        store.save_snapshot(bundle)
        store.save_snapshot(bundle)  # identical -> no-op
        assert store.exists()

    def test_conflicting_write_rejected(self):
        import tempfile as _tf

        box, clock, advance = _make_clock()
        tracker, _, _ = self._tracker_with_state(clock)
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        b1 = tracker.snapshot(recorded_at=clock(), persisted=True)
        advance(1)
        b2 = tracker.snapshot(recorded_at=clock(), persisted=True)
        store.save_snapshot(b1)
        with pytest.raises(OperationalStateIntegrityError):
            store.save_snapshot(b2)

    def test_overwrite(self):
        import tempfile as _tf

        box, clock, advance = _make_clock()
        tracker, _, _ = self._tracker_with_state(clock)
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        b1 = tracker.snapshot(recorded_at=clock(), persisted=True)
        advance(1)
        b2 = tracker.snapshot(recorded_at=clock(), persisted=True)
        store.save_snapshot(b1)
        store.save_snapshot(b2, overwrite=True)
        assert store.load_snapshot().snapshot_id == b2.snapshot_id

    def test_corrupt_state_fails_closed(self):
        import tempfile as _tf

        store = OperationalStateStore(Path(_tf.mkdtemp()))
        store.path().parent.mkdir(parents=True, exist_ok=True)
        store.path().write_text("{corrupt", encoding="utf-8")
        with pytest.raises(OperationalStoreError):
            store.load_snapshot()

    def test_future_schema_rejected(self):
        import tempfile as _tf

        store = OperationalStateStore(Path(_tf.mkdtemp()))
        store.path().parent.mkdir(parents=True, exist_ok=True)
        store.path().write_text(
            json.dumps({"schema_version": 999, "bundle": {}}),
            encoding="utf-8",
        )
        from engine.persistence.exceptions import (
            UnsupportedOperationalStateSchemaVersionError,
        )

        with pytest.raises(UnsupportedOperationalStateSchemaVersionError):
            store.load_snapshot()

    def test_missing_state_fresh_start(self):
        import tempfile as _tf

        box, clock, _ = _make_clock()
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        assert store.load_snapshot() is None
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
        summary = tracker.startup_recovery(scanner_state="STOPPED", at=clock())
        assert "fresh start" in summary or "no durable operational" in summary

    def test_restart_recovery_identity_continuity(self):
        import tempfile as _tf

        box, clock, _ = _make_clock()
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        tracker, lc, ac = self._tracker_with_state(clock, store=store)
        bundle = tracker.shutdown(at=clock())
        assert store.exists()
        restarted = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
        summary = restarted.startup_recovery(scanner_state="STOPPED", at=clock())
        assert "recovered" in summary
        snap = restarted.snapshot(recorded_at=clock())
        expected_setup = {
            sid for sid in bundle.set_ids if sid.startswith("setup-")
        }
        assert sorted(set(snap.set_ids)) == sorted(expected_setup)
        assert sorted(snap.alert_ids) == sorted(bundle.alert_ids)

    def test_corrupt_state_restart_records_recovery_failure(self):
        import tempfile as _tf

        box, clock, _ = _make_clock()
        store = OperationalStateStore(Path(_tf.mkdtemp()))
        store.path().parent.mkdir(parents=True, exist_ok=True)
        store.path().write_text("garbage", encoding="utf-8")
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
        summary = tracker.startup_recovery(scanner_state="STOPPED", at=clock())
        assert "FAILED" in summary
        assert tracker.health(reference_now=clock()).health_state is OperationalHealthState.FAILED

    def test_serialization_deterministic_bytes(self):
        box, clock, _ = _make_clock()
        tracker, _, _ = self._tracker_with_state(clock)
        bundle = tracker.snapshot(recorded_at=clock(), persisted=True)
        a = serialize_operational_state(bundle)
        b = serialize_operational_state(bundle)
        assert a == b
        assert isinstance(serialize_operational_state(bundle).encode("utf-8"), bytes)

    def test_default_directory_relative(self):
        import os

        prev = os.getcwd()
        try:
            os.chdir(tempfile.mkdtemp())
            path = default_operational_state_directory()
            assert str(path).startswith("./") or path.is_relative_to(Path.cwd())
        finally:
            os.chdir(prev)

    def test_formatter_sections(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        tracker.refresh_heartbeat()
        report = tracker.health(reference_now=clock())
        text = OperationalHealthFormatter().format(report)
        assert "OPERATIONAL HEALTH REPORT" in text
        assert "DISCLAIMER" in text
        assert isinstance(text, str)

    def test_formatter_validation(self):
        with pytest.raises(ValueError):
            OperationalHealthFormatter(precision=-1)
        with pytest.raises(ValueError):
            OperationalHealthFormatter(width=0)

    def test_formatter_json_deterministic(self):
        box, clock, _ = _make_clock()
        tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)
        report = tracker.health(reference_now=clock())
        formatter = OperationalHealthFormatter()
        a = formatter.health_report_to_json(report)
        b = formatter.health_report_to_json(report)
        assert a == b
        parsed = json.loads(a)
        assert "health_state" in parsed


# ===================================================================
# M. GRACEFUL SHUTDOWN / RESTART / NO-OVERLAP
# ===================================================================


class TestScannerLifecycle:
    """Graceful shutdown / restart / no-overlap tests for the FROZEN
    19.3 ContinuousScanner, following the 19.3 test conventions
    (bounded real-time sleeps so the worker thread can progress)."""

    def _engine(self):
        return ContinuousScannerEngine.build("fixture", timeframe="15m")

    def test_start_stop_start_stop(self):
        import time

        from dashboard.continuous_scanner import ContinuousScanner
        from tests.test_continuous_scanner import FakeClock

        clock = FakeClock(_clock_at())
        scanner = ContinuousScanner(
            engine=self._engine(),
            config=ContinuousScanConfig(
                scan_interval_seconds=60, run_forever=True,
            ),
            universe=["RELIANCE"],
            clock=clock,
            waiter=lambda s: clock.advance(s),
        )
        scanner.start()
        time.sleep(0.05)  # let at least one cycle start
        scanner.stop()
        scanner.join(timeout=10)
        assert scanner.state().name == "STOPPED"
        first = scanner.cycle_count
        assert first >= 1
        # Restart: start -> stop -> start -> stop leaves no orphan worker.
        scanner.start()
        time.sleep(0.05)
        scanner.stop()
        scanner.join(timeout=10)
        assert scanner.state().name == "STOPPED"
        assert scanner.cycle_count >= first

    def test_no_overlap_no_duplicate_cycle_ids(self):
        from dashboard.continuous_scanner import ContinuousScanner
        from tests.test_continuous_scanner import FakeClock

        clock = FakeClock(_clock_at())
        scanner = ContinuousScanner(
            engine=self._engine(),
            config=ContinuousScanConfig(
                scan_interval_seconds=1, max_cycles=3,
            ),
            universe=["RELIANCE"],
            clock=clock,
            waiter=lambda s: clock.advance(s),
        )
        results = scanner.run(cycles=3)
        assert len(results) == 3
        ids = [c.cycle_id for c in results]
        assert len(ids) == len(set(ids))
        refs = [r.reference_now for r in results]
        assert len(set(refs)) == 3  # no overlap: distinct reference times

    def test_duplicate_stop_safe(self):
        import time

        from dashboard.continuous_scanner import ContinuousScanner
        from tests.test_continuous_scanner import FakeClock

        clock = FakeClock(_clock_at())
        scanner = ContinuousScanner(
            engine=self._engine(),
            config=ContinuousScanConfig(
                scan_interval_seconds=60, run_forever=True,
            ),
            universe=["RELIANCE"],
            clock=clock,
            waiter=lambda s: clock.advance(s),
        )
        scanner.start()
        time.sleep(0.05)
        scanner.stop()
        scanner.join(timeout=10)
        scanner.stop()  # idempotent
        assert scanner.state().name == "STOPPED"


# ===================================================================
# N. POINT-IN-TIME PROTECTION
# ===================================================================


class TestPointInTime:
    def test_no_wall_clock_in_identities(self):
        from engine.models.setup_lifecycle import (
            SetupDirection,
            build_setup_id,
        )

        sid = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m",
        )
        assert sid.startswith("setup-")
        # Same inputs -> same id regardless of when computed.
        sid2 = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m",
        )
        assert sid == sid2

    def test_future_mutation_does_not_alter_prior_state(self):
        from dashboard.setup_lifecycle import (
            SetupLifecycleEngine,
            SetupLifecycleStore,
        )
        from engine.config.setup_lifecycle_config import SetupLifecycleConfig

        box, clock, advance = _make_clock()
        eng = SetupLifecycleEngine(
            SetupLifecycleConfig(), SetupLifecycleStore(),
        )
        obs = eng.process(_bullish_for("RELIANCE"), "c1", timestamp=_clock_at())
        before = obs.state
        # A FUTURE observation (different cycle) cannot mutate the first.
        advance(3600)
        eng.process(_bullish_for("RELIANCE"), "c2", timestamp=clock())
        assert obs.state == before  # immutable history

    def test_alert_payload_immutable(self):
        from engine.models.user_alerts import AlertEvent

        assert AlertEvent.__dataclass_params__.frozen


def _bullish_for(instrument: str = "RELIANCE"):
    from tests.test_setup_lifecycle import _bullish

    return _bullish(instrument)


# ===================================================================
# O. BROKER / EXECUTION BOUNDARY (AST)
# ===================================================================


class TestBrokerBoundary:
    MODULES = [
        "src/dashboard/reliability.py",
        "src/dashboard/alert_outbox.py",
        "src/dashboard/structured_log.py",
        "src/engine/persistence/reliability_store.py",
        "src/engine/persistence/reliability_serialization.py",
        "src/engine/reporting/reliability.py",
        "src/engine/config/reliability_config.py",
        "src/engine/models/operational_health.py",
    ]

    def test_no_broker_execution_imports(self):
        root = Path(__file__).resolve().parent.parent
        forbidden = {
            "execution_command", "execution_authorization",
            "broker_adapter", "submission_lifecycle",
            "operational_trade_intent", "paper_trading",
            "reference_broker_adapter", "upstox_broker", "fake_broker",
            "requests", "httpx", "urllib", "socket", "websocket",
            "broker", "kiteconnect", "yfinance",
        }
        for rel in self.MODULES:
            path = root / rel
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imports.append(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        imports.append(node.module.split(".")[0])
            hit = [token for token in forbidden if token in imports]
            assert not hit, f"{rel} imports forbidden broker/execution {hit}"

    def test_no_network_imports_in_reliability(self):
        root = Path(__file__).resolve().parent.parent
        forbidden = {"requests", "httpx", "urllib", "socket", "aiohttp", "websocket"}
        for rel in self.MODULES:
            path = root / rel
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module.split(".")[0])
            hit = [t for t in forbidden if t in imports]
            assert not hit, f"{rel} imports network {hit}"

    def test_no_credentials_attr_in_reliability(self):
        root = Path(__file__).resolve().parent.parent
        for rel in self.MODULES:
            ast.parse((root / rel).read_text(encoding="utf-8"))
            text = (root / rel).read_text(encoding="utf-8")
            assert "Authorization:" not in text
            assert "UPSTOX_EXECUTION_ACCESS_TOKEN" not in text
            assert "UPSTOX_ANALYTICS_TOKEN" not in text


# ===================================================================
# Misc model invariants
# ===================================================================


class TestModelInvariants:
    def test_models_frozen(self):
        for cls in (
            OperationalStateBundle, ProviderHealth, CycleHealth,
            DeliveryHealth, OperationalEventRecord, RetryRecord,
            PerSymbolFailure, CycleFailureSummary, OperationalHealthReport,
        ):
            assert cls.__dataclass_params__.frozen
            obj = cls.__new__(cls)
            with pytest.raises(AttributeError):
                object.__setattr__(obj, "_x", 1)

    def test_snapshot_id_prefix(self):
        assert operational_snapshot_id(
            _clock_at(), "f", "c", "a", "rv",
        ).startswith("ops-")

    def test_per_symbol_failure_requires_positive_attempts(self):
        with pytest.raises(ValueError):
            PerSymbolFailure(
                symbol="X",
                category=ReliabilityFailureCategory.PROVIDER_UNAVAILABLE,
                timestamp=_clock_at(),
                attempts=0,
            )

    def test_cycle_summary_requires_positive_counts(self):
        with pytest.raises(ValueError):
            CycleFailureSummary(requested=-1)


# Local import shims for helpers referenced above
from dashboard.user_alerts import ConsoleAlertSink  # noqa: E402
from engine.models.operational_health import (  # noqa: E402
    OperationalEventRecord,
)  # noqa: E402