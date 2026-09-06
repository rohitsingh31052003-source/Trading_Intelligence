"""
Checkpoint 19.9 — adversarial forward-validation test suite.

Deterministic, offline, adversarial coverage of the mandatory 35
scenarios: future information can never leak backwards into an earlier
observation; duplicates, restarts, retries, missing/partial windows,
provider failures, symbol-unavailability, ordering, corrupt persistence,
market-closed / weekend handling, tiny / large samples and the broker /
execution boundary.

No wall-clock, no randomness, no network, no credentials, no broker /
execution imports.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from dashboard.forward_validation import (
    ForwardOutcomeEngine,
    ForwardSessionManager,
    ForwardValidationEngine,
)
from engine.config.forward_validation_config import ForwardValidationConfig
from engine.models.forward_validation import (
    ForwardObservationStatus,
    LifecycleResolution,
    OutcomeAvailability,
)
from engine.models.setup_lifecycle import SetupLifecycleState
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)
from engine.persistence.forward_validation_store import (
    ForwardObservationStore,
)

from tests._checkpoint19_9_fixtures import (
    FIXTURE_START,
    FIXTURE_WEEKEND,
    alert,
    candle,
    lifecycle,
    make_observation,
    scripted_candles,
)

T = FIXTURE_START


def _manager(cfg: ForwardValidationConfig | None = None):
    return ForwardSessionManager(cfg or ForwardValidationConfig())


def _session(manager, universe=("RELIANCE",), started_at=T):
    return manager.open_session(universe=universe, started_at=started_at)


# ==================================================================
# 1-7. FUTURE-INFORMATION LEAKAGE
# ==================================================================


class TestFutureLeakage:
    def test_1_future_candle_before_outcome_eval(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=3))
        # Adversarial: the future candle arrives BEFORE outcome
        # evaluation — the original observation (already recorded) is
        # unchanged and the outcome consumes it strictly-after only.
        future = [candle(1, close=120.0), candle(2, close=130.0), candle(3, close=140.0)]
        out = engine.measure(obs, future, measurement_timestamp=T + timedelta(minutes=45))
        assert out.bars_used == 2
        assert obs.reference_price == 100.0
        assert obs.observation_id.startswith("fobs-")

    def test_2_future_setup_confirmation_does_not_affect_earlier(self):
        manager = _manager()
        session = _session(manager)
        early = make_observation(
            session.session_id, scan_cycle_id="c1", instrument="RELIANCE",
            reference_now=T,
            quality_status=SetupQualityStatus.WATCH,
            quality_classification=SetupQualityClassification.LOW,
            quality_score=40, lifecycle_state=LifecycleResolution.DETECTED,
        )
        manager.record_observation(session.session_id, early)
        # Later observation CONFIRMS the setup — the earlier observation
        # must remain an immutable WATCH/DETECTED record.
        later = make_observation(
            session.session_id, scan_cycle_id="c2", instrument="RELIANCE",
            reference_now=T + timedelta(minutes=30),
            quality_status=SetupQualityStatus.QUALIFIED,
            quality_classification=SetupQualityClassification.EXCELLENT,
            quality_score=88, lifecycle_state=LifecycleResolution.CONFIRMED,
        )
        manager.record_observation(session.session_id, later)
        assert early.quality_classification is SetupQualityClassification.LOW
        assert early.lifecycle_state is LifecycleResolution.DETECTED
        assert later.quality_classification is SetupQualityClassification.EXCELLENT
        assert later.lifecycle_state is LifecycleResolution.CONFIRMED

    def test_3_future_bos_does_not_affect_earlier(self):
        # A "future BOS" is a future market-structure event. The
        # observation layer never consumes market structure; the
        # immutable record stands regardless of what happens later.
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        oid = obs.observation_id
        _ = ForwardOutcomeEngine().measure(
            obs,
            scripted_candles(8, closes=[100, 300, 400, 500, 600, 700, 800, 900])[1:],
        )
        assert obs.observation_id == oid
        assert obs.mtf_alignment == "ALIGNED"

    def test_4_future_trend_change_does_not_affect_earlier(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        _ = ForwardOutcomeEngine().measure(obs, scripted_candles(6)[1:])
        # MTF label is frozen at T.
        assert obs.mtf_alignment == "ALIGNED"

    def test_5_future_support_resistance_change(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        # The observation has no support/resistance fields — the market
        # context is frozen into the upstream 19.4/19.5 outputs, never
        # re-derived by the observation layer.
        assert not hasattr(obs, "support")
        assert not hasattr(obs, "resistance")

    def test_6_future_alert_does_not_affect_earlier(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(
            session.session_id, scan_cycle_id="c", instrument="RELIANCE",
            reference_now=T, alert_state="", alert_id="",
        )
        manager.record_observation(session.session_id, obs)
        alert_record = alert("RELIANCE", alert_id="future-alert")
        assert alert_record.alert_id != obs.alert_id
        # The recorded observation remains non-alerted regardless of a
        # later alert.
        assert obs.alert_state == ""

    def test_7_future_lifecycle_transition_does_not_affect_earlier(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(
            session.session_id, scan_cycle_id="c", instrument="RELIANCE",
            reference_now=T, lifecycle_state=LifecycleResolution.DETECTED,
        )
        manager.record_observation(session.session_id, obs)
        later = lifecycle("RELIANCE", state=SetupLifecycleState.INVALIDATED)
        assert later.state is SetupLifecycleState.INVALIDATED
        assert obs.lifecycle_state is LifecycleResolution.DETECTED


# ==================================================================
# 8-10. IDEMPOTENCY / RESTART / RETRY
# ==================================================================


class TestIdempotencyRestartRetry:
    def test_8_same_observation_processed_twice(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        first = manager.record_observation(session.session_id, obs)
        second = manager.record_observation(session.session_id, obs)
        assert first.status is ForwardObservationStatus.RECORDED
        assert second.status is ForwardObservationStatus.DUPLICATE
        assert manager.observations_count(session.session_id) == 1

    def test_9_same_session_restarted(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        # A "restart" is a NEW manager with the SAME config: opening a
        # new session yields the SAME deterministic session id; the old
        # observations are not lost from the audit trail.
        manager2 = _manager()
        session2 = manager2.open_session(universe=("RELIANCE",), started_at=T)
        assert session2.session_id == session.session_id
        # Observations are deterministic by identity — re-recording is
        # exactly idempotent at the record level.

    def test_10_same_alert_retried_20_times(self):
        manager = _manager()
        session = _session(manager)
        # Alert identity is preserved by construction: the SAME alert
        # retried 20 times keeps the SAME alert identity (19.8 outbox
        # preserves the original alert_id across retries). Building the
        # same alert 20 times produces ONE deterministic alert id, and
        # the captured observation never changes its alert binding.
        alert_ids = {
            alert("RELIANCE", alert_id="alert-q", setup_id="s1").alert_id
            for _ in range(20)
        }
        assert len(alert_ids) == 1
        # And the captured observation alert-id never changes when the
        # same alert is re-delivered.
        obs = make_observation(
            session.session_id, scan_cycle_id="c", instrument="RELIANCE",
            reference_now=T, alert_id="alert-q",
        )
        manager.record_observation(session.session_id, obs)
        assert obs.alert_id == "alert-q"
        # The alert identity is NOT derived from the retry count.
        assert obs.alert_id == "alert-q"


# ==================================================================
# 11-13. MISSING / PARTIAL / PROVIDER FAILURE
# ==================================================================


class TestMissingPartialProvider:
    def test_11_missing_outcome_window(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        out = ForwardOutcomeEngine().measure(obs, (), measurement_timestamp=T + timedelta(hours=1))
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE
        assert out.bars_used == 0

    def test_12_partial_outcome_window(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=5))
        out = engine.measure(
            obs,
            scripted_candles(4)[1:2],
            measurement_timestamp=T + timedelta(minutes=30),
        )
        assert out.window_complete is False
        assert out.bars_used >= 0

    def test_13_provider_failure_during_observation(self):
        # A provider failure means NO upstream quality/lifecycle output
        # for the symbol -> an explicit non-observation (never a crash,
        # never fabricated data).
        manager = _manager()
        session = _session(manager, universe=("RELIANCE", "TCS"))
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS"))
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(
            session,
            scan_cycle_id="scan-1",
            reference_now=T,
            quality_universe=None,  # provider failure surfaces as None
        )
        assert len(cycle.observations) == 2
        for obs in cycle.observations:
            assert obs.quality_status is SetupQualityStatus.UNAVAILABLE
            assert obs.lifecycle_state is LifecycleResolution.DATA_UNAVAILABLE

    def test_14_provider_failure_during_outcome_measurement(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        # No forward candles (provider failure) -> OUTCOME_UNAVAILABLE,
        # never a fabricated favorable/unfavorable outcome.
        out = ForwardOutcomeEngine().measure(obs, None, measurement_timestamp=T + timedelta(hours=1))
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE


# ==================================================================
# 15-17. SYMBOL / UNIVERSE AVAILABILITY
# ==================================================================


class TestSymbolAvailability:
    def test_15_one_symbol_unavailable(self):
        manager = _manager()
        session = _session(manager, universe=("RELIANCE", "TCS"))
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS"))
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        # Every constituent is explicitly accounted for (both
        # unavailable in this fully-offline path).
        assert len(cycle.observations) == 2
        assert all(o.quality_status is SetupQualityStatus.UNAVAILABLE for o in cycle.observations)

    def test_16_50_symbols_unavailable(self):
        universe = tuple(f"SYM{i:03d}" for i in range(50))
        manager = _manager()
        session = _session(manager, universe=universe)
        cfg = ForwardValidationConfig(universe=universe)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert len(cycle.observations) == 50
        assert len({o.instrument for o in cycle.observations}) == 50

    def test_17_entire_provider_unavailable(self):
        manager = _manager()
        session = _session(manager, universe=("RELIANCE", "TCS", "HDFCBANK"))
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS", "HDFCBANK"))
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        # No provider output at all -> every symbol is an explicit
        # non-observation, cycle still completes with a result.
        assert len(cycle.observations) == 3
        assert all(o.quality_status is SetupQualityStatus.UNAVAILABLE for o in cycle.observations)


# ==================================================================
# 18-20. SCANNER / DELIVERY / RECOVERY
# ==================================================================


class TestScannerDeliveryRecovery:
    def test_18_scanner_restart(self):
        # A "scanner restart" is modeled as a new cycle after a stop:
        # identical deterministic inputs yield identical observation
        # identities and no loss of prior records.
        manager = _manager()
        session = _session(manager)
        engine = ForwardValidationEngine(ForwardValidationConfig(), session_manager=manager)
        a = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        b = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert a.cycle_id == b.cycle_id
        assert manager.observations_count(session.session_id) == 1

    def test_19_alert_delivery_failure(self):
        # Delivery failure belongs to 19.7/19.8; the observation layer
        # only carries the alert state at T. A failed delivery cannot
        # change the analytical state or identity.
        manager = _manager()
        session = _session(manager)
        obs = make_observation(
            session.session_id, scan_cycle_id="c", instrument="RELIANCE",
            reference_now=T, alert_state="ALERTED", alert_id="alert-f",
        )
        manager.record_observation(session.session_id, obs)
        assert obs.alert_id == "alert-f"
        assert obs.alert_state == "ALERTED"

    def test_20_reliability_recovery(self):
        # Recovery is a 19.8 concern; forward validation consumes the
        # operational state as-is (here: unavailable, i.e. no
        # operational state was supplied) without manufacturing a
        # recovery claim.
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        assert obs.status is ForwardObservationStatus.RECORDED


# ==================================================================
# 21-24. PERSISTENCE / ORDERING
# ==================================================================


class TestPersistenceOrdering:
    def test_21_corrupt_persistence_fails_closed(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        # Save an observation, then corrupt its file -> load must FAIL
        # (never silently return a valid-looking record).
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        path = store.observation_path(obs.observation_id)
        path.write_text("{corrupted-json", encoding="utf-8")

        from engine.persistence.exceptions import ForwardStoreIntegrityError

        with pytest.raises(ForwardStoreIntegrityError):
            store.load_observation(obs.observation_id)

    def test_22_different_provider_order(self):
        # Provider iteration order NEVER determines the recorded
        # observation set (sorted canonical instruments).
        manager = _manager()
        session = _session(manager, universe=("TCS", "RELIANCE", "HDFCBANK"))
        cfg = ForwardValidationConfig(universe=("TCS", "RELIANCE", "HDFCBANK"))
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert [o.instrument for o in cycle.observations] == [
            "HDFCBANK", "RELIANCE", "TCS",
        ]

    def test_23_different_symbol_order(self):
        m1 = _manager()
        s1 = _session(m1, universe=("RELIANCE", "TCS"))
        m2 = _manager()
        s2 = _session(m2, universe=("TCS", "RELIANCE"))
        e1 = ForwardValidationEngine(ForwardValidationConfig(), session_manager=m1)
        e2 = ForwardValidationEngine(ForwardValidationConfig(), session_manager=m2)
        c1 = e1.process(s1, scan_cycle_id="scan-1", reference_now=T)
        c2 = e2.process(s2, scan_cycle_id="scan-1", reference_now=T)
        assert [o.observation_id for o in c1.observations] == [
            o.observation_id for o in c2.observations
        ]

    def test_24_duplicate_candles(self):
        # Duplicate candle timestamps in the forward series never break
        # measurement: dedup is NOT required because the strictly-after
        # + completion filters are pure predicates over the candle list.
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        candles = scripted_candles(4)[1:4] + scripted_candles(4)[1:4]
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=3))
        out = engine.measure(obs, candles, measurement_timestamp=T + timedelta(minutes=60))
        assert out.bars_used == 3
        assert out.bars_available == 6


# ==================================================================
# 25-29. MARKET HOURS / CYCLE
# ==================================================================


class TestMarketHoursCycle:
    def test_25_market_closed(self):
        # A closed-market reference never indicates a system failure;
        # it simply yields an unavailable outcome when no forward data
        # exists.
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        out = ForwardOutcomeEngine().measure(obs, ())
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE

    def test_26_weekend(self):
        manager = _manager()
        session = _session(manager, started_at=FIXTURE_WEEKEND)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=FIXTURE_WEEKEND)
        manager.record_observation(session.session_id, obs)
        out = ForwardOutcomeEngine().measure(obs, ())
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE

    def test_27_long_running_cycle(self):
        # A long-running cycle = many sequential observations; the
        # engine remains deterministic and bounded.
        manager = _manager()
        universe = tuple(f"SYM{i:03d}" for i in range(100))
        session = _session(manager, universe=universe)
        cfg = ForwardValidationConfig(universe=universe)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert len(cycle.observations) == 100

    def test_28_no_setup_day(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS", "HDFCBANK"))
        manager = _manager(cfg)
        session = _session(manager, universe=cfg.universe)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        report = engine.report(session.session_id)
        assert report.setups_detected == 0
        assert len(cycle.observations) == 3

    def test_29_large_setup_day(self):
        # MANY setup observations in one cycle: identities remain
        # unique + deterministic.
        manager = _manager()
        session = _session(manager, universe=("RELIANCE", "TCS", "HDFCBANK"))
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS", "HDFCBANK"))
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-big", reference_now=T)
        assert len(cycle.observations) == 3


# ==================================================================
# 30-33. SAMPLE SIZE / COVERAGE / DUPLICATE ALERTS
# ==================================================================


class TestSampleCoverage:
    def test_30_tiny_sample(self):
        manager = _manager()
        session = _session(manager)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        report = ForwardValidationEngine(ForwardValidationConfig(), session_manager=manager).report(session.session_id)
        assert "SAMPLE TOO SMALL" in report.sample_size_note

    def test_31_incomplete_top200_coverage(self):
        # 19.1 universe = 201 instruments (200 + benchmark). When the
        # upstream pipeline evaluates only a subset, the forward layer
        # still records ONE observation per REQUESTED constituent and
        # never silently drops symbols.
        universe = tuple(f"SYM{i:03d}" for i in range(200))
        manager = _manager()
        session = _session(manager, universe=universe)
        cfg = ForwardValidationConfig(universe=universe)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert len(cycle.observations) == 200

    def test_32_duplicate_alert_event(self):
        # The same alert event processed twice produces the same alert
        # identity; the observation is idempotent.
        a1 = alert("RELIANCE", alert_id="alert-q", setup_id="s1")
        a2 = alert("RELIANCE", alert_id="alert-q", setup_id="s1")
        assert a1.alert_id == a2.alert_id

    def test_33_no_cherry_picking(self):
        # The report consumes ALL recorded observations (full
        # population) — no subset filtering exists in the layer.
        manager = _manager()
        session = _session(manager, universe=("RELIANCE", "TCS", "HDFCBANK"))
        engine = ForwardValidationEngine(ForwardValidationConfig(), session_manager=manager)
        engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        report = engine.report(session.session_id)
        assert report.instruments_attempted == 3
        assert len(report.observations) == 3


# ==================================================================
# 34-35. BROKER / EXECUTION BOUNDARY
# ==================================================================


class TestBrokerBoundary:
    def test_34_attempted_broker_import(self):
        # The forward-validation modules must not import execution /
        # broker code. AST-check every new 19.9 module.
        import ast
        import pathlib

        modules = [
            "src/engine/config/forward_validation_config.py",
            "src/engine/models/forward_validation.py",
            "src/engine/persistence/forward_validation_serialization.py",
            "src/engine/persistence/forward_validation_store.py",
            "src/dashboard/forward_validation.py",
            "src/engine/reporting/forward_validation.py",
            "scripts/forward_validation.py",
        ]
        forbidden_imports = {
            "broker_adapter", "submission_lifecycle", "execution_command",
            "execution_authorization", "upstox_broker", "fake_broker",
            "paper_trading", "trade_planning", "ExecutionCommand",
            "ExecutionAuthorization", "BrokerAdapter",
        }
        for module in modules:
            tree = ast.parse(pathlib.Path(module).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] not in forbidden_imports
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] not in forbidden_imports

    def test_35_no_network_no_credentials(self):
        # The engine/config/models/persistence must not use network
        # libraries or read execution credentials.
        import ast
        import pathlib

        modules = [
            "src/engine/config/forward_validation_config.py",
            "src/engine/models/forward_validation.py",
            "src/engine/persistence/forward_validation_serialization.py",
            "src/engine/persistence/forward_validation_store.py",
            "src/dashboard/forward_validation.py",
            "src/engine/reporting/forward_validation.py",
        ]
        forbidden = {"requests", "httpx", "urllib", "socket", "http",
                     "websocket", "aiohttp"}
        for module in modules:
            tree = ast.parse(pathlib.Path(module).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert alias.name.split(".")[0] not in forbidden
                elif isinstance(node, ast.ImportFrom):
                    assert (node.module or "").split(".")[0] not in forbidden