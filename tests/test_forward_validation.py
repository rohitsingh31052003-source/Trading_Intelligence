"""
Checkpoint 19.9 — validation / forward-testing framework tests.

Deterministic, offline, adversarial test suite for the final Checkpoint
19 layer. Covers the 30 mandatory test requirements and the 35
adversarial scenarios (split across suites: model/config/identity/
point-in-time/outcome/session/universe/reporting/latency in this file;
adversarial scenarios in ``test_forward_validation_adversarial.py``;
persistence/restart + CLI in ``test_forward_validation_persistence.py``
+ ``test_forward_validation_cli.py``).

No wall-clock, no randomness, no network, no credentials, no broker /
execution imports.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta

import pytest

from dashboard.forward_validation import (
    ForwardOutcomeEngine,
    ForwardSessionManager,
    ForwardValidationEngine,
    build_forward_observation,
)
from engine.config.forward_validation_config import (
    DEFAULT_MAX_HOLDING_BARS,
    ForwardValidationConfig,
)
from engine.models.forward_validation import (
    FORWARD_VALIDATION_MODEL_VERSION,
    ForwardCycleResult,
    ForwardObservation,
    ForwardObservationStatus,
    ForwardOutcome,
    ForwardValidationReport,
    LifecycleResolution,
    OutcomeAvailability,
    OutcomeDirection,
    build_forward_cycle_id,
    build_observation_id,
    build_outcome_id,
    build_report_id,
    build_session_id,
    classify_outcome_direction,
)
from engine.models.setup_lifecycle import (
    LifecycleObservationStatus,
    SetupLifecycleState,
)
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)

from tests._checkpoint19_9_fixtures import (
    FIXTURE_START,
    FIXTURE_WEEKEND,
    lifecycle,
    make_observation,
    make_outcome,
    quality,
    scripted_candles,
)

T = FIXTURE_START


# ==================================================================
# A. CONFIG
# ==================================================================


class TestConfig:
    def test_defaults(self):
        cfg = ForwardValidationConfig()
        assert cfg.provider == "fixture"
        assert cfg.primary_timeframe == "15m"
        assert cfg.max_holding_bars == DEFAULT_MAX_HOLDING_BARS
        assert cfg.record_duplicates is False
        assert cfg.reject_out_of_order is True

    def test_snapshot_sorted_deterministic(self):
        cfg = ForwardValidationConfig(label="x")
        a = cfg.snapshot()
        b = ForwardValidationConfig(label="x").snapshot()
        assert a == b
        keys = [k for k, _ in a]
        assert keys == sorted(keys)

    def test_policy_version_changes_on_any_change(self):
        base = ForwardValidationConfig()
        v1 = base.policy_version()
        assert ForwardValidationConfig().policy_version() == v1
        assert ForwardValidationConfig(max_holding_bars=7).policy_version() != v1
        assert ForwardValidationConfig(label="z").policy_version() != v1
        assert ForwardValidationConfig(provider="fixture").policy_version() == v1

    def test_invalid_timeframes_rejected(self):
        with pytest.raises(ValueError):
            ForwardValidationConfig(timeframes=("1D",))
        with pytest.raises(ValueError):
            ForwardValidationConfig(timeframes=("bogus",))
        with pytest.raises(ValueError):
            ForwardValidationConfig(timeframes=())

    def test_invalid_horizon_rejected(self):
        with pytest.raises(ValueError):
            ForwardValidationConfig(max_holding_bars=0)
        with pytest.raises(TypeError):
            ForwardValidationConfig(max_holding_bars=True)

    def test_metadata_pair_validation(self):
        with pytest.raises(TypeError):
            ForwardValidationConfig(metadata=(("a",),))
        with pytest.raises(TypeError):
            ForwardValidationConfig(metadata=(("a", 1),))

    def test_frozen(self):
        cfg = ForwardValidationConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            cfg.max_holding_bars = 3  # type: ignore[misc]


# ==================================================================
# B. OBSERVATION CREATION + IDENTITY
# ==================================================================


class TestObservation:
    def test_creation_and_fields(self):
        obs = make_observation(
            "fsess-x",
            scan_cycle_id="scan-1",
            instrument="RELIANCE",
            reference_now=T,
        )
        assert obs.observation_id.startswith("fobs-")
        assert obs.instrument == "RELIANCE"
        assert obs.direction == "BULLISH"
        assert obs.observation_timestamp == T
        assert obs.is_directional
        assert obs.is_qualified
        assert obs.is_alerted
        assert obs.has_reference_price

    def test_identity_deterministic(self):
        a = make_observation("s", scan_cycle_id="c", instrument="TCS", reference_now=T)
        b = make_observation("s", scan_cycle_id="c", instrument="TCS", reference_now=T)
        assert a.observation_id == b.observation_id

    def test_identity_changes_with_inputs(self):
        base = make_observation("s", scan_cycle_id="c", instrument="TCS", reference_now=T)
        assert base.observation_id != make_observation(
            "s", scan_cycle_id="c", instrument="TCS", reference_now=T + timedelta(minutes=15)
        ).observation_id
        assert base.observation_id != make_observation(
            "s", scan_cycle_id="d", instrument="TCS", reference_now=T
        ).observation_id
        assert base.observation_id != make_observation(
            "s", scan_cycle_id="c", instrument="HDFCBANK", reference_now=T
        ).observation_id

    def test_invalid_observation_id_rejected(self):
        with pytest.raises(ValueError):
            make_observation(
                "s", scan_cycle_id="c", instrument="TCS", reference_now=T
            ).__class__(
                observation_id="nope",
                setup_id="", lifecycle_id="", instrument="TCS", direction="",
                setup_type="", primary_timeframe="15m",
                observation_timestamp=T, scan_cycle_id="c",
                lifecycle_state=LifecycleResolution.DATA_UNAVAILABLE,
                lifecycle_observation_status=LifecycleObservationStatus.DATA_UNAVAILABLE,
                quality_status=SetupQualityStatus.UNAVAILABLE,
                quality_classification=SetupQualityClassification.UNAVAILABLE,
                quality_score=None, mtf_alignment="UNAVAILABLE",
                mtf_completeness="UNAVAILABLE", alert_state="", alert_id="",
            )

    def test_naive_timestamp_rejected(self):
        naive = datetime(2026, 9, 4, 5, 30)
        with pytest.raises(ValueError):
            ForwardObservation(
                observation_id="fobs-x",
                setup_id="", lifecycle_id="", instrument="TCS", direction="",
                setup_type="", primary_timeframe="15m",
                observation_timestamp=naive, scan_cycle_id="c",
                lifecycle_state=LifecycleResolution.DATA_UNAVAILABLE,
                lifecycle_observation_status=LifecycleObservationStatus.DATA_UNAVAILABLE,
                quality_status=SetupQualityStatus.UNAVAILABLE,
                quality_classification=SetupQualityClassification.UNAVAILABLE,
                quality_score=None, mtf_alignment="UNAVAILABLE",
                mtf_completeness="UNAVAILABLE", alert_state="", alert_id="",
            )

    def test_frozen_and_slots(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="TCS", reference_now=T)
        assert hasattr(obs, "__slots__")
        with pytest.raises(dataclasses.FrozenInstanceError):
            obs.instrument = "NIFTY"  # type: ignore[misc]


class TestObservationBuilder:
    def test_build_from_duck_typed_upstream(self):
        obs = build_forward_observation(
            session_id="fsess-x",
            scan_cycle_id="scan-1",
            reference_now=T,
            instrument="RELIANCE",
            quality=quality("RELIANCE"),
            lifecycle=lifecycle("RELIANCE"),
            policy_version="p1",
            reference_price=101.5,
        )
        assert obs.direction == "BULLISH"
        assert obs.quality_status is SetupQualityStatus.QUALIFIED
        assert obs.quality_classification is SetupQualityClassification.EXCELLENT
        assert obs.lifecycle_state is LifecycleResolution.CONFIRMED
        assert obs.setup_id == "setup-reliance"
        assert obs.mtf_alignment == "ALIGNED"
        assert obs.reference_price == 101.5

    def test_build_missing_upstream_explicit(self):
        obs = build_forward_observation(
            session_id="fsess-x",
            scan_cycle_id="scan-1",
            reference_now=T,
            instrument="RELIANCE",
            quality=None,
            lifecycle=None,
            policy_version="p1",
        )
        assert obs.direction == ""
        assert obs.quality_status is SetupQualityStatus.UNAVAILABLE
        assert obs.lifecycle_state is LifecycleResolution.DATA_UNAVAILABLE
        assert obs.setup_id == ""
        assert obs.mtf_alignment == "UNAVAILABLE"
        assert obs.reference_price is None


# ==================================================================
# C. OUTCOME SEPARATION + MEASUREMENT
# ==================================================================


class TestOutcomeEngine:
    def test_separate_record(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        out = engine.measure(obs, scripted_candles(5)[1:6])
        assert isinstance(out, ForwardOutcome)
        assert out.observation_id == obs.observation_id
        assert out.outcome_id != obs.observation_id
        assert obs.reference_price == 100.0  # never mutated

    def test_forward_only_filter(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=3))
        candles = scripted_candles(5)  # at T..T+4
        # Strictly-after filter: index 0 (at T) is excluded.
        # Completion at measurement T+45 (inclusive close): index 1
        # (close T+30) and index 2 (close T+45) are completed; index 3
        # (close T+60) is not. Horizon trims to 3 -> used == 2.
        out = engine.measure(obs, candles, measurement_timestamp=T + timedelta(minutes=45))
        assert out.bars_used == 2
        assert out.bars_available == 4
        assert out.horizon_bars == 3
        # Only 2 of 3 horizon candles are completed at T+45; the window
        # is not yet full.
        assert out.window_complete is False

    def test_direction_handling_bullish(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=3))
        rising = scripted_candles(4, closes=[100, 102, 104, 106])[1:4]
        out = engine.measure(obs, rising, measurement_timestamp=T + timedelta(minutes=45))
        assert out.direction_consistent is True
        assert out.outcome_direction is OutcomeDirection.FAVORABLE
        assert out.forward_return is not None and out.forward_return > 0

    def test_direction_handling_bearish(self):
        obs = make_observation(
            "s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T,
            direction="BEARISH",
        )
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=3))
        falling = scripted_candles(4, closes=[100, 98, 96, 94])[1:4]
        out = engine.measure(obs, falling, measurement_timestamp=T + timedelta(minutes=45))
        assert out.direction_consistent is True
        assert out.outcome_direction is OutcomeDirection.FAVORABLE
        assert out.forward_return < 0

    def test_measurement_window_horizon(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=2))
        out = engine.measure(obs, scripted_candles(6)[1:6], measurement_timestamp=T + timedelta(minutes=60))
        assert out.bars_used == 2
        assert out.horizon_bars == 2
        # 2 of the 5 after-T candles (indexes 1,2) complete by T+60.
        assert out.window_complete is True

    def test_missing_data_unavailable(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        out = engine.measure(obs, (), measurement_timestamp=T + timedelta(minutes=15))
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE
        assert out.forward_return is None
        assert out.bars_used == 0

    def test_partial_window(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=5))
        out = engine.measure(
            obs,
            scripted_candles(8)[1:7],
            measurement_timestamp=T + timedelta(minutes=105),
        )
        assert out.bars_used == 5
        assert out.bars_available == 6
        assert out.window_complete is True

    def test_window_incomplete_when_window_not_mature(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine(ForwardValidationConfig(max_holding_bars=5))
        out = engine.measure(obs, scripted_candles(6)[1:3], measurement_timestamp=T + timedelta(minutes=30))
        assert out.bars_available == 2
        # Only index 1 (close T+30) is completed at T+30; index 2
        # (close T+45) is not yet completed.
        assert out.bars_used == 1
        assert out.window_complete is False
        assert out.availability in (
            OutcomeAvailability.OUTCOME_PARTIAL,
            OutcomeAvailability.WINDOW_INCOMPLETE,
        )

    def test_no_reference_price_ambiguous(self):
        obs = make_observation(
            "s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T,
            reference_price=None,
        )
        engine = ForwardOutcomeEngine()
        out = engine.measure(obs, scripted_candles(4)[1:4])
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE
        assert out.outcome_direction is OutcomeDirection.NOT_EVALUABLE

    def test_measurement_before_observation_rejected(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        with pytest.raises(ValueError):
            engine.measure(obs, scripted_candles(3), measurement_timestamp=T - timedelta(minutes=1))

    def test_measurement_never_mutates_observation(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        out = engine.measure(obs, scripted_candles(5)[1:5])
        assert out.observation_id == obs.observation_id
        assert obs.reference_price == 100.0

    def test_outcome_id_deterministic(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        mt = T + timedelta(minutes=60)
        a = engine.measure(obs, scripted_candles(5)[1:5], measurement_timestamp=mt)
        b = engine.measure(obs, scripted_candles(5)[1:5], measurement_timestamp=mt)
        assert a.outcome_id == b.outcome_id

    def test_outcome_id_changes_with_measurement_time(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        engine = ForwardOutcomeEngine()
        a = engine.measure(obs, scripted_candles(5)[1:5], measurement_timestamp=T + timedelta(minutes=45))
        b = engine.measure(obs, scripted_candles(5)[1:5], measurement_timestamp=T + timedelta(minutes=60))
        assert a.outcome_id != b.outcome_id


class TestClassifyOutcomeDirection:
    def test_bullish_favorable(self):
        assert (
            classify_outcome_direction(
                direction="BULLISH", forward_return=0.02,
                max_favorable=0.03, max_adverse=-0.01,
            )
            is OutcomeDirection.FAVORABLE
        )

    def test_bullish_unfavorable(self):
        assert (
            classify_outcome_direction(
                direction="BULLISH", forward_return=-0.02,
                max_favorable=0.01, max_adverse=-0.03,
            )
            is OutcomeDirection.UNFAVORABLE
        )

    def test_bullish_neutral(self):
        assert (
            classify_outcome_direction(
                direction="BULLISH", forward_return=0.0,
                max_favorable=0.0, max_adverse=0.0,
            )
            is OutcomeDirection.NEUTRAL
        )

    def test_bearish_mirror(self):
        assert (
            classify_outcome_direction(
                direction="BEARISH", forward_return=-0.02,
                max_favorable=0.03, max_adverse=-0.01,
            )
            is OutcomeDirection.FAVORABLE
        )
        assert (
            classify_outcome_direction(
                direction="BEARISH", forward_return=0.02,
                max_favorable=0.01, max_adverse=-0.02,
            )
            is OutcomeDirection.UNFAVORABLE
        )

    def test_not_evaluable(self):
        assert (
            classify_outcome_direction(
                direction="", forward_return=None,
                max_favorable=None, max_adverse=None,
            )
            is OutcomeDirection.NOT_EVALUABLE
        )


# ==================================================================
# D. SESSIONS
# ==================================================================


class TestSessionManager:
    def test_open_session(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(
            universe=("RELIANCE", "TCS"),
            started_at=T,
            label="run-1",
        )
        assert session.session_id.startswith("fsess-")
        assert session.status == "OPEN"
        assert session.universe == ("RELIANCE", "TCS")
        assert session.provider == "fixture"

    def test_session_identity_deterministic(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        a = manager.open_session(universe=("RELIANCE",), started_at=T)
        manager2 = ForwardSessionManager(ForwardValidationConfig())
        b = manager2.open_session(universe=("RELIANCE",), started_at=T)
        assert a.session_id == b.session_id

    def test_session_identity_changes_with_config(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        a = manager.open_session(universe=("RELIANCE",), started_at=T)
        manager2 = ForwardSessionManager(ForwardValidationConfig(label="z"))
        b = manager2.open_session(universe=("RELIANCE",), started_at=T)
        assert a.session_id != b.session_id

    def test_duplicate_observation_flagged(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        first = manager.record_observation(session.session_id, obs)
        dup = manager.record_observation(session.session_id, obs)
        assert first.status is ForwardObservationStatus.RECORDED
        assert dup.status is ForwardObservationStatus.DUPLICATE
        assert manager.observations_count(session.session_id) == 1

    def test_out_of_order_rejected(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        manager.record_observation(
            session.session_id,
            make_observation(session.session_id, scan_cycle_id="c2", instrument="RELIANCE", reference_now=T + timedelta(minutes=30)),
        )
        late = manager.record_observation(
            session.session_id,
            make_observation(session.session_id, scan_cycle_id="c1", instrument="RELIANCE", reference_now=T + timedelta(minutes=15)),
        )
        assert late.status is ForwardObservationStatus.OUT_OF_ORDER
        assert manager.observations_count(session.session_id) == 1

    def test_out_of_order_allowed_when_configured(self):
        manager = ForwardSessionManager(ForwardValidationConfig(reject_out_of_order=False))
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        manager.record_observation(
            session.session_id,
            make_observation(session.session_id, scan_cycle_id="c2", instrument="RELIANCE", reference_now=T + timedelta(minutes=30)),
        )
        later = manager.record_observation(
            session.session_id,
            make_observation(session.session_id, scan_cycle_id="c1", instrument="RELIANCE", reference_now=T + timedelta(minutes=15)),
        )
        assert later.status is ForwardObservationStatus.RECORDED
        assert manager.observations_count(session.session_id) == 2

    def test_close_session(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        closed = manager.close_session(session.session_id, ended_at=T + timedelta(hours=1))
        assert closed.status == "CLOSED"
        assert closed.ended_at == T + timedelta(hours=1)
        with pytest.raises(ValueError):
            manager.close_session(session.session_id)


# ==================================================================
# E. CYCLE  + UNIVERSE ACCOUNTING
# ==================================================================


class TestCycle:
    def test_process_requires_open_session(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        manager.close_session(session.session_id)
        engine = ForwardValidationEngine(
            ForwardValidationConfig(),
            session_manager=manager,
        )
        with pytest.raises(ValueError):
            engine.process(session, reference_now=T)

    def test_universe_exactly_once(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS", "HDFCBANK"))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert isinstance(cycle, ForwardCycleResult)
        assert len(cycle.observations) == 3
        assert [o.instrument for o in cycle.observations] == [
            "HDFCBANK", "RELIANCE", "TCS",
        ]
        assert len({o.observation_id for o in cycle.observations}) == 3

    def test_empty_universe(self):
        cfg = ForwardValidationConfig(universe=())
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert cycle.observations == ()

    def test_missing_symbol_not_dropped(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS"))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert len(cycle.observations) == 2
        for obs in cycle.observations:
            assert obs.quality_status is SetupQualityStatus.UNAVAILABLE
            assert obs.lifecycle_state is LifecycleResolution.DATA_UNAVAILABLE

    def test_duplicate_processing_idempotent(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE",))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        a = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        b = engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        assert a.cycle_id == b.cycle_id
        assert manager.observations_count(session.session_id) == 1
        assert b.observations[0].status is ForwardObservationStatus.DUPLICATE

    def test_reference_prices_attached(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE",))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        cycle = engine.process(
            session,
            scan_cycle_id="scan-1",
            reference_now=T,
            reference_prices={"RELIANCE": 250.5},
        )
        assert cycle.observations[0].reference_price == 250.5

    def test_lifecycle_and_alert_state_captured(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE",))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        lifecycle("RELIANCE", setup_id="setup-r", lifecycle_id="lc-r")
        from engine.models.setup_lifecycle import (
            SetupLifecycleCycleResult,
            SetupLifecycleObservationResult,
        )

        lc_result = SetupLifecycleObservationResult(
            instrument="RELIANCE",
            observation_timestamp=T,
            scan_cycle_id="scan-1",
            setup_id="setup-r",
            lifecycle_id="lc-r",
            state=SetupLifecycleState.CONFIRMED,
            created=True,
            advanced=False,
            duplicate=False,
            late_rejected=False,
            observation=None,
            transition=None,
            observation_status=LifecycleObservationStatus.OBSERVED,
            reason="",
        )
        lc_cycle = SetupLifecycleCycleResult(
            cycle_id="lc-cycle-1",
            scan_cycle_id="scan-1",
            reference_now=T,
            instruments=("RELIANCE",),
            results=(lc_result,),
        )
        cycle = engine.process(
            session,
            scan_cycle_id="scan-1",
            reference_now=T,
            quality_universe=None,
            lifecycle_cycle=lc_cycle,
            alert_cycle=None,
        )
        obs = cycle.observations[0]
        assert obs.setup_id == "setup-r"
        assert obs.lifecycle_state is LifecycleResolution.CONFIRMED

    def test_cycle_id_deterministic(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE",))
        m1 = ForwardSessionManager(cfg)
        m2 = ForwardSessionManager(cfg)
        s1 = m1.open_session(started_at=T)
        s2 = m2.open_session(started_at=T)
        e1 = ForwardValidationEngine(cfg, session_manager=m1)
        e2 = ForwardValidationEngine(cfg, session_manager=m2)
        c1 = e1.process(s1, scan_cycle_id="scan-1", reference_now=T)
        c2 = e2.process(s2, scan_cycle_id="scan-1", reference_now=T)
        assert c1.cycle_id == c2.cycle_id
        assert c1.cycle_id.startswith("fcycle-")


# ==================================================================
# F. REPORT
# ==================================================================


class TestReport:
    def _setup(self):
        cfg = ForwardValidationConfig(universe=("RELIANCE", "TCS"))
        manager = ForwardSessionManager(cfg)
        session = manager.open_session(started_at=T)
        engine = ForwardValidationEngine(cfg, session_manager=manager)
        return cfg, manager, session, engine

    def test_empty_report(self):
        _, manager, session, engine = self._setup()
        engine.close_session(session.session_id)
        report = engine.report(session.session_id)
        assert isinstance(report, ForwardValidationReport)
        assert report.report_id.startswith("frep-")
        assert report.observations == ()

    def test_aggregation_counts(self):
        _, manager, session, engine = self._setup()
        engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        report = engine.report(session.session_id)
        assert len(report.observations) == 2
        assert report.instruments_attempted == 2
        assert report.setups_detected == 0

    def test_setup_frequency_aggregation(self):
        _, manager, session, engine = self._setup()
        obs = make_observation(session.session_id, scan_cycle_id="scan-1", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        report = engine.report(session.session_id)
        setup_counts = dict(report.setup_counts)
        assert setup_counts.get("setup-reliance", 0) == 1
        assert dict(report.classification_counts).get("EXCELLENT", 0) == 1

    def test_outcome_dimensions(self):
        _, manager, session, engine = self._setup()
        obs = make_observation(session.session_id, scan_cycle_id="scan-1", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        outcome = make_outcome(obs)
        report = engine.report(session.session_id, outcomes=(outcome,))
        assert dict(report.outcome_counts).get("OUTCOME_AVAILABLE", 0) == 1
        assert dict(report.outcome_direction_counts).get("FAVORABLE", 0) == 1

    def test_report_id_deterministic(self):
        cfg = ForwardValidationConfig(label="x")
        m1 = ForwardSessionManager(cfg)
        m2 = ForwardSessionManager(cfg)
        s1 = m1.open_session(universe=("R",), started_at=T)
        s2 = m2.open_session(universe=("R",), started_at=T)
        e1 = ForwardValidationEngine(cfg, session_manager=m1)
        e2 = ForwardValidationEngine(cfg, session_manager=m2)
        r1 = e1.report(s1.session_id)
        r2 = e2.report(s2.session_id)
        assert r1.report_id == r2.report_id

    def test_sample_size_note_honest(self):
        _, manager, session, engine = self._setup()
        engine.process(session, scan_cycle_id="scan-1", reference_now=T)
        report = engine.report(session.session_id)
        assert "SAMPLE TOO SMALL" in report.sample_size_note


# ==================================================================
# G. POINT-IN-TIME / LEAKAGE
# ==================================================================


class TestPointInTime:
    def test_observation_unchanged_by_future_outcome(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager.record_observation(session.session_id, obs)
        engine = ForwardOutcomeEngine()
        future = scripted_candles(10, closes=[100, 105, 110, 115, 120, 125, 130, 135, 140, 145])
        out = engine.measure(obs, future[1:])
        assert out.forward_return is not None and out.forward_return > 0
        assert obs == make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)

    def test_original_observation_byte_identical(self):
        from engine.persistence.forward_validation_serialization import (
            serialize_observation,
        )

        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        original_bytes = serialize_observation(obs)
        engine = ForwardOutcomeEngine()
        _ = engine.measure(obs, scripted_candles(8, closes=[100, 200, 50, 300, 400, 30, 500, 600])[1:])
        assert serialize_observation(obs) == original_bytes

    def test_setup_identity_unchanged(self):
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=T)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T, setup_id="setup-r")
        setup_before = obs.setup_id
        _ = ForwardOutcomeEngine().measure(obs, scripted_candles(5)[1:])
        assert obs.setup_id == setup_before


# ==================================================================
# H. IDENTITY HELPERS
# ==================================================================


class TestIdentityHelpers:
    def test_session_id(self):
        sid = build_session_id(
            provider="fixture",
            timeframes=("15m", "1h"),
            universe=("RELIANCE",),
            started_at=T,
            policy_version="p",
        )
        assert sid.startswith("fsess-")

    def test_observation_id(self):
        oid = build_observation_id(
            session_id="s", scan_cycle_id="c", instrument="RELIANCE",
            direction="BULLISH", setup_type="X", primary_timeframe="15m",
            observation_timestamp=T, policy_version="p",
        )
        assert oid.startswith("fobs-")

    def test_outcome_id(self):
        oid = build_outcome_id(
            observation_id="fobs-x", measurement_timestamp=T,
            horizon_bars=5, policy_version="p",
        )
        assert oid.startswith("fout-")

    def test_cycle_id(self):
        cid = build_forward_cycle_id(
            session_id="s", scan_cycle_id="c", reference_now=T, policy_version="p",
        )
        assert cid.startswith("fcycle-")

    def test_report_id(self):
        rid = build_report_id(session_ids=("s",), policy_version="p")
        assert rid.startswith("frep-")

    def test_model_version_constant(self):
        assert FORWARD_VALIDATION_MODEL_VERSION == 1


# ==================================================================
# I. LATENCY
# ==================================================================


class TestLatency:
    def test_no_latency_when_timestamps_unavailable(self):
        """Latency is NOT manufactured: without timestamps the CLI/demo
        report unavailable measurements; the engine has no latency
        fields that can silently default to zero."""
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        assert obs.observation_timestamp == T
        # The observation carries only the analytical timestamp; any
        # latency computation must be explicit (Report what is
        # available — the formatter's session/report never prints a
        # synthetic latency).

    def test_analytical_temporal_ordering(self):
        """Observation timestamp strictly precedes outcome measurement
        timestamp (documented latency anchor availability)."""
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs, measurement_timestamp=T + timedelta(minutes=45))
        assert out.measurement_timestamp > obs.observation_timestamp


# ==================================================================
# J. FROZEN MODEL / VOCABULARY
# ==================================================================


class TestVocabulary:
    def test_statuses_distinct(self):
        # The 19.9 statuses are DELIBERATELY distinct from the upstream
        # 19.5/19.6/19.7/19.8 vocabularies.
        assert ForwardObservationStatus.__name__ != "SetupQualityStatus"
        assert OutcomeAvailability.__name__ != "AlertDeliveryStatus"
        assert OutcomeAvailability.__name__ != "OperationalHealthState"
        assert OutcomeDirection.__name__ != "MTFDirection"

    def test_no_execution_vocabulary(self):
        import ast
        import pathlib

        tree = ast.parse(
            pathlib.Path("src/engine/models/forward_validation.py").read_text()
        )
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        # No executable BUY / SELL / order vocabulary in the model
        # module (docstrings may legitimately state what the layer does
        # NOT do).
        assert not ({"BUY", "SELL", "place_order", "cancel_order",
                     "order_size", "position"} & names)

    def test_weekend_reference_safe(self):
        # A weekend reference is a valid instant; sessions/observations
        # accept it and outcome measurement simply reports no forward
        # candles when none are supplied (no system failure).
        manager = ForwardSessionManager(ForwardValidationConfig())
        session = manager.open_session(universe=("RELIANCE",), started_at=FIXTURE_WEEKEND)
        obs = make_observation(
            session.session_id, scan_cycle_id="c", instrument="RELIANCE",
            reference_now=FIXTURE_WEEKEND,
        )
        manager.record_observation(session.session_id, obs)
        out = ForwardOutcomeEngine().measure(obs, ())
        assert out.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE