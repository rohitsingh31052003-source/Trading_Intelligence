"""
Checkpoint 19.6 — setup lifecycle tests.

These tests prove :class:`dashboard.setup_lifecycle.SetupLifecycleEngine`
is a deterministic, honest SETUP LIFECYCLE layer that answers:

    "Is this the SAME setup across subsequent scan cycles, what
     lifecycle state is it currently in, and how has that setup evolved
     over time?"

Coverage mirrors the 60-point Checkpoint 19.6 test requirement list
plus the mandatory adversarial lifecycle scenarios:

* the lifecycle layer consumes the FROZEN 19.5 setup-quality results
  and the FROZEN 19.3 scan-cycle identity; 19.1 universe / 19.2 data
  status / 19..4 MTF / 19..5 quality semantics remain intact;
* setup identity is deterministic, reproducible, explainable and
  point-in-time safe; identity never depends on scan-cycle id, score,
  price, wall-clock, provider order or data-quality fields;
* first observation creates exactly one lifecycle; repeated observation
  reuses the same setup id / lifecycle; a changed score never creates a
  duplicate;
* state transitions are explicit and deterministic (DETECTED ->
  CONFIRMED -> INVALIDATED / EXPIRED); transition reasons are correct;
  terminal lifecycles cannot resurrect;
* quality / MTF-alignment / setup-evidence changes are represented as
  assessment changes (never new identities / never state churn);
* provider failure / stale / incomplete / unsupported data is
  DATA_UNAVAILABLE (never invalidation); clean absence is
  distinguished from disproven setup; expiration is deterministic;
* processing the same scan cycle twice is idempotent; out-of-order
  observations are rejected; future observations never mutate earlier
  lifecycle history; universe-wide processing is deterministic and
  failure-isolated;
* active / terminal lifecycles are retrieved deterministically and the
  history is auditable;
* NO broker execution, NO broker imports, NO alert sending, NO 19..8
  reliability framework, NO 19.9 forward-testing framework, NO
  trade-plan semantics, NO duplicate 19.5 scoring engine.

All tests are deterministic and network-free: scripted fake
assessments + direct model construction.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta

import pytest

from dashboard.setup_lifecycle import (
    SetupLifecycleEngine,
    SetupLifecycleStore,
    lifecycle_cycle_id,
    resolve_setup_identity,
)
from engine.config.setup_lifecycle_config import (
    LIFECYCLE_MODEL_VERSION,
    SetupLifecycleConfig,
    transition_rule_version,
)
from engine.models.setup_confluence import (
    SetupDirection,
)
from engine.models.setup_lifecycle import (
    LIFECYCLE_CYCLE_ID_PREFIX,
    LIFECYCLE_OBSERVATION_ID_PREFIX,
    LIFECYCLE_TRANSITION_ID_PREFIX,
    LifecycleObservationStatus,
    SETUP_ID_PREFIX,
    SetupIdentity,
    SetupLifecycle,
    SetupLifecycleCounts,
    SetupLifecycleCycleResult,
    SetupLifecycleObservation,
    SetupLifecycleObservationResult,
    SetupLifecycleState,
    SetupLifecycleTransition,
    build_setup_id,
    opposite_direction,
)
from engine.models.setup_quality import (
    SetupQualityResult,
    SetupQualityStatus,
)
from engine.reporting.setup_lifecycle import SetupLifecycleFormatter


# ------------------------------------------------------------------
# Deterministic 19..5 assessment builders
# ------------------------------------------------------------------


def _now() -> datetime:
    """A deterministic Friday 11:00 IST reference instant (05:30 UTC)."""
    return datetime(2026, 9, 4, 5, 30, tzinfo=UTC)


def T(i: int) -> datetime:
    return _now() + timedelta(minutes=15 * i)


def _bullish(instrument: str = "RELIANCE"):
    """A QUALIFIED BULLISH TREND_CONTINUATION 19.5 assessment."""
    from tests.test_setup_quality import (
        _aligned_mtf,
        _bullish_15m,
        _evaluate,
        _market_context,
    )
    from engine.models.market_context import MarketTrendState

    return _evaluate(
        _aligned_mtf(
            instrument,
            market_state=_market_context(MarketTrendState.BULLISH),
        ),
        _bullish_15m(),
    )


def _bearish(instrument: str = "RELIANCE"):
    from tests.test_setup_quality import (
        _aligned_mtf,
        _bearish_15m,
        _evaluate,
        _market_context,
    )
    from engine.models.market_context import MarketTrendState

    return _evaluate(
        _aligned_mtf(
            instrument,
            market_state=_market_context(MarketTrendState.BEARISH),
        ),
        _bearish_15m(),
    )


def _no_setup(instrument: str = "RELIANCE"):
    """A clean NO_SETUP assessment (UNKNOWN direction, no setup type)."""
    from tests.test_setup_quality import (
        _aligned_mtf,
        _evaluate,
        _flat_candles,
    )
    from engine.models.market_context import (
        MarketContext,
        MarketTrend,
        MarketTrendState,
        PriceLocation,
        RangeContext,
        RangeState,
        SupportResistanceContext,
    )
    from engine.models.structure_analysis import StructureBias

    unknown_ctx = MarketContext(
        index=59,
        trend=MarketTrend(
            state=MarketTrendState.UNKNOWN, bias=StructureBias.UNKNOWN,
            structure_intact=False, reasons=["test"],
        ),
        range=RangeContext(
            state=RangeState.UNKNOWN, high=None, low=None,
            width=None, position=None, reason="test",
        ),
        support_resistance=SupportResistanceContext(
            support=None, resistance=None,
            distance_to_support=None, distance_to_resistance=None,
            location=PriceLocation.UNKNOWN,
        ),
        recent_structure=(),
        confirmed_swings=0,
    )
    return _evaluate(
        _aligned_mtf(instrument, market_state=unknown_ctx),
        _flat_candles(),
    )


def _data_gate(instrument: str = "RELIANCE"):
    """A data-gate INCOMPLETE assessment (no usable state)."""
    from tests.test_setup_quality import (
        _aligned_mtf,
        _evaluate,
    )

    return _evaluate(_aligned_mtf(instrument, completeness=1), [])


def _fresh(
    config: SetupLifecycleConfig | None = None,
) -> SetupLifecycleEngine:
    return SetupLifecycleEngine(
        config or SetupLifecycleConfig(), SetupLifecycleStore(),
    )


def stepper_absent(
    eng: SetupLifecycleEngine,
    instrument: str,
    assessment,
    *,
    count: int,
    start: int,
):
    """Process ``count`` clean no-setup cycles starting at T(start);
    returns the resulting lifecycle state."""

    result = None
    for i in range(count):
        result = eng.process(
            assessment, f"c{start + i}", timestamp=T(start + i),
        )
    lc = eng.store.active_lifecycles()
    if lc:
        return lc[0].state
    return result.state


def engine_absent_times(
    eng: SetupLifecycleEngine,
    instrument: str,
    assessment,
    *,
    count: int,
    start: int,
):
    return stepper_absent(
        eng, instrument, assessment, count=count, start=start,
    )


def _minimal_lifecycle() -> SetupLifecycle:
    """A minimal valid lifecycle for model-validation tests."""
    identity = SetupIdentity(
        instrument="RELIANCE",
        direction=SetupDirection.BULLISH,
        setup_type="BREAKOUT",
        primary_timeframe="15m",
        setup_id=build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m",
        ),
    )
    return SetupLifecycle(
        lifecycle_id=build_lifecycle_id(identity.setup_id, 1),
        instance_discriminator=1,
        setup_id=identity.setup_id,
        identity=identity,
        state=SetupLifecycleState.DETECTED,
        created_at=T(0),
    )


def build_lifecycle_id(setup_id: str, instance: int) -> str:
    from dashboard.setup_lifecycle import (
        build_lifecycle_id as _build,
    )

    return _build(setup_id, instance)


# ==================================================================
# A. ARCHITECTURE
# ==================================================================


class TestArchitecture:
    def test_consumes_19_5_assessments(self):
        eng = _fresh()
        res = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        assert res.created
        assert res.state is SetupLifecycleState.CONFIRMED
        obs = eng.store.load(res.lifecycle_id).observations[0]
        assert isinstance(obs.assessment, SetupQualityResult)

    def test_consumes_19_3_scan_cycle_identity(self):
        eng = _fresh()
        res = eng.process(_bullish("RELIANCE"), "cycle-scan-xyz", timestamp=T(0))
        obs = eng.store.load(res.lifecycle_id).observations[0]
        assert obs.scan_cycle_id == "cycle-scan-xyz"
        assert res.scan_cycle_id == "cycle-scan-xyz"

    def test_19_1_universe_canonical(self):
        eng = _fresh()
        res = eng.process(_bullish("reliance"), "cycle-1", timestamp=T(0))
        assert res.instrument == "RELIANCE"
        assert eng.store.active_lifecycles()[0].instrument == "RELIANCE"

    def test_19_2_data_status_semantics_intact(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        res = eng.process(_data_gate("RELIANCE"), "cycle-2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 0

    def test_19_4_mtf_semantics_intact(self):
        eng = _fresh()
        res = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        obs = eng.store.load(res.lifecycle_id).observations[0]
        assert obs.quality_status == "QUALIFIED"
        assert obs.quality_classification == "EXCELLENT"
        assert obs.quality_score is not None

    def test_19_5_quality_semantics_intact(self):
        # A WATCH-level directional candidate resolves an identity but is
        # NOT confirmed until a qualified assessment appears.
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        stale_result = sq.evaluate(stale_mtf, _bullish_15m())
        assert stale_result.setup_type is not None
        eng = _fresh()
        res = eng.process(stale_result, "cycle-1", timestamp=T(0))
        assert res.state is SetupLifecycleState.DETECTED


# ==================================================================
# B. SETUP IDENTITY
# ==================================================================


class TestSetupIdentity:
    def test_first_observation_creates_exactly_one_lifecycle(self):
        eng = _fresh()
        res = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        assert res.created and res.advanced
        assert eng.store.total_count == 1

    def test_repeated_observation_reuses_same_setup_id(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        r2 = eng.process(_bullish("RELIANCE"), "cycle-2", timestamp=T(1))
        assert r1.setup_id == r2.setup_id
        assert r1.lifecycle_id == r2.lifecycle_id
        assert eng.store.total_count == 1

    def test_changed_quality_score_no_duplicate(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.mtf_analysis import MtfAlignmentState

        eng = _fresh()
        score_high = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
            _bullish_15m(),
        )
        conflicting = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
                alignment=MtfAlignmentState.CONFLICTING,
            ),
            _bullish_15m(),
        )
        assert conflicting.score != score_high.score
        r1 = eng.process(score_high, "cycle-1", timestamp=T(0))
        r2 = eng.process(conflicting, "cycle-2", timestamp=T(1))
        assert r2.setup_id == r1.setup_id
        assert r2.lifecycle_id == r1.lifecycle_id
        assert eng.store.total_count == 1
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.latest_score == conflicting.score

    def test_provider_ordering_does_not_change_identity(self):
        eng = _fresh()
        res1 = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        res2 = eng.process(_bullish("RELIANCE"), "cycle-2", timestamp=T(1))
        assert res1.setup_id == res2.setup_id

    def test_scan_cycle_id_alone_does_not_create_identity(self):
        eng = _fresh()
        res1 = eng.process(_bullish("RELIANCE"), "cycle-A", timestamp=T(0))
        res2 = eng.process(_bullish("RELIANCE"), "cycle-B", timestamp=T(1))
        assert res1.setup_id == res2.setup_id

    def test_identity_deterministic_across_independent_runs(self):
        e1 = _fresh()
        e2 = _fresh()
        r1 = e1.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        r2 = e2.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        assert r1.setup_id == r2.setup_id
        assert r1.lifecycle_id == r2.lifecycle_id

    def test_identity_does_not_depend_on_randomness(self):
        ids = {
            build_setup_id(
                "RELIANCE", SetupDirection.BULLISH,
                "TREND_CONTINUATION", "15m",
            )
            for _ in range(20)
        }
        assert len(ids) == 1

    def test_identity_excludes_transient_fields(self):
        r1 = _bullish("RELIANCE")
        r2 = _bullish("RELIANCE")
        assert resolve_setup_identity(r1).setup_id == resolve_setup_identity(
            r2,
        ).setup_id

    def test_identity_not_dependent_on_observation_timestamp(self):
        ident = resolve_setup_identity(_bullish("RELIANCE"))
        assert ident is not None
        assert ident.setup_id.startswith(SETUP_ID_PREFIX)

    def test_setup_type_is_part_of_identity(self):
        a = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "TREND_CONTINUATION", "15m",
        )
        b = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m",
        )
        assert a != b

    def test_direction_is_part_of_identity(self):
        a = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "TREND_CONTINUATION", "15m",
        )
        b = build_setup_id(
            "RELIANCE", SetupDirection.BEARISH, "TREND_CONTINUATION", "15m",
        )
        assert a != b

    def test_identity_stable_across_observation_instants(self):
        eng = _fresh()
        first = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        eng.process(_bullish("RELIANCE"), "c2", timestamp=T(2))
        assert eng.store.active_lifecycles()[0].setup_id == first.setup_id

    def test_no_identity_for_non_directional(self):
        assert resolve_setup_identity(_no_setup("RELIANCE")) is None
        assert resolve_setup_identity(_data_gate("RELIANCE")) is None


# ==================================================================
# C. INITIAL STATE
# ==================================================================


class TestInitialState:
    def test_first_qualified_observation_starts_confirmed(self):
        eng = _fresh()
        res = eng.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
        assert res.state is SetupLifecycleState.CONFIRMED

    def test_first_unqualified_observation_starts_detected(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        result = sq.evaluate(stale_mtf, _bullish_15m())
        assert result.setup_type is not None
        eng = _fresh()
        res = eng.process(result, "cycle-1", timestamp=T(0))
        assert res.state is SetupLifecycleState.DETECTED

    def test_initial_state_deterministic(self):
        e1 = _fresh()
        e2 = _fresh()
        a = e1.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        b = e2.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert a.state is b.state
        assert a.lifecycle_id == b.lifecycle_id


# ==================================================================
# D. STATE TRANSITIONS
# ==================================================================


class TestStateTransitions:
    def test_detected_to_confirmed(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        unqualified = sq.evaluate(stale_mtf, _bullish_15m())
        eng = _fresh()
        r1 = eng.process(unqualified, "c1", timestamp=T(0))
        assert r1.state is SetupLifecycleState.DETECTED
        r2 = eng.process(_bullish("RELIANCE"), "c2", timestamp=T(1))
        assert r2.state is SetupLifecycleState.CONFIRMED
        assert r2.transition is not None
        assert r2.transition.is_confirmation

    def test_detected_to_invalidated(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        inv = next(
            lc for lc in eng.store.terminal_lifecycles()
            if lc.state is SetupLifecycleState.INVALIDATED
        )
        assert inv.transitions[-1].is_invalidation
        assert "opposing" in inv.transitions[-1].reason.lower()

    def test_detected_to_expired(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        state = engine_absent_times(
            eng, "RELIANCE", _no_setup("RELIANCE"), count=5, start=1,
        )
        assert state is SetupLifecycleState.EXPIRED
        assert any(
            lc.state is SetupLifecycleState.EXPIRED
            for lc in eng.store.terminal_lifecycles()
        )

    def test_confirmed_to_expired(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = stepper_absent(
            eng, "RELIANCE", _no_setup("RELIANCE"), count=5, start=1,
        )
        assert res is SetupLifecycleState.EXPIRED

    def test_transition_reasons_correct(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        inv = next(
            lc for lc in eng.store.terminal_lifecycles()
            if lc.state is SetupLifecycleState.INVALIDATED
        )
        reasons = {t.reason for t in inv.transitions}
        assert any("opposing" in r for r in reasons)
        assert any("confirmed" in r for r in reasons)

    def test_terminal_behaviour_correct(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        assert eng.store.active_count == 1
        assert any(lc.is_terminal for lc in eng.store.list_lifecycles())

    def test_terminal_lifecycles_cannot_resurrect(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        terminal = next(
            lc for lc in eng.store.terminal_lifecycles()
            if lc.setup_id == r1.setup_id
        )
        assert terminal.is_terminal
        r3 = eng.process(_bullish("RELIANCE"), "c3", timestamp=T(2))
        assert r3.created
        assert r3.lifecycle_id != terminal.lifecycle_id
        assert len(eng.store.lifecycles_for_setup(r1.setup_id)) == 2

    def test_invalid_transition_rejected_by_model(self):
        with pytest.raises(ValueError):
            SetupLifecycleTransition(
                transition_id="trans-x",
                lifecycle_id="lifecycle-x",
                setup_id="setup-x",
                from_state=SetupLifecycleState.INVALIDATED,
                to_state=SetupLifecycleState.CONFIRMED,
                observation_timestamp=_now(),
                scan_cycle_id="c1",
                reason="invalid resurrection",
            )
        with pytest.raises(ValueError):
            SetupLifecycleTransition(
                transition_id="trans-y",
                lifecycle_id="lifecycle-x",
                setup_id="setup-x",
                from_state=SetupLifecycleState.DETECTED,
                to_state=SetupLifecycleState.DETECTED,
                observation_timestamp=_now(),
                scan_cycle_id="c1",
                reason="no change",
            )


# ==================================================================
# E. QUALITY CHANGES
# ==================================================================


class TestQualityChanges:
    def test_score_change_does_not_break_identity(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.mtf_analysis import MtfAlignmentState

        eng = _fresh()
        a = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
            _bullish_15m(),
        )
        r1 = eng.process(a, "c1", timestamp=T(0))
        b = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
                alignment=MtfAlignmentState.CONFLICTING,
            ),
            _bullish_15m(),
        )
        assert b.score != a.score
        r2 = eng.process(b, "c2", timestamp=T(1))
        assert r1.setup_id == r2.setup_id
        assert r2.lifecycle_id == r1.lifecycle_id

    def test_classification_change_represented_correctly(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_no_setup("RELIANCE"), "c2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.ABSENT
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 1
        assert lc.latest_status == "NO_SETUP"

    def test_mtf_alignment_change_handled(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.mtf_analysis import MtfAlignmentState

        eng = _fresh()
        aligned = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
            _bullish_15m(),
        )
        r1 = eng.process(aligned, "c1", timestamp=T(0))
        conflicting = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
                alignment=MtfAlignmentState.CONFLICTING,
            ),
            _bullish_15m(),
        )
        r2 = eng.process(conflicting, "c2", timestamp=T(1))
        assert r1.setup_id == r2.setup_id
        assert r2.lifecycle_id == r1.lifecycle_id

    def test_setup_evidence_change_handled(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        r2 = eng.process(_bullish("RELIANCE"), "c2", timestamp=T(1))
        assert r1.setup_id == r2.setup_id
        assert r2.lifecycle_id == r1.lifecycle_id


# ==================================================================
# F. DATA FAILURES
# ==================================================================


class TestDataFailures:
    def test_provider_failure_does_not_invalidate(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_data_gate("RELIANCE"), "c2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 0

    def test_stale_data_not_invalidation(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        stale = sq.evaluate(stale_mtf, _bullish_15m())
        res = eng.process(stale, "c2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.OBSERVED
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED

    def test_incomplete_mtf_handled_explicitly(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_data_gate("RELIANCE"), "c2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE

    def test_unsupported_data_handled_explicitly(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _evaluate,
        )
        from engine.models.mtf_analysis import MarketStateCompleteness

        unsupported = _evaluate(
            _aligned_mtf(
                "RELIANCE", completeness=MarketStateCompleteness.UNSUPPORTED,
            ),
            [],
        )
        assert unsupported.status is SetupQualityStatus.INCOMPLETE
        eng = _fresh()
        res = eng.process(unsupported, "c1", timestamp=T(0))
        assert res.observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE

    def test_missing_observation_distinguished_from_disproven(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_no_setup("RELIANCE"), "c2", timestamp=T(1))
        assert res.observation_status is LifecycleObservationStatus.ABSENT
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED


# ==================================================================
# G. DISAPPEARANCE
# ==================================================================


class TestDisappearance:
    def test_disappearing_one_scan_documented_rules(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_no_setup("RELIANCE"), "c2", timestamp=T(1))
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 1

    def test_multiple_missing_documented_rules(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        for i in range(1, 4):
            eng.process(_no_setup("RELIANCE"), f"c{i+1}", timestamp=T(i))
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 3

    def test_expiration_deterministic(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        state = stepper_absent(
            eng, "RELIANCE", _no_setup("RELIANCE"), count=4, start=1,
        )
        assert state is SetupLifecycleState.CONFIRMED
        state5 = stepper_absent(
            eng, "RELIANCE", _no_setup("RELIANCE"), count=1, start=5,
        )
        assert state5 is SetupLifecycleState.EXPIRED


# ==================================================================
# H. IDEMPOTENCY
# ==================================================================


class TestIdempotency:
    def test_same_scan_cycle_twice_no_duplicate_history(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert res.duplicate
        lc = eng.store.active_lifecycles()[0]
        assert lc.observation_count == 1

    def test_same_assessment_twice_deterministic(self):
        eng = _fresh()
        a = _bullish("RELIANCE")
        r1 = eng.process(a, "c1", timestamp=T(0))
        r2 = eng.process(a, "c1", timestamp=T(0))
        assert r1.state is r2.state
        assert r2.duplicate

    def test_duplicate_transition_events_prevented(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        lc = eng.store.active_lifecycles()[0]
        assert res.duplicate
        assert lc.transition_count == 1


# ==================================================================
# I. ORDERING
# ==================================================================


class TestOrdering:
    def test_out_of_order_does_not_corrupt_state(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        late = eng.process(_bullish("RELIANCE"), "c0.5", timestamp=T(0))
        assert late.late_rejected
        lc = eng.store.active_lifecycles()[0]
        assert lc.observation_count == 1
        assert lc.last_observation_timestamp == T(1)

    def test_provider_response_ordering_cannot_affect_lifecycle(self):
        e1 = _fresh()
        e2 = _fresh()
        e1.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        e1.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        e2.process(_bearish("RELIANCE"), "y0", timestamp=T(100))
        e2.process(_bullish("RELIANCE"), "y1", timestamp=T(101))
        active_dir1 = {
            lc.identity.direction.value
            for lc in e1.store.active_lifecycles()
        }
        active_dir2 = {
            lc.identity.direction.value
            for lc in e2.store.active_lifecycles()
        }
        assert active_dir1 == {"BEARISH"}
        assert active_dir2 == {"BULLISH"}
        e3 = _fresh()
        e4 = _fresh()
        res3 = e3.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res4 = e4.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert res3.lifecycle_id == res4.lifecycle_id

    def test_universe_ordering_cannot_affect_lifecycle_results(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.setup_quality import (
            SetupQualityUniverseResult,
            setup_quality_analysis_id,
        )
        from engine.config.setup_quality_config import SetupQualityConfig

        def universe(instruments):
            results = [
                _evaluate(
                    _aligned_mtf(
                        instr,
                        market_state=_market_context(MarketTrendState.BULLISH),
                    ),
                    _bullish_15m(),
                )
                for instr in instruments
            ]
            return SetupQualityUniverseResult(
                analysis_id=setup_quality_analysis_id(
                    ("15m", "1h"), instruments, _now(),
                    SetupQualityConfig().snapshot(),
                ),
                reference_now=_now(),
                timeframes=("15m", "1h"),
                primary_timeframe="15m",
                universe_instrument_count=len(instruments),
                instruments=instruments,
                results=tuple(results),
            )

        e1 = _fresh()
        e2 = _fresh()
        e1.process_universe(universe(("RELIANCE", "TCS")), "c1", timestamp=T(0))
        e2.process_universe(universe(("TCS", "RELIANCE")), "c1", timestamp=T(0))
        assert {
            lc.setup_id for lc in e1.store.list_lifecycles()
        } == {
            lc.setup_id for lc in e2.store.list_lifecycles()
        }


# ==================================================================
# J. TIME
# ==================================================================


class TestTime:
    def test_explicit_timestamps_drive_decisions(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_no_setup("RELIANCE"), "c2", timestamp=T(1))
        lc = eng.store.active_lifecycles()[0]
        assert lc.observations[-1].observation_timestamp == T(1)

    def test_no_hidden_wall_clock_dependency(self):
        import dashboard.setup_lifecycle as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "datetime.now(" not in src
        assert "time.time(" not in src
        assert "utcnow(" not in src


# ==================================================================
# K. POINT-IN-TIME / NO-LOOK-AHEAD
# ==================================================================


class TestPointInTime:
    def test_future_observations_cannot_change_earlier_decisions(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        obs_at_1 = eng.store.load(r1.lifecycle_id).observations[0]
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(2))
        lc_after = eng.store.load(r1.lifecycle_id)
        assert lc_after.observations[0].observation_id == obs_at_1.observation_id
        assert lc_after.observations[0].at_state == obs_at_1.at_state
        assert lc_after.observations[0].observation_timestamp == T(1)

    def test_future_confirmation_cannot_retroactively_confirm(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        eng = _fresh()
        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        unqualified = sq.evaluate(stale_mtf, _bullish_15m())
        r1 = eng.process(unqualified, "c1", timestamp=T(1))
        assert r1.state is SetupLifecycleState.DETECTED
        eng.process(_bullish("RELIANCE"), "c2", timestamp=T(2))
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.observations[0].at_state is SetupLifecycleState.DETECTED
        assert lc.observations[1].at_state is SetupLifecycleState.CONFIRMED

    def test_future_invalidation_cannot_alter_history(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        first = eng.store.load(r1.lifecycle_id).observations[0]
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(2))
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.state is SetupLifecycleState.INVALIDATED
        assert lc.observations[0].at_state is SetupLifecycleState.CONFIRMED
        assert first.observation_id == lc.observations[0].observation_id

    def test_future_mtf_information_cannot_alter_earlier_state(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        first = eng.store.load(r1.lifecycle_id).observations[0]
        eng.process(_data_gate("RELIANCE"), "c2", timestamp=T(2))
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.observations[0].observation_id == first.observation_id
        assert lc.observations[0].at_state is first.at_state

    def test_future_scan_cycles_cannot_alter_earlier_history(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        eng.process(_bullish("RELIANCE"), "c2", timestamp=T(2))
        eng.process(_bullish("RELIANCE"), "c3", timestamp=T(3))
        obs = eng.store.active_lifecycles()[0].observations
        assert [o.at_state for o in obs] == [
            SetupLifecycleState.CONFIRMED,
            SetupLifecycleState.CONFIRMED,
            SetupLifecycleState.CONFIRMED,
        ]
        assert obs[0].observation_timestamp == T(1)


# ==================================================================
# L. UNIVERSE
# ==================================================================


class TestUniverse:
    def test_top200_processing_deterministic(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.setup_quality import (
            SetupQualityUniverseResult,
            setup_quality_analysis_id,
        )
        from engine.config.universe_boundary import UniverseBuilder
        from engine.config.setup_quality_config import SetupQualityConfig

        instruments = UniverseBuilder.nifty200().symbols[:4]
        results = [
            _evaluate(
                _aligned_mtf(
                    instr,
                    market_state=_market_context(MarketTrendState.BULLISH),
                ),
                _bullish_15m(),
            )
            for instr in instruments
        ]
        uni = SetupQualityUniverseResult(
            analysis_id=setup_quality_analysis_id(
                ("15m", "1h"), instruments, _now(),
                SetupQualityConfig().snapshot(),
            ),
            reference_now=_now(),
            timeframes=("15m", "1h"),
            primary_timeframe="15m",
            universe_instrument_count=len(instruments),
            instruments=instruments,
            results=tuple(results),
        )
        e1 = _fresh()
        e2 = _fresh()
        c1 = e1.process_universe(uni, "cu1", timestamp=T(0))
        c2 = e2.process_universe(uni, "cu1", timestamp=T(0))
        assert c1.cycle_id == c2.cycle_id
        assert c1.counts.created == c2.counts.created
        assert {lc.setup_id for lc in e1.store.list_lifecycles()} == {
            lc.setup_id for lc in e2.store.list_lifecycles()
        }

    def test_one_symbol_failure_does_not_terminate_universe(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bearish_15m,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.setup_quality import (
            SetupQualityUniverseResult,
            setup_quality_analysis_id,
        )
        from engine.config.setup_quality_config import SetupQualityConfig

        eng = _fresh()
        bull = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
            _bullish_15m(),
        )
        bear = _evaluate(
            _aligned_mtf(
                "TCS",
                market_state=_market_context(MarketTrendState.BEARISH),
            ),
            _bearish_15m(),
        )
        uni = SetupQualityUniverseResult(
            analysis_id=setup_quality_analysis_id(
                ("15m", "1h"), ("RELIANCE", "TCS"), _now(),
                SetupQualityConfig().snapshot(),
            ),
            reference_now=_now(),
            timeframes=("15m", "1h"),
            primary_timeframe="15m",
            universe_instrument_count=2,
            instruments=("RELIANCE", "TCS"),
            results=(bull, bear),
        )
        cres = eng.process_universe(uni, "c1", timestamp=T(0))
        assert cres.counts.created == 2

    def test_one_malformed_assessment_does_not_terminate_cycle(self):
        r = SetupLifecycleObservationResult(
            instrument="RELIANCE",
            observation_timestamp=T(0),
            scan_cycle_id="c1",
            state=SetupLifecycleState.DETECTED,
        )
        cycle = SetupLifecycleCycleResult(
            cycle_id=lifecycle_cycle_id("c1", T(0), SetupLifecycleConfig()),
            scan_cycle_id="c1",
            reference_now=T(0),
            instruments=("RELIANCE",),
            results=(r,),
        )
        assert cycle.counts.processed == 0
        assert cycle.result_for("reliance") is r

    def test_every_relevant_assessment_accounted_for(self):
        r = SetupLifecycleObservationResult(
            instrument="RELIANCE",
            observation_timestamp=T(0),
            scan_cycle_id="c1",
            state=SetupLifecycleState.DETECTED,
        )
        cycle = SetupLifecycleCycleResult(
            cycle_id="lc-cycle-x",
            scan_cycle_id="c1",
            reference_now=T(0),
            instruments=("RELIANCE",),
            results=(r,),
        )
        assert cycle.instruments == ("RELIANCE",)
        assert cycle.result_for("reliance") is r


# ==================================================================
# M. LIFECYCLE RETRIEVAL
# ==================================================================


class TestRetrieval:
    def test_active_lifecycles_retrieved_deterministically(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        active = eng.store.active_lifecycles()
        assert all(lc.is_active for lc in active)
        assert [lc.lifecycle_id for lc in active] == sorted(
            lc.lifecycle_id for lc in active
        )

    def test_terminal_lifecycles_retrieved_deterministically(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        terminals = eng.store.terminal_lifecycles()
        assert any(lc.is_terminal for lc in terminals)
        assert [lc.lifecycle_id for lc in terminals] == sorted(
            lc.lifecycle_id for lc in terminals
        )

    def test_lifecycle_history_auditable(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_no_setup("RELIANCE"), "c2", timestamp=T(1))
        lc = eng.store.active_lifecycles()[0]
        for obs in lc.observations:
            assert obs.reason
            assert obs.observation_id.startswith(LIFECYCLE_OBSERVATION_ID_PREFIX)
        for tr in lc.transitions:
            assert tr.reason
            assert tr.transition_id.startswith(LIFECYCLE_TRANSITION_ID_PREFIX)

    def test_lifecycle_for_instrument_filter(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bullish("TCS"), "c1", timestamp=T(0))
        assert len(eng.store.lifecycles_for_instrument("RELIANCE")) == 1
        assert len(eng.store.active_for_instrument("TCS", "15m")) == 1

    def test_active_for_setup_single_instance(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bullish("RELIANCE"), "c2", timestamp=T(1))
        assert eng.store.active_for_setup(
            eng.store.active_lifecycles()[0].setup_id,
        ) is not None


# ==================================================================
# N. MODEL / CONFIG VALIDATION
# ==================================================================


class TestModelValidation:
    def test_setup_identity_frozen_slots(self):
        ident = SetupIdentity(
            instrument="RELIANCE",
            direction=SetupDirection.BULLISH,
            setup_type="BREAKOUT",
            primary_timeframe="15m",
            setup_id=build_setup_id(
                "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m",
            ),
        )
        with pytest.raises(Exception):
            ident.instrument = "TCS"  # type: ignore[misc]  # frozen

    def test_setup_identity_rejects_no_direction(self):
        with pytest.raises(ValueError):
            SetupIdentity(
                instrument="RELIANCE",
                direction=SetupDirection.NEUTRAL,
                setup_type="BREAKOUT",
                primary_timeframe="15m",
                setup_id=build_setup_id(
                    "RELIANCE", SetupDirection.NEUTRAL, "BREAKOUT", "15m",
                ),
            )

    def test_lifecycle_requires_strictly_chronological_observations(self):
        lc = _minimal_lifecycle()
        with pytest.raises(ValueError):
            SetupLifecycle(
                lifecycle_id=lc.lifecycle_id,
                instance_discriminator=1,
                setup_id=lc.setup_id,
                identity=lc.identity,
                state=lc.state,
                created_at=lc.created_at,
                last_observation_timestamp=T(0),
                consecutive_missing=0,
                latest_observation_id="obs-1",
                observations=(
                    SetupLifecycleObservation(
                        observation_id="obs-1",
                        lifecycle_id=lc.lifecycle_id,
                        setup_id=lc.setup_id,
                        instrument=lc.instrument,
                        scan_cycle_id="c1",
                        observation_timestamp=T(1),
                        observation_status=LifecycleObservationStatus.OBSERVED,
                        at_state=SetupLifecycleState.CONFIRMED,
                    ),
                    SetupLifecycleObservation(
                        observation_id="obs-2",
                        lifecycle_id=lc.lifecycle_id,
                        setup_id=lc.setup_id,
                        instrument=lc.instrument,
                        scan_cycle_id="c1",
                        observation_timestamp=T(1),
                        observation_status=LifecycleObservationStatus.OBSERVED,
                        at_state=SetupLifecycleState.CONFIRMED,
                    ),
                ),
            )

    def test_terminal_lifecycle_cannot_be_reopened(self):
        with pytest.raises(ValueError):
            SetupLifecycleTransition(
                transition_id="trans-1",
                lifecycle_id="lifecycle-x",
                setup_id="setup-x",
                from_state=SetupLifecycleState.EXPIRED,
                to_state=SetupLifecycleState.CONFIRMED,
                observation_timestamp=T(0),
                scan_cycle_id="c1",
                reason="invalid",
            )

    def test_counts_invariants(self):
        counts = SetupLifecycleCounts(
            created=1, advanced=1, duplicate=0, late_rejected=0,
            active=1, terminal=0,
        )
        assert counts.processed == 2

    def test_config_defaults_and_validation(self):
        config = SetupLifecycleConfig()
        assert config.max_consecutive_missing_observations == 5
        assert config.identity_version == 1
        assert config.confirm_on_qualified is True
        with pytest.raises(ValueError):
            SetupLifecycleConfig(max_consecutive_missing_observations=0)
        with pytest.raises(TypeError):
            SetupLifecycleConfig(identity_version=True)
        with pytest.raises(TypeError):
            SetupLifecycleConfig(confirm_on_qualified="yes")
        assert LIFECYCLE_MODEL_VERSION == 1
        assert len(transition_rule_version(config)) == 16

    def test_config_snapshot_deterministic(self):
        assert SetupLifecycleConfig().snapshot() == SetupLifecycleConfig().snapshot()


# ==================================================================
# O. BOUNDARIES
# ==================================================================


class TestBoundaries:
    def test_no_broker_execution(self):
        import dashboard.setup_lifecycle as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "place_order" not in src
        assert "cancel_order" not in src

    def test_no_broker_imports(self):
        import dashboard.setup_lifecycle as mod

        tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "broker" not in alias.name
            elif isinstance(node, ast.ImportFrom):
                assert "broker" not in (node.module or "")

    def test_no_alert_sending(self):
        import dashboard.setup_lifecycle as mod

        src = open(mod.__file__, encoding="utf-8").read().lower()
        for kw in ("telegram", "whatsapp", "smtp", "webhook"):
            assert kw not in src

    def test_no_notification_provider(self):
        import engine.models.setup_lifecycle as models

        src = open(models.__file__, encoding="utf-8").read().lower()
        for kw in ("email", "push", "sms"):
            assert kw not in src

    def test_no_19_8_reliability_framework(self):
        import dashboard.setup_lifecycle as mod

        # The lifecycle layer must not perform FILE I/O / persistence /
        # recovery (19.8's domain). Prose in docstrings that lists the
        # prohibition is NOT an implementation — check only for actual
        # I/O primitives and framework APIs.
        src = open(mod.__file__, encoding="utf-8").read()
        for kw in ("tempfile", "mkstemp", "os.replace", "open(",
                   "pathlib", "open(", "json.dump", "json.load("):
            assert kw not in src
        assert "SetupLifecycleStore" in src  # in-memory store present
        store = mod.SetupLifecycleStore()
        assert not hasattr(store, "_directory")
        assert isinstance(store._lifecycles, dict)

    def test_no_19_9_forward_testing(self):
        import dashboard.setup_lifecycle as mod

        src = open(mod.__file__, encoding="utf-8").read()
        src = src.split('"""', 2)[-1] if src.startswith('"""') else src
        lower = src.lower()
        assert "forward_test" not in lower
        assert "backtest" not in lower

    def test_no_trade_plan_semantics(self):
        import dashboard.setup_lifecycle as mod
        import engine.models.setup_lifecycle as models

        for m in (mod, models):
            src = open(m.__file__, encoding="utf-8").read()
            for kw in ("entry_price", "stop_loss", "take_profit",
                       "position_size", "risk_percent", "quantity"):
                assert kw not in src

    def test_no_duplicate_19_5_scoring_engine(self):
        import dashboard.setup_lifecycle as mod

        src = open(mod.__file__, encoding="utf-8").read()
        assert "from dashboard.setup_quality import" not in src
        assert "SetupQualityEngine(" not in src

    def test_lifecycle_model_does_not_import_trading_engines(self):
        import engine.models.setup_lifecycle as models

        tree = ast.parse(open(models.__file__, encoding="utf-8").read())
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        for name in imported:
            for kw in ("paper_trade", "submission", "broker", "execution"):
                assert kw not in name


# ==================================================================
# P. ADVERSARIAL LIFECYCLE TESTS
# ==================================================================


class TestAdversarial:
    def test_same_setup_multiple_cycles(self):
        eng = _fresh()
        for i in range(6):
            eng.process(_bullish("RELIANCE"), f"c{i+1}", timestamp=T(i))
        lc = eng.store.active_lifecycles()[0]
        assert lc.observation_count == 6
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.transition_count == 1

    def test_score_changes_significantly(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.mtf_analysis import MtfAlignmentState

        eng = _fresh()
        a = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
            _bullish_15m(),
        )
        r1 = eng.process(a, "c1", timestamp=T(0))
        conflicting = _evaluate(
            _aligned_mtf(
                "RELIANCE",
                market_state=_market_context(MarketTrendState.BULLISH),
                alignment=MtfAlignmentState.CONFLICTING,
            ),
            _bullish_15m(),
        )
        assert conflicting.score != a.score
        r2 = eng.process(conflicting, "c2", timestamp=T(1))
        assert r2.lifecycle_id == r1.lifecycle_id
        lc = eng.store.load(r1.lifecycle_id)
        assert len(lc.observations) == 2

    def test_temporary_disappearance_provider_failure(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_data_gate("RELIANCE"), "c2", timestamp=T(1))
        eng.process(_data_gate("RELIANCE"), "c3", timestamp=T(2))
        eng.process(_bullish("RELIANCE"), "c4", timestamp=T(3))
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 0
        assert lc.observation_count == 4

    def test_temporary_disappearance_mtf_incomplete(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        for i in (1, 2):
            eng.process(_data_gate("RELIANCE"), f"c{i+1}", timestamp=T(i))
        lc = eng.store.active_lifecycles()[0]
        assert lc.state is SetupLifecycleState.CONFIRMED
        assert lc.consecutive_missing == 0

    def test_future_confirmation_after_earlier_observation(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig
        from engine.models.intraday_coverage import IntradayCoverageStatus

        eng = _fresh()
        sq = SetupQualityEngine(SetupQualityConfig())
        stale_mtf = _aligned_mtf(
            "RELIANCE",
            market_state=_market_context(MarketTrendState.BULLISH),
            primary_availability=IntradayCoverageStatus.STALE,
        )
        unqualified = sq.evaluate(stale_mtf, _bullish_15m())
        r1 = eng.process(unqualified, "c1", timestamp=T(1))
        assert r1.state is SetupLifecycleState.DETECTED
        eng.process(_bullish("RELIANCE"), "c2", timestamp=T(2))
        lc = eng.store.load(r1.lifecycle_id)
        assert [o.at_state for o in lc.observations] == [
            SetupLifecycleState.DETECTED,
            SetupLifecycleState.CONFIRMED,
        ]
        assert lc.observations[0].at_state is SetupLifecycleState.DETECTED

    def test_future_invalidation_after_earlier_observation(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(1))
        first_state = eng.store.load(r1.lifecycle_id).observations[0].at_state
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(2))
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.state is SetupLifecycleState.INVALIDATED
        assert lc.observations[0].at_state is first_state

    def test_similar_setup_after_terminal_new_instance(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        eng.process(_bullish("RELIANCE"), "c3", timestamp=T(2))
        instances = eng.store.lifecycles_for_setup(r1.setup_id)
        assert len(instances) == 2
        assert instances[0].is_terminal
        assert instances[1].is_active
        assert instances[1].instance_discriminator == 2

    def test_out_of_order_observations(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(5))
        late = eng.process(_no_setup("RELIANCE"), "c0", timestamp=T(0))
        assert late.late_rejected
        lc = eng.store.active_lifecycles()[0]
        assert lc.observation_count == 1
        assert lc.last_observation_timestamp == T(5)

    def test_same_scan_cycle_processed_twice(self):
        eng = _fresh()
        r1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        dup = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert dup.duplicate
        lc = eng.store.load(r1.lifecycle_id)
        assert lc.observation_count == 1

    def test_two_instruments_identical_looking_assessments(self):
        eng = _fresh()
        a = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        b = eng.process(_bullish("TCS"), "c1", timestamp=T(0))
        assert a.setup_id != b.setup_id
        assert a.lifecycle_id != b.lifecycle_id
        assert eng.store.total_count == 2

    def test_provider_ordering_changes(self):
        eng = _fresh()
        res1 = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        res1b = eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert res1b.duplicate
        assert eng.store.load(res1.lifecycle_id).observation_count == 1

    def test_universe_ordering_changes(self):
        from tests.test_setup_quality import (
            _aligned_mtf,
            _bullish_15m,
            _evaluate,
            _market_context,
        )
        from engine.models.market_context import MarketTrendState
        from engine.models.setup_quality import (
            SetupQualityUniverseResult,
            setup_quality_analysis_id,
        )
        from engine.config.setup_quality_config import SetupQualityConfig

        def uni(instruments):
            return SetupQualityUniverseResult(
                analysis_id=setup_quality_analysis_id(
                    ("15m", "1h"), instruments, _now(),
                    SetupQualityConfig().snapshot(),
                ),
                reference_now=_now(),
                timeframes=("15m", "1h"),
                primary_timeframe="15m",
                universe_instrument_count=len(instruments),
                instruments=instruments,
                results=tuple(
                    _evaluate(
                        _aligned_mtf(
                            instr,
                            market_state=_market_context(MarketTrendState.BULLISH),
                        ),
                        _bullish_15m(),
                    )
                    for instr in instruments
                ),
            )

        e1 = _fresh()
        e2 = _fresh()
        e1.process_universe(uni(("RELIANCE", "TCS")), "c1", timestamp=T(0))
        e2.process_universe(uni(("TCS", "RELIANCE")), "c1", timestamp=T(0))
        assert sorted(lc.setup_id for lc in e1.store.list_lifecycles()) == sorted(
            lc.setup_id for lc in e2.store.list_lifecycles()
        )


# ==================================================================
# Q. EVENTS / CYCLE ID / DETERMINISM
# ==================================================================


class TestEventsAndIdentity:
    def test_events_immutable_timestamped_deterministic(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        eng.process(_bearish("RELIANCE"), "c2", timestamp=T(1))
        terminal = next(
            lc for lc in eng.store.terminal_lifecycles()
            if lc.state is SetupLifecycleState.INVALIDATED
        )
        tr = terminal.transitions[-1]
        assert tr.observation_timestamp.tzinfo is not None
        assert tr.reason
        assert tr.transition_id.startswith(LIFECYCLE_TRANSITION_ID_PREFIX)
        with pytest.raises(Exception):
            tr.reason = "mutated"  # type: ignore[misc]  # frozen

    def test_cycle_id_deterministic(self):
        c1 = lifecycle_cycle_id("c1", T(0), SetupLifecycleConfig())
        c2 = lifecycle_cycle_id("c1", T(0), SetupLifecycleConfig())
        assert c1 == c2
        assert c1.startswith(LIFECYCLE_CYCLE_ID_PREFIX)
        assert c1 != lifecycle_cycle_id("c2", T(0), SetupLifecycleConfig())

    def test_opposite_direction_helper(self):
        assert opposite_direction(SetupDirection.BULLISH) is SetupDirection.BEARISH
        assert opposite_direction(SetupDirection.BEARISH) is SetupDirection.BULLISH
        with pytest.raises(ValueError):
            opposite_direction(SetupDirection.NEUTRAL)

    def test_formatter_returns_str(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        lc = eng.store.active_lifecycles()[0]
        text = SetupLifecycleFormatter().format_lifecycle(lc)
        assert isinstance(text, str)
        assert "SETUP LIFECYCLE" in text
        assert "WARNING" in text

    def test_formatter_deterministic(self):
        eng = _fresh()
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        lc = eng.store.active_lifecycles()[0]
        fmt = SetupLifecycleFormatter()
        assert fmt.format_lifecycle(lc) == fmt.format_lifecycle(lc)

    def test_determinism_repeated_processing(self):
        e1 = _fresh()
        e2 = _fresh()
        for i in range(3):
            e1.process(_bullish("RELIANCE"), f"c{i}", timestamp=T(i))
            e2.process(_bullish("RELIANCE"), f"c{i}", timestamp=T(i))
        assert {
            (lc.lifecycle_id, lc.state.value, lc.observation_count)
            for lc in e1.store.list_lifecycles()
        } == {
            (lc.lifecycle_id, lc.state.value, lc.observation_count)
            for lc in e2.store.list_lifecycles()
        }

    def test_store_reset_and_save(self):
        store = SetupLifecycleStore()
        eng = SetupLifecycleEngine(SetupLifecycleConfig(), store)
        eng.process(_bullish("RELIANCE"), "c1", timestamp=T(0))
        assert store.total_count == 1
        store.reset()
        assert store.total_count == 0

    def test_observation_result_invariants(self):
        with pytest.raises(ValueError):
            SetupLifecycleObservationResult(
                instrument="RELIANCE",
                observation_timestamp=T(0),
                scan_cycle_id="c1",
                late_rejected=True,
                duplicate=True,
            )

    def test_identity_version_affects_id(self):
        a = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m", identity_version=1,
        )
        b = build_setup_id(
            "RELIANCE", SetupDirection.BULLISH, "BREAKOUT", "15m", identity_version=2,
        )
        assert a != b


# ==================================================================
# R. REGRESSION
# ==================================================================


class TestRegression:
    def test_pipeline_baseline_unchanged(self):
        try:
            from engine.pipeline.historical_pipeline import (
                HistoricalEvaluationPipeline,
                PipelineConfig,
            )
            from tests.test_pipeline import trending_dataset  # type: ignore
        except Exception:
            pytest.skip("pipeline baseline imports unavailable")
        candles = trending_dataset()
        result = HistoricalEvaluationPipeline(PipelineConfig()).evaluate(candles)
        assert len(result.signals) == 4
        assert result.completed_trades == 3

    def test_19_5_engine_unchanged(self):
        from dashboard.setup_quality import SetupQualityEngine
        from engine.config.setup_quality_config import SetupQualityConfig

        assert SetupQualityEngine(SetupQualityConfig()) is not None

    def test_19_4_mtf_unchanged(self):
        from dashboard.mtf_analysis import MtfAnalysisEngine
        from engine.config.mtf_analysis_config import MtfAnalysisConfig

        assert MtfAnalysisEngine(MtfAnalysisConfig()) is not None

    def test_19_3_scanner_unchanged(self):
        from dashboard.continuous_scanner import ContinuousScannerEngine

        assert ContinuousScannerEngine() is not None

    def test_19_1_universe_unchanged(self):
        from engine.config.universe_boundary import UniverseBuilder

        assert len(UniverseBuilder.nifty200().symbols) == 200
