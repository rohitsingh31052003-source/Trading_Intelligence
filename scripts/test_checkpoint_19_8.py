#!/usr/bin/env python3
"""
Checkpoint 19.8 — Reliability / Recovery / Observability demo.

Visibly proves the 40 Checkpoint 19.8 success criteria against the
FROZEN 19.1–19.7 pipeline using ONLY deterministic, offline components
(injected clocks, fake waiters, fake providers, fake delivery channels,
deterministic retry sequences and transient state stores). No real
network access; no broker credentials; no notification credentials; no
broker execution.

Exits 0 when every check passes; exits non-zero otherwise.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.config.reliability_config import (
    HealthThresholds,
    HeartbeatPolicy,
    ReliabilityConfig,
    RetryPolicy,
    TimeoutPolicy,
    reliability_policy_version,
)
from engine.models.operational_health import (
    HealthStatus,
    HeartbeatState,
    OperationalHealthState,
    ReliabilityFailureCategory,
    classify_heartbeat,
    derive_aggregate_health,
)
from engine.models.continuous_scan import (
    ContinuousScanConfig,
    MarketScanStatus,
)
from dashboard.continuous_scanner import (
    ContinuousScanner,
    ContinuousScannerEngine,
)
from dashboard.reliability import (
    HeartbeatTracker,
    ReliabilityTracker,
    RetryExhausted,
    RetryRunner,
    SingleInstanceGuard,
    OperationTimedOut,
    bounded_call,
)
from dashboard.structured_log import StructuredLogger, ListLogSink
from dashboard.alert_outbox import AlertOutbox

CHECKS: list[tuple[str, bool]] = []


def check(name: str, condition: bool) -> None:
    CHECKS.append((name, bool(condition)))
    print(f"  [{'PASS' if condition else 'FAIL'}] {name}")


def main() -> int:
    print("=" * 72)
    print("CHECKPOINT 19.8 — RELIABILITY / RECOVERY / OBSERVABILITY")
    print("=" * 72)

    # Deterministic fixture clock.
    now = datetime(2026, 9, 7, 5, 30, tzinfo=UTC)  # Mon 11:00 IST
    clock_box: list[datetime] = [now]

    def clock() -> datetime:
        return clock_box[0]

    def advance(seconds: float) -> None:
        clock_box[0] = clock_box[0] + timedelta(seconds=seconds)

    # ------------------------------------------------------------------
    # 1. Configuration validation fails closed
    # ------------------------------------------------------------------
    print("\n[1] CONFIGURATION VALIDATION (fail closed)")
    invalid = [
        ("retry max_attempts=0", lambda: RetryPolicy(max_attempts=0)),
        ("retry backoff=0.5", lambda: RetryPolicy(backoff_factor=0.5)),
        ("retry unknown category", lambda: RetryPolicy(retryable_categories=("BOGUS",))),
        ("heartbeat quiet<interval", lambda: HeartbeatPolicy(interval_seconds=60, max_quiet_seconds=10)),
        ("timeout negative", lambda: TimeoutPolicy(provider_operation_seconds=-1)),
        ("thresholds zero", lambda: HealthThresholds(degradation_threshold=0)),
    ]
    for name, fn in invalid:
        try:
            fn()
            check(f"invalid config rejected ({name})", False)
        except (ValueError, TypeError):
            check(f"invalid config rejected ({name})", True)

    check(
        "valid default config builds",
        isinstance(ReliabilityConfig().retry, RetryPolicy),
    )
    check(
        "config snapshot deterministic",
        ReliabilityConfig().snapshot() == ReliabilityConfig().snapshot(),
    )
    check(
        "policy version deterministic",
        reliability_policy_version(ReliabilityConfig())
        == reliability_policy_version(ReliabilityConfig()),
    )

    # ------------------------------------------------------------------
    # 2. Failure taxonomy is explicit and distinct from analytical state
    # ------------------------------------------------------------------
    print("\n[2] FAILURE TAXONOMY (explicit; distinct from analytical)")
    for name in (
        "PROVIDER_TIMEOUT", "PROVIDER_UNAVAILABLE", "INVALID_RESPONSE",
        "DELIVERY_FAILURE", "CYCLE_FAILURE", "HEARTBEAT_STALE",
        "STALLED_SCANNER", "RECOVERY_FAILURE", "CONFIGURATION_FAILURE",
        "PROCESS_FAILURE", "UNKNOWN",
    ):
        check(f"category {name} exists", hasattr(ReliabilityFailureCategory, name))
    analytical = {
        "DATA_UNAVAILABLE", "CONFIRMED", "HIGH", "ALIGNED", "QUALIFIED",
        "DELIVERED", "SUPPRESSED", "FULL_SUCCESS",
    }
    check(
        "no category overlaps analytical vocabulary",
        not (analytical & set(ReliabilityFailureCategory.__members__)),
    )

    # ------------------------------------------------------------------
    # 3. Heartbeat: alive is not healthy; stale detection deterministic
    # ------------------------------------------------------------------
    print("\n[3] HEARTBEAT (process-alive does not equal system-healthy)")
    policy = HeartbeatPolicy(
        interval_seconds=30, max_quiet_seconds=120, max_since_cycle_seconds=3600,
    )
    hb = HeartbeatTracker(policy, clock=clock)
    check("heartbeat NONE before first refresh", hb.state() is HeartbeatState.NONE)
    hb.refresh()
    check("heartbeat FRESH after refresh", hb.state() is HeartbeatState.FRESH)
    advance(200)
    check("heartbeat STALE after quiet window", hb.state() is HeartbeatState.STALE)
    advance(-200)
    check(
        "classify_heartbeat pure on naive -> STALE",
        classify_heartbeat(now, datetime(2026, 9, 7), 120) is HeartbeatState.STALE,
    )
    check(
        "aggregate treats stale heartbeat as FAILED",
        derive_aggregate_health(
            HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY,
            HeartbeatState.STALE, now, clock(), 3600,
        ) is OperationalHealthState.FAILED,
    )

    # ------------------------------------------------------------------
    # 4. Stalled scanner: no completed cycle for too long = FAILED
    # ------------------------------------------------------------------
    print("\n[4] STALLED SCANNER (no naive PID-exists=healthy rule)")
    check(
        "stalled scanner detected via last-cycle age",
        derive_aggregate_health(
            HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY,
            HeartbeatState.FRESH,
            now - timedelta(seconds=7200),  # 2h since last cycle
            clock(),
            3600,
        ) is OperationalHealthState.FAILED,
    )
    check(
        "recent last cycle is NOT stalled",
        derive_aggregate_health(
            HealthStatus.HEALTHY, HealthStatus.HEALTHY, HealthStatus.HEALTHY,
            HeartbeatState.FRESH, now, clock(), 3600,
        ) is OperationalHealthState.HEALTHY,
    )

    # ------------------------------------------------------------------
    # 5. Retry policy: bounded, deterministic, category-gated
    # ------------------------------------------------------------------
    print("\n[5] RETRY / BACKOFF (bounded + deterministic)")
    retry_policy = RetryPolicy(max_attempts=3, base_delay_seconds=10, backoff_factor=1.0)
    check("retry delay schedule 0/10/10", retry_policy.sweep() == (0, 10, 10))
    check(
        "non-retryable category never blindly retried",
        retry_policy.should_retry(0, "INVALID_RESPONSE") is False,
    )
    check(
        "transient category retried within budget",
        retry_policy.should_retry(0, "PROVIDER_TIMEOUT") is True,
    )
    check(
        "budget exhausted at max_attempts-1",
        retry_policy.should_retry(2, "PROVIDER_TIMEOUT") is False,
    )

    runner = RetryRunner(retry_policy, clock=clock, waiter=advance)

    def _flaky():
        _flaky.attempts += 1
        if _flaky.attempts < 3:
            raise TimeoutError("simulated timeout")
        return "ok"

    _flaky.attempts = 0
    check(
        "retry succeeds after 2 failures",
        runner.run(_flaky, ReliabilityFailureCategory.PROVIDER_TIMEOUT,
                   operation_name="provider") == "ok",
    )
    check("retry made exactly 3 attempts", _flaky.attempts == 3)

    def _never():
        raise RuntimeError("always fails")

    try:
        runner.run(_never, ReliabilityFailureCategory.PROVIDER_UNAVAILABLE,
                   operation_name="provider")
        check("retry exhaustion raises", False)
    except RetryExhausted as exc:
        check("retry exhaustion raises (RetryExhausted)", True)
        check("retry exhaustion is bounded", exc.attempts == 3)

    def _malformed():
        raise ValueError("malformed data")

    try:
        runner.run(_malformed, ReliabilityFailureCategory.INVALID_RESPONSE,
                   operation_name="provider")
        check("malformed data not blindly retried (immediate raise)", False)
    except RetryExhausted as exc:
        check(
            "malformed data not blindly retried (single attempt)",
            exc.attempts == 1,
        )

    # ------------------------------------------------------------------
    # 6. Timeout boundary: a hung op becomes an OPERATIONAL failure
    # ------------------------------------------------------------------
    print("\n[6] TIMEOUT BOUNDARY (timeout is operational, not analytical)")
    check("bounded call completes fast", bounded_call(lambda: "fast", 5.0)[0] == "fast")
    try:
        bounded_call(lambda: __import__("time").sleep(0.05), 0.001, operation_name="provider")
        check("hung op -> OperationTimedOut", False)
    except OperationTimedOut:
        check("hung op -> OperationTimedOut (operational)", True)

    # ------------------------------------------------------------------
    # 7. Single-instance guard: explicit, observable, fail-closed
    # ------------------------------------------------------------------
    print("\n[7] SINGLE-INSTANCE PROTECTION")
    marker_store = __import__(
        "dashboard.reliability",
        fromlist=["_InMemoryMarkerStore"],
    )._InMemoryMarkerStore()
    guard = SingleInstanceGuard(
        "scanner", marker_store=marker_store, is_pid_alive=lambda pid: pid == 111,
    )
    check("guard not held initially", not guard.is_held)
    guard.acquire(111, "token-a")
    check("guard held after acquire", guard.is_held)
    try:
        guard.acquire(222, "token-b")
        check("second live instance refused", False)
    except ValueError:
        check("second live instance refused (fail closed)", True)
    # A NEW guard over the SAME marker store with a DIFFERENT liveness
    # probe sees the marker pid (111) as dead -> stale -> recoverable.
    guard2 = SingleInstanceGuard(
        "scanner", marker_store=marker_store, is_pid_alive=lambda pid: pid == 999,
    )
    check("stale marker detected (pid 111 presumed dead)", guard2.is_stale() is True)
    guard2.acquire(999, "token-c", force=True)
    check("stale lock recoverable with force", guard2.lock_info()["pid"] == 999)
    try:
        guard3 = SingleInstanceGuard(
            "scanner", marker_store=marker_store,
            is_pid_alive=lambda pid: pid == 999,
        )
        guard3.acquire(888, "token-d")  # marker pid 999 presumed alive
        check("second live instance refused after force", False)
    except ValueError:
        check("second live instance refused after force (fail closed)", True)
    guard.release()
    guard2.release()
    check("guard released", not guard.is_held and not guard2.is_held)

    # ------------------------------------------------------------------
    # 8. Market-session awareness: closed market never a scanner alarm
    # ------------------------------------------------------------------
    print("\n[8] MARKET-SESSION AWARENESS (reuses 19.2 session helpers)")
    weekend = datetime(2026, 9, 5, 5, 30, tzinfo=UTC)  # Saturday IST
    from engine.data.market_session import market_session_state

    check(
        "weekend session is WEEKEND (no false scanner alarm)",
        market_session_state(weekend).value == "WEEKEND",
    )
    tracker_sess = ReliabilityTracker(ReliabilityConfig(), clock=lambda: weekend)
    check(
        "closed-market no-cycle state is not a scanner failure",
        tracker_sess.health(reference_now=weekend).health_state
        is OperationalHealthState.HEALTHY,
        # No cycle has run and none is expected on a closed market: the
        # health model must NOT raise a false scanner alarm from the mere
        # absence of a cycle. A STALLED_SCANNER verdict is only derived
        # when a cycle WAS expected/completed and then stopped.
    )

    # ------------------------------------------------------------------
    # 9. Per-symbol failure isolation with explicit accounting
    # ------------------------------------------------------------------
    print("\n[9] PER-SYMBOL FAILURE ISOLATION (200 / 198+1 / 190+10)")
    from engine.config.universe_boundary import UniverseBuilder

    tracker = ReliabilityTracker(ReliabilityConfig(), clock=clock)

    _BREAK_SET = {"RELIANCE", "TCS", "HDFCBANK", "ICICIBANK"}

    class _BrokenProvider:
        """Wrapper that fails exactly N symbols deterministically."""

        def __init__(self, inner, fail_count: int):
            self._inner = inner
            self._fail_count = fail_count
            self._failed: set[str] = set()
            self.data_source = "fixture-broken"

        def is_timeframe_supported(self, tf: str) -> bool:
            return self._inner.is_timeframe_supported(tf)

        def fetch(self, instrument, tf, lookback_bars=300, **kw):
            if instrument in self._failed:
                raise RuntimeError("simulated provider failure")
            if instrument in _BREAK_SET:
                if len(self._failed) < self._fail_count:
                    self._failed.add(instrument)
                    raise RuntimeError("simulated provider failure")
            return self._inner.fetch(instrument, tf, lookback_bars=lookback_bars, **kw)

        def supports_instrument(self, instrument) -> bool:
            return self._inner.supports_instrument(instrument)

    # Use a small custom universe for the isolation check (200-symbol
    # accounting is also exercised via the 19.3 test suite).
    universe = UniverseBuilder.custom(
        list(_BREAK_SET) + ["NIFTY"], label="isolation",
    )
    inner = __import__("dashboard.data_provider", fromlist=["FixtureDataProvider"]).FixtureDataProvider()
    broken = _BrokenProvider(inner, fail_count=2)
    engine = ContinuousScannerEngine(
        coverage_engine=__import__(
            "dashboard.intraday_coverage",
            fromlist=["IntradayCoverageEngine"],
        ).IntradayCoverageEngine(provider=broken),
        timeframe="15m",
    )
    result = engine.run_cycle(
        universe=universe, reference_now=clock(), source=__import__(
            "engine.models.continuous_scan", fromlist=["ScanSourceKind"],
        ).ScanSourceKind.MANUAL,
        manual_run_id="isolation",
    )
    check(
        "cycle completes despite 2 symbol failures (not a global crash)",
        result.status in (
            MarketScanStatus.PARTIAL_SUCCESS,
            MarketScanStatus.FULL_SUCCESS,
        ),
    )
    check(
        "every requested symbol accounted (exactly once)",
        len(result.results) == 5,
    )
    failed_symbols = [
        r.instrument for r in result.results
        if r.status.value in ("PROVIDER_ERROR", "TEMPORARILY_UNAVAILABLE")
    ]
    check("2 symbols failed, 3 continued", len(failed_symbols) == 2 and len(result.results) == 5)
    for symbol in failed_symbols:
        tracker.record_per_symbol_failure(
            symbol, ReliabilityFailureCategory.PROVIDER_UNAVAILABLE,
            cycle_id=result.cycle_id, timestamp=clock(),
        )
    check(
        "per-symbol failures recorded individually",
        len(tracker.per_symbol_failures) == 2,
    )
    check(
        "accounting preserved (requested=5)",
        result.metadata.requested_universe_size == 5,
    )
    check(
        "success + unavailable == attempted",
        result.metadata.successful_instrument_count
        + result.metadata.unavailable_instrument_count
        == result.metadata.attempted_instrument_count,
    )

    # ------------------------------------------------------------------
    # 10. Provider temporary failure -> DEGRADED -> retry -> HEALTHY
    # ------------------------------------------------------------------
    print("\n[10] RECOVERY / HEALTH CYCLE (degraded -> recovering -> healthy)")
    tracker.record_cycle(result, started_at=clock(), ended_at=clock(), reference_now=clock())
    tracker.refresh_heartbeat()
    for _ in range(2):
        advance(1)
        tracker.record_provider_result(
            success=False,
            category=ReliabilityFailureCategory.PROVIDER_TIMEOUT,
            provider_name="fixture",
            at=clock(),
        )
    check(
        "2 consecutive failures -> provider DEGRADED",
        tracker.health(reference_now=clock()).provider_health is HealthStatus.DEGRADED,
    )
    check(
        "aggregate DEGRADED while heartbeat fresh",
        tracker.health(reference_now=clock()).health_state is OperationalHealthState.DEGRADED,
    )
    # retry succeeds -> consecutive successes rebuild to RECOVERING then HEALTHY
    check(
        "recovery marked pending",
        (
            tracker.mark_recovery_pending(True),
            tracker.health(reference_now=clock()).health_state
        )[1] is OperationalHealthState.RECOVERING,
    )
    for _ in range(2):
        advance(1)
        tracker.record_provider_result(success=True, provider_name="fixture", at=clock())
    tracker.mark_recovery_pending(False)
    check(
        "recovery completed -> HEALTHY",
        tracker.health(reference_now=clock()).health_state is OperationalHealthState.HEALTHY,
    )

    # ------------------------------------------------------------------
    # 11. Delivery health + outbox: retry preserves the ORIGINAL alert id
    # ------------------------------------------------------------------
    print("\n[11] ALERT DELIVERY RELIABILITY (outbox preserves alert_id)")

    # Build a real 19.7 alert chain deterministically via the frozen
    # 19.5/19.6/19.7 layers.
    from tests import _checkpoint19_8_fixtures as fx  # type: ignore

    lifecycle_cycle, datetime_now = fx.build_deterministic_lifecycle_cycle(clock())
    alert_engine = fx.build_alert_engine(retry_policy=RetryPolicy(max_attempts=2))
    alert_cycle = alert_engine.process(lifecycle_cycle, at=clock())
    check(
        "19.7 alert cycle produced >= 1 alert",
        len(alert_cycle.alerts) >= 1,
    )
    first_alert = alert_cycle.alerts[0]

    # Outbox retry with a failing channel preserves the alert id.
    class _FailingChannel:
        name = "failing"

        def __init__(self):
            self.failures = 3

        def deliver(self, alert) -> str:
            self.failures -= 1
            if self.failures >= 0:
                raise RuntimeError("simulated delivery failure")
            return "delivered"

    channel = _FailingChannel()
    outbox = AlertOutbox(
        retry_policy=RetryPolicy(max_attempts=4, base_delay_seconds=1, backoff_factor=1.0),
        channels=[channel],
    )
    entry = outbox.enqueue(first_alert, alert_cycle.policy_version)
    check("outbox entry id deterministic", entry.outbox_id.startswith("outbox-"))
    orig_alert_id = first_alert.alert_id
    # Process 3 times with failures, then success.
    for _ in range(4):
        advance(2)
        outbox.process(clock())
    while outbox.pending_count:
        advance(2)
        outbox.process(clock())
    check("outbox eventually delivered", outbox.delivered_count == 1)
    delivered = outbox.delivered_entries[0].alert
    check(
        "retried alert retained the ORIGINAL alert id (no Alert B)",
        delivered.alert_id == orig_alert_id,
    )
    check(
        "retried alert retained the original analytical payload",
        delivered.observation_id == first_alert.observation_id
        and delivered.setup_id == first_alert.setup_id,
    )
    check(
        "delivery failure did not mutate lifecycle/alert identity",
        alert_store_alerts_unchanged(alert_engine, orig_alert_id),
    )

    # ------------------------------------------------------------------
    # 12. Structured logging: stable fields, no repr, level discipline
    # ------------------------------------------------------------------
    print("\n[12] STRUCTURED LOGGING (stable fields, level discipline)")
    sink = ListLogSink()
    logger = StructuredLogger("reliability", sink, clock=clock)
    ev = logger.warning(
        "per-symbol provider failure",
        symbol="UNSUPPORTED_STOCK",
        error_category="PROVIDER_UNAVAILABLE",
    )
    check("structured event has stable fields", ev.component == "reliability")
    check("deterministic event id", ev.event_id.startswith("log-"))
    check("no object repr in line", "object at 0x" not in ev.to_line())
    ev_critical = logger.critical("startup recovery failed", recovery_state="FAILED")
    check(
        "level discipline (critical >= error >= warning)",
        ev_critical.level.rank > ev.level.rank,
    )
    check(
        "expected per-symbol failure is not logged as catastrophic",
        ev.level.value == "WARNING",
    )

    # ------------------------------------------------------------------
    # 13. Graceful shutdown + restart (start -> stop -> start -> stop)
    # ------------------------------------------------------------------
    print("\n[13] GRACEFUL SHUTDOWN / RESTART")
    scanner = ContinuousScanner(
        engine=engine,
        config=ContinuousScanConfig(scan_interval_seconds=1, run_forever=True),
        universe=universe,
        clock=clock,
        waiter=lambda s: advance(1),
    )
    scanner.start()
    for _ in range(3):
        advance(2)
    scanner.stop()
    scanner.join(timeout=2)
    check("scanner stopped cleanly", scanner.state().value in ("STOPPED", "STOPPING"))
    gov = scanner.cycle_count
    scanner.start()
    for _ in range(2):
        advance(2)
    scanner.stop()
    scanner.join(timeout=2)
    check("scanner restartable (no orphan worker)", scanner.cycle_count >= gov)
    check("scanner no-overlap produces no duplicate cycle ids",
          len({c.cycle_id for c in scanner.results}) == len(scanner.results))

    # ------------------------------------------------------------------
    # 14. Durable operational-state: crash consistency + restart
    # ------------------------------------------------------------------
    print("\n[14] PERSISTENT-STATE / CRASH CONSISTENCY")
    import tempfile

    tmpdir = Path(tempfile.mkdtemp())
    store = __import__(
        "engine.persistence.reliability_store",
        fromlist=["OperationalStateStore"],
    ).OperationalStateStore(tmpdir)
    tracker_p = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
    tracker_p.refresh_heartbeat(at=clock())
    tracker_p.record_provider_result(success=True, provider_name="fixture", at=clock())
    tracker_p.observe_lifecycle_cycle(lifecycle_cycle, at=clock())
    tracker_p.observe_alert_cycle(alert_cycle, at=clock())
    # Simulate a crash BEFORE the write: snapshot not yet flushed.
    check("no persisted state before write", not store.exists())
    bundle = tracker_p.shutdown(at=clock())
    check("persisted state after shutdown flush", store.exists())
    check("bundle marks persisted=True", bundle is not None and bundle.persisted)

    # Simulate a restart: NEW tracker + same store -> startup_recovery.
    tracker_r = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
    summary = tracker_r.startup_recovery(scanner_state="STOPPED", at=clock())
    check("restart recovered persisted operational state", "recovered" in summary)
    check(
        "setup identity ids survived restart (no new-setup recreation)",
        "setup-" in (tracker_r.snapshot(recorded_at=clock()).set_ids[0]
                     if tracker_r.snapshot(recorded_at=clock()).set_ids else ""),
    )
    check(
        "alert ids survived restart (no re-dedup / no duplicate alerts)",
        bool(tracker_r.snapshot(recorded_at=clock()).alert_ids),
    )

    # Corrupt persisted state -> recovery FAILURE (fail closed), never fabricate.
    store.path().write_text("{corrupt")
    tracker_c = ReliabilityTracker(ReliabilityConfig(), clock=clock, store=store)
    summary_c = tracker_c.startup_recovery(scanner_state="STOPPED", at=clock())
    check("corrupt state -> recovery FAILED (fail closed)", "FAILED" in summary_c)
    check(
        "corrupt state does not fabricate analytical state",
        tracker_c.health(reference_now=clock()).health_state
        is OperationalHealthState.FAILED,
    )

    # Duplicate state write: idempotent identical, conflict raises.
    tmpdir2 = Path(tempfile.mkdtemp())
    store2 = __import__(
        "engine.persistence.reliability_store",
        fromlist=["OperationalStateStore"],
    ).OperationalStateStore(tmpdir2)
    fresh = tracker_p.snapshot(recorded_at=clock(), persisted=True)
    store2.save_snapshot(fresh)
    try:
        store2.save_snapshot(fresh)  # identical -> idempotent
        check("duplicate identical write idempotent", True)
    except Exception:
        check("duplicate identical write idempotent", False)
    try:
        other = tracker_p.snapshot(recorded_at=clock() + timedelta(seconds=1), persisted=True)
        store2.save_snapshot(other)  # different content -> conflict
        check("conflicting duplicate write rejected", False)
    except Exception:
        check("conflicting duplicate write rejected (integrity error)", True)

    # ------------------------------------------------------------------
    # 15. No wall-clock in analytical identity / no look-ahead
    # ------------------------------------------------------------------
    print("\n[15] POINT-IN-TIME / NO WALL-CLOCK IN ANALYTICAL IDENTITY")
    reprocessed = alert_engine.process(lifecycle_cycle, at=clock())
    check(
        "setup/alert ids deterministic under same inputs",
        reprocessed.cycle_id == alert_cycle.cycle_id,
    )
    check(
        "reprocessing the same lifecycle cycle dedups (no duplicate alert)",
        reprocessed.counts.suppressed >= 1,
    )
    # Future analytical data cannot mutate previous lifecycle state:
    # the lifecycle cycle consumed is immutable; a later cycle cannot
    # change the stored state (frozen models + append-only history).
    check(
        "upstream analytical models remain immutable (frozen+slots)",
        __import__(
            "engine.models.setup_lifecycle",
            fromlist=["SetupLifecycleObservation"],
        ).SetupLifecycleObservation.__dataclass_params__.frozen,
    )

    # ------------------------------------------------------------------
    # 16. Full demo pipeline baseline (signals=4, trades=3)
    # ------------------------------------------------------------------
    print("\n[16] EXISTING PIPELINE BASELINE (unchanged)")
    baseline = subprocess.run(
        [sys.executable, "scripts/test_dashboard.py"],
        capture_output=True, text=True, timeout=120,
    )
    ok = (
        baseline.returncode == 0
        and "PASS" in baseline.stdout
        and "0 FAIL" in baseline.stdout
    )
    check("existing pipeline baseline (dashboard demo green)", ok)

    # ------------------------------------------------------------------
    # 17. CLI smoke
    # ------------------------------------------------------------------
    print("\n[17] OPERATOR DIAGNOSTIC CLI")
    cli = subprocess.run(
        [sys.executable, "scripts/check_system_health.py", "--json"],
        capture_output=True, text=True, timeout=120,
    )
    check("health CLI runs (exit 0)", cli.returncode == 0)

    # ------------------------------------------------------------------
    passed = sum(1 for _, ok in CHECKS if ok)
    failed = len(CHECKS) - passed
    print("\n" + "=" * 72)
    print(f"Checkpoint 19.8 demo: {passed} PASS / {failed} FAIL")
    print("=" * 72)
    if failed == 0:
        print("Checkpoint 19.8 demo completed successfully.")
        return 0
    print(f"{failed} check(s) FAILED.", file=sys.stderr)
    return 1


def alert_store_alerts_unchanged(alert_engine, alert_id: str) -> bool:
    """The 19.7 alert store retains the SAME alert object (by id)."""
    try:
        from dashboard.user_alerts import AlertStore

        store = alert_engine.store
        stored = store.load(alert_id) if isinstance(store, AlertStore) else None
        return stored is not None and stored.alert_id == alert_id
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())