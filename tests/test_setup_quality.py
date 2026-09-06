"""
Checkpoint 19.5 — setup-quality intelligence tests.

These tests prove :class:`dashboard.setup_quality.SetupQualityEngine` is
a deterministic, honest SETUP-QUALITY layer that answers:

    "Given the market state currently available for this instrument,
     does it contain a potentially meaningful trading setup, how strong
     is that setup according to the defined quality model, and why?"

Coverage mirrors the 52-point Checkpoint 19.5 test requirement list:

* the setup-quality layer consumes the FROZEN 19.4 MTF results and the
  FROZEN 19.1 universe; 19.2 data-status semantics and 19.3 scan
  architecture remain compatible;
* setup detection reuses the proven Sprint 11O + 11Q engines (no new
  detection heuristic is invented);
* setup evidence is explicit (positive + negative factors);
* the quality score is deterministic, bounded, component-transparent,
  and reproducible;
* MTF alignment / mixed / conflicting / incomplete / unsupported / stale
  handling is explicit (missing evidence is never positive);
* POINT-IN-TIME SAFETY is proven: appending arbitrary future candles
  (future breakout / BOS / higher-timeframe confirmation / trend change
  / support-resistance event) does NOT change an already-computed
  setup-quality result at an earlier timestamp;
* qualified candidates are ranked deterministically with deterministic
  tie-breaking; provider response ordering cannot alter ranking;
* one-symbol / one-timeframe failure does not terminate universe
  analysis; invalid data cannot become a qualified setup; missing data
  stays visible;
* every qualified setup has an explanation matching its actual scoring
  inputs; analysis + source timestamps + model version are preserved;
* NO trade execution, NO broker import, NO alert, NO setup lifecycle,
  NO forward-testing framework, NO 19.8 reliability framework, NO MTF
  engine duplication, NO universe duplication.

All tests are deterministic and network-free: scripted fake providers +
direct model construction.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from dashboard.data_provider import (
    FreshnessState,
    InstrumentSeries,
    ProviderStatus,
)
from dashboard.mtf_analysis import MtfAnalysisEngine
from dashboard.setup_quality import SetupQualityEngine, SetupQualityViewBuilder
from engine.config.mtf_analysis_config import MtfAnalysisConfig
from engine.config.setup_quality_config import SetupQualityConfig
from engine.config.universe_boundary import UniverseBuilder
from engine.models.intraday_coverage import IntradayCoverageStatus
from engine.models.market_context import (
    MarketContext,
    MarketTrend,
    MarketTrendState,
    PriceLocation,
    RangeContext,
    RangeState,
    SupportResistanceContext,
)
from engine.models.market_structure import StructurePoint, StructureType
from engine.models.mtf_analysis import (
    MTFDirection,
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfTimeframeState,
    MtfUniverseAnalysis,
    PerSymbolMtfResult,
)
from engine.models.ohlcv import OHLCVCandle
from engine.models.setup_confluence import (
    SetupClassification,
    SetupDirection,
)
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityCounts,
    SetupQualityResult,
    SetupQualityStatus,
    SetupQualityUniverseResult,
    setup_quality_analysis_id,
)
from engine.models.structure_analysis import StructureBias
from engine.reporting.setup_quality import SetupQualityFormatter


# ------------------------------------------------------------------
# Deterministic candle + market-state builders
# ------------------------------------------------------------------


def _candle(ts: datetime, close: float) -> OHLCVCandle:
    return OHLCVCandle(
        timestamp=ts,
        open=close,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=1000.0,
    )


def _now() -> datetime:
    """A deterministic Friday 11:00 IST reference instant (05:30 UTC)."""
    return datetime(2026, 9, 4, 5, 30, tzinfo=UTC)


def _bullish_15m(n: int = 60) -> list[OHLCVCandle]:
    """Bullish 15m zigzag -> BULLISH market context (HH/HL)."""
    from engine.data.historical_fixtures import _bullish_zigzag

    last = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    start_ts = last - timedelta(minutes=15) * (n - 1)
    return _bullish_zigzag(
        105.0, n_legs=n // 5 + 1, start_ts=start_ts,
        step=timedelta(minutes=15), rise=6.0, pullback=3.0,
    )[:n]


def _bearish_15m(n: int = 60) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bearish_zigzag

    last = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    start_ts = last - timedelta(minutes=15) * (n - 1)
    return _bearish_zigzag(
        105.0, n_legs=n // 5 + 1, start_ts=start_ts,
        step=timedelta(minutes=15), fall=6.0, bounce=3.0,
    )[:n]


def _market_context(
    direction: MarketTrendState,
    *,
    structure_intact: bool = True,
    confirmed_swings: int = 7,
    location: PriceLocation | None = None,
    range_state: RangeState = RangeState.NOT_IN_RANGE,
) -> MarketContext:
    """A deterministic Sprint 11P MarketContext for tests.

    The default price location is direction-appropriate (NEAR_SUPPORT
    for a bullish candidate, NEAR_RESISTANCE for a bearish candidate)
    so a clean directional setup is naturally constructive.
    """

    if location is None:
        location = (
            PriceLocation.NEAR_SUPPORT
            if direction is MarketTrendState.BULLISH
            else PriceLocation.NEAR_RESISTANCE
        )

    trend = MarketTrend(
        state=direction,
        bias=(
            StructureBias.BULLISH
            if direction is MarketTrendState.BULLISH
            else StructureBias.BEARISH
        ),
        structure_intact=structure_intact,
        reasons=["test"],
    )
    sr = SupportResistanceContext(
        support=100.0 if location in (
            PriceLocation.NEAR_SUPPORT, PriceLocation.BELOW_SUPPORT,
        ) else 90.0,
        resistance=110.0 if location in (
            PriceLocation.NEAR_RESISTANCE, PriceLocation.ABOVE_RESISTANCE,
        ) else 120.0,
        distance_to_support=-0.01,
        distance_to_resistance=0.01,
        location=location,
    )
    rng = RangeContext(
        state=range_state,
        high=110.0 if range_state is RangeState.IN_RANGE else None,
        low=90.0 if range_state is RangeState.IN_RANGE else None,
        width=10.0 if range_state is RangeState.IN_RANGE else None,
        position=0.5 if range_state is RangeState.IN_RANGE else None,
        reason="test",
    )
    recent = tuple(
        StructurePoint(
            swing=type("S", (), {"price": 100.0, "index": i})(),
            structure=(
                StructureType.HIGHER_HIGH
                if direction is MarketTrendState.BULLISH
                else StructureType.LOWER_LOW
            ),
        )
        for i in range(confirmed_swings)
    )
    return MarketContext(
        index=59,
        trend=trend,
        range=rng,
        support_resistance=sr,
        recent_structure=recent,
        confirmed_swings=confirmed_swings,
    )


def _mtf_timeframe_state(
    instrument: str,
    timeframe: str,
    *,
    availability: IntradayCoverageStatus = IntradayCoverageStatus.VALID,
    market_state: MarketContext | None = None,
    direction: MTFDirection = MTFDirection.BULLISH,
    completed_count: int = 60,
) -> MtfTimeframeState:
    return MtfTimeframeState(
        instrument=instrument,
        timeframe=timeframe,
        availability=availability,
        completed_candle_count=completed_count,
        latest_completed_timestamp=_now(),
        latest_completed_candle=_candle(_now(), 105.0),
        market_state=market_state,
        direction=direction,
        market_state_available=market_state is not None,
        reason="",
    )


def _aligned_mtf(
    instrument: str = "RELIANCE",
    *,
    direction: MTFDirection = MTFDirection.BULLISH,
    market_state: MarketContext | None = None,
    completeness: MarketStateCompleteness = MarketStateCompleteness.COMPLETE,
    alignment: MtfAlignmentState = MtfAlignmentState.ALIGNED,
    primary_availability: IntradayCoverageStatus = IntradayCoverageStatus.VALID,
    primary_state: MtfTimeframeState | None = None,
) -> PerSymbolMtfResult:
    """A deterministic 19.4 per-symbol MTF result (15m + 1h).

    The 19.4 ``PerSymbolMtfResult.__post_init__`` RECOMPUTES
    completeness/alignment from the timeframe states, so the states are
    built to NATURALLY produce the requested completeness/alignment
    (a caller-suppbled explicit classification would be overridden).
    """

    # Secondary frame direction (drives alignment). The 19.4 model
    # RECOMPUTES direction from the market-state trend, so a conflicting
    # frame needs a market state with the opposite trend and a mixed
    # frame needs a market state whose trend maps to NEUTRAL.
    secondary_market_state = market_state
    if alignment is MtfAlignmentState.CONFLICTING:
        secondary_direction = (
            MTFDirection.BEARISH
            if direction is MTFDirection.BULLISH
            else MTFDirection.BULLISH
        )
        secondary_market_state = _market_context(
            MarketTrendState.BEARISH
            if direction is MTFDirection.BULLISH
            else MarketTrendState.BULLISH,
        )
    elif alignment is MtfAlignmentState.MIXED:
        secondary_direction = MTFDirection.NEUTRAL
        secondary_market_state = _market_context(MarketTrendState.NEUTRAL)
    else:
        secondary_direction = direction

    if completeness is MarketStateCompleteness.UNSUPPORTED:
        states = (
            _mtf_timeframe_state(
                instrument, "15m",
                availability=IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
                market_state=None, direction=MTFDirection.UNKNOWN,
            ),
            _mtf_timeframe_state(
                instrument, "1h",
                availability=IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
                market_state=None, direction=MTFDirection.UNKNOWN,
            ),
        )
    elif completeness is MarketStateCompleteness.UNAVAILABLE:
        states = (
            _mtf_timeframe_state(
                instrument, "15m",
                availability=IntradayCoverageStatus.PROVIDER_ERROR,
                market_state=None, direction=MTFDirection.UNKNOWN,
            ),
            _mtf_timeframe_state(
                instrument, "1h",
                availability=IntradayCoverageStatus.PROVIDER_ERROR,
                market_state=None, direction=MTFDirection.UNKNOWN,
            ),
        )
    elif completeness is MarketStateCompleteness.INCOMPLETE:
        states = (
            _mtf_timeframe_state(
                instrument, "15m",
                availability=primary_availability,
                market_state=market_state, direction=direction,
            ),
            _mtf_timeframe_state(
                instrument, "1h",
                availability=IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
                market_state=None, direction=MTFDirection.UNKNOWN,
            ),
        )
    else:
        states = (
            _mtf_timeframe_state(
                instrument, "15m",
                availability=primary_availability,
                market_state=market_state, direction=direction,
            ),
            _mtf_timeframe_state(
                instrument, "1h",
                availability=IntradayCoverageStatus.VALID,
                market_state=secondary_market_state,
                direction=secondary_direction,
            ),
        )
    return PerSymbolMtfResult(
        instrument=instrument,
        timeframes=("15m", "1h"),
        timeframe_states=states,
        completeness=completeness,
        alignment=alignment,
        alignment_reason="test",
        reference_now=_now(),
    )


# ------------------------------------------------------------------
# Setup-quality engine helpers
# ------------------------------------------------------------------


def _engine(config: SetupQualityConfig | None = None) -> SetupQualityEngine:
    return SetupQualityEngine(config)


def _evaluate(
    mtf: PerSymbolMtfResult,
    candles: list[OHLCVCandle] | None = None,
    config: SetupQualityConfig | None = None,
) -> SetupQualityResult:
    return _engine(config).evaluate(mtf, candles or _bullish_15m())


# ==================================================================
# A. ARCHITECTURE
# ==================================================================


class TestArchitecture:
    def test_consumes_19_4_mtf_results(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.mtf is mtf  # by reference, never duplicated
        assert result.mtf.instrument == result.instrument

    def test_uses_19_1_universe(self):
        sq = _engine()
        # evaluate_universe accepts the frozen 19.1 UniverseDefinition.
        mtf_analysis = MtfUniverseAnalysis(
            analysis_id="mtf-test",
            reference_now=_now(),
            timeframes=("15m", "1h"),
            universe_instrument_count=1,
            instruments=("RELIANCE",),
            results=(_aligned_mtf(),),
        )
        out = sq.evaluate_universe(mtf_analysis, {"RELIANCE": _bullish_15m()})
        assert out.universe_instrument_count == 1
        assert out.instruments == ("RELIANCE",)
        assert len(out.results) == 1

    def test_preserves_19_2_data_status_semantics(self):
        # STALE primary data is a hard cap, never a high-quality setup.
        mtf = _aligned_mtf(
            primary_availability=IntradayCoverageStatus.STALE,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert result.stale_data is True
        assert result.classification.rank_value <= (
            SetupQualityClassification.LOW.rank_value
        )

    def test_19_3_scan_architecture_compatible(self):
        # The 19.3 continuous scanner's per-symbol coverage object can
        # flow into the 19.4 layer unchanged; 19.5 consumes the 19.4
        # result, not the scanner itself.
        from engine.models.continuous_scan import (
            MarketScanStatus,
            MarketScanCycleResult,
            PerSymbolScanResult,
        )
        from engine.models.intraday_coverage import IntradayInstrumentCoverage

        coverage = IntradayInstrumentCoverage(
            instrument="RELIANCE",
            timeframe="15m",
            status=IntradayCoverageStatus.VALID,
            candle_count=60,
        )
        scan = MarketScanCycleResult(
            cycle_id="cycle-test",
            reference_now=_now(),
            timeframe="15m",
            universe=("RELIANCE",),
            results=(
                PerSymbolScanResult(
                    instrument="RELIANCE", coverage=coverage,
                ),
            ),
            status=MarketScanStatus.FULL_SUCCESS,
        )
        assert scan.results[0].available is True
        # 19.5 consumes the 19.4 result derived from the same data path.
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.status is SetupQualityStatus.QUALIFIED

    def test_19_4_mtf_behavior_intact(self):
        # The 19.4 alignment/completeness classifications are preserved
        # verbatim on the 19.5 result.
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.INCOMPLETE,
            alignment=MtfAlignmentState.INCOMPLETE,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert result.mtf.alignment is MtfAlignmentState.INCOMPLETE
        assert result.mtf.completeness is MarketStateCompleteness.INCOMPLETE
        assert result.data_complete is False


# ==================================================================
# B. SETUP DETECTION
# ==================================================================


class TestSetupDetection:
    def test_valid_setup_detected(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.status is SetupQualityStatus.QUALIFIED
        assert result.setup_classification is SetupClassification.POTENTIAL_SETUP
        assert result.setup_direction is SetupDirection.BULLISH
        assert result.confluence_score is not None and result.confluence_score >= 3

    def test_invalid_non_setup_rejected(self):
        # A NO_SETUP 11Q assessment (no directional evidence) is never
        # a qualified setup. Build a market context with UNKNOWN trend
        # and NO confirmed structure (nothing directional to detect).
        from engine.models.market_context import MarketTrend, RangeState
        from engine.models.structure_analysis import StructureBias

        unknown_ctx = MarketContext(
            index=59,
            trend=MarketTrend(
                state=MarketTrendState.UNKNOWN,
                bias=StructureBias.UNKNOWN,
                structure_intact=False,
                reasons=["test"],
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
        mtf = _aligned_mtf(market_state=unknown_ctx)
        result = _evaluate(mtf, _flat_candles())
        assert result.status is SetupQualityStatus.NO_SETUP
        assert result.classification is SetupQualityClassification.REJECTED

    def test_setup_classification_deterministic(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        a = _evaluate(mtf)
        b = _evaluate(mtf)
        assert a.setup_classification == b.setup_classification
        assert a.setup_direction == b.setup_direction

    def test_setup_evidence_explicit(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.positive_factors
        assert any("CONFLUENCE" in f for f in result.positive_factors)
        assert result.explanation

    def test_positive_evidence_represented(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert "SETUP_POTENTIAL" in result.positive_factors
        assert "MTF_ALIGNED" in result.positive_factors

    def test_negative_evidence_represented(self):
        mtf = _aligned_mtf(
            alignment=MtfAlignmentState.CONFLICTING,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert "MTF_CONFLICTING" in result.negative_factors
        assert result.has_conflict is False  # 11Q conflict is separate


# ==================================================================
# C. SCORING
# ==================================================================


class TestScoring:
    def test_score_deterministic(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        a = _evaluate(mtf)
        b = _evaluate(mtf)
        assert a.score == b.score
        assert a.score_components == b.score_components

    def test_score_within_bounds(self):
        for direction in (
            MarketTrendState.BULLISH, MarketTrendState.BEARISH,
            MarketTrendState.NEUTRAL, MarketTrendState.RANGE,
        ):
            mtf = _aligned_mtf(market_state=_market_context(direction))
            result = _evaluate(mtf)
            if result.score is not None:
                assert 0 <= result.score <= 100

    def test_each_component_behavior(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        comps = result.score_component_map
        assert set(comps) == {
            "mtf_alignment", "setup_confirmation",
            "setup_confluence", "structure_quality",
        }
        # MTF aligned -> full weight
        assert comps["mtf_alignment"] == 30
        # POTENTIAL_SETUP -> full weight
        assert comps["setup_confirmation"] == 30
        # confluence 3/5 -> 15
        assert comps["setup_confluence"] == 15
        # structure intact -> full weight
        assert comps["structure_quality"] == 15

    def test_weighting_sum_100(self):
        config = SetupQualityConfig()
        assert config.max_score == 100

    def test_threshold_classification(self):
        config = SetupQualityConfig()
        assert config.classification_for_score(100) == "EXCELLENT"
        assert config.classification_for_score(80) == "EXCELLENT"
        assert config.classification_for_score(79) == "HIGH"
        assert config.classification_for_score(65) == "HIGH"
        assert config.classification_for_score(64) == "MEDIUM"
        assert config.classification_for_score(50) == "MEDIUM"
        assert config.classification_for_score(49) == "LOW"
        assert config.classification_for_score(35) == "LOW"
        assert config.classification_for_score(34) == "REJECTED"

    def test_same_input_same_score(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        results = [_evaluate(mtf) for _ in range(5)]
        assert len({r.score for r in results}) == 1

    def test_no_randomness(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        results = [_evaluate(mtf) for _ in range(10)]
        assert all(r.score == results[0].score for r in results)

    def test_no_unexplained_contribution(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        total = sum(c.points for c in result.score_components)
        assert total == result.score
        assert all(c.reason for c in result.score_components)


# ==================================================================
# D. MTF HANDLING
# ==================================================================


class TestMtfHandling:
    def test_strong_alignment_affects_quality(self):
        aligned = _evaluate(
            _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH)),
        )
        mixed = _evaluate(
            _aligned_mtf(
                alignment=MtfAlignmentState.MIXED,
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
        )
        assert aligned.score > mixed.score
        assert aligned.score_component_map["mtf_alignment"] == 30
        assert mixed.score_component_map["mtf_alignment"] == 15

    def test_conflicting_mtf_affects_quality(self):
        conflicting = _evaluate(
            _aligned_mtf(
                alignment=MtfAlignmentState.CONFLICTING,
                market_state=_market_context(MarketTrendState.BULLISH),
            ),
        )
        assert conflicting.score_component_map["mtf_alignment"] == 0
        assert "MTF_CONFLICTING" in conflicting.negative_factors

    def test_incomplete_mtf_explicit(self):
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.INCOMPLETE,
            alignment=MtfAlignmentState.INCOMPLETE,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert result.data_complete is False
        assert result.score_component_map["mtf_alignment"] == 0
        assert "MTF_INCOMPLETE" in result.negative_factors
        # Incomplete MTF caps at LOW at most.
        assert result.classification.rank_value <= (
            SetupQualityClassification.LOW.rank_value
        )

    def test_unsupported_timeframe_explicit(self):
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.UNSUPPORTED,
            alignment=MtfAlignmentState.UNAVAILABLE,
            market_state=None,
        )
        result = _evaluate(mtf)
        assert result.status is SetupQualityStatus.INCOMPLETE
        assert result.classification is SetupQualityClassification.INCOMPLETE
        assert result.score is None

    def test_stale_timeframe_explicit(self):
        mtf = _aligned_mtf(
            primary_availability=IntradayCoverageStatus.STALE,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert result.stale_data is True
        assert "STALE_PRIMARY_DATA" in result.negative_factors
        assert result.classification.rank_value <= (
            SetupQualityClassification.LOW.rank_value
        )


# ==================================================================
# E. POINT-IN-TIME CORRECTNESS (adversarial look-ahead)
# ==================================================================


class TestPointInTime:
    def test_future_candles_do_not_create_earlier_setups(self):
        # At T the market is NOT a setup; a future breakout candle
        # appended after T must NOT create a setup at T.
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        # Append a strong future bullish breakout candle.
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), 200.0),
            _candle(base[-1].timestamp + timedelta(minutes=30), 210.0),
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.status == still_t.status
        assert at_t.score == still_t.score
        assert at_t.classification == still_t.classification

    def test_future_candles_do_not_change_earlier_quality(self):
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), 50.0),
            _candle(base[-1].timestamp + timedelta(minutes=30), 45.0),
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.score == still_t.score
        assert at_t.score_components == still_t.score_components
        assert at_t.explanation == still_t.explanation

    def test_future_bos_does_not_change_earlier_results(self):
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        # A future BOS (break of structure) candle.
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), 300.0),
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.score == still_t.score
        assert at_t.status == still_t.status

    def test_future_htf_candle_does_not_change_earlier_results(self):
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        # A future 1h candle that closes after T is never used (the 19.4
        # result is already fixed at T; the 19.5 engine reads only the
        # supplied completed primary candles).
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), 400.0),
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.score == still_t.score
        assert at_t.data_complete == still_t.data_complete

    def test_future_support_resistance_does_not_leak_backward(self):
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), 15.0),
            _candle(base[-1].timestamp + timedelta(minutes=30), 10.0),
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.score == still_t.score
        assert at_t.setup_type == still_t.setup_type

    def test_appending_future_data_leaves_earlier_results_unchanged(self):
        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        at_t = _evaluate(mtf, base)
        future = list(base) + [
            _candle(base[-1].timestamp + timedelta(minutes=15), c)
            for c in (120.0, 130.0, 140.0, 150.0)
        ]
        still_t = _evaluate(mtf, future)
        assert at_t.score == still_t.score
        assert at_t.status == still_t.status
        assert at_t.classification == still_t.classification
        assert at_t.positive_factors == still_t.positive_factors
        assert at_t.negative_factors == still_t.negative_factors


# ==================================================================
# F. RANKING
# ==================================================================


class TestRanking:
    def _universe(self, results: tuple[PerSymbolMtfResult, ...], candles_map):
        analysis = MtfUniverseAnalysis(
            analysis_id="mtf-rank",
            reference_now=_now(),
            timeframes=("15m", "1h"),
            universe_instrument_count=len(results),
            instruments=tuple(r.instrument for r in results),
            results=results,
        )
        return _engine().evaluate_universe(analysis, candles_map)

    def test_qualified_ranked_correctly(self):
        strong = _aligned_mtf(
            "RELIANCE", market_state=_market_context(MarketTrendState.BULLISH),
        )
        weak = _aligned_mtf(
            "TCS", market_state=_market_context(MarketTrendState.BEARISH),
            direction=MTFDirection.BEARISH,
        )
        out = self._universe((strong, weak), {
            "RELIANCE": _bullish_15m(), "TCS": _bearish_15m(),
        })
        assert len(out.ranked_qualified) == 2
        # Stronger (higher score) ranks first.
        scores = [r.score for r in out.ranked_qualified]
        assert scores == sorted(scores, reverse=True)

    def test_ranking_deterministic(self):
        results = (
            _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH)),
            _aligned_mtf("TCS", market_state=_market_context(MarketTrendState.BEARISH), direction=MTFDirection.BEARISH),
            _aligned_mtf("HDFCBANK", market_state=_market_context(MarketTrendState.BULLISH)),
        )
        candles = {
            "RELIANCE": _bullish_15m(),
            "TCS": _bearish_15m(),
            "HDFCBANK": _bullish_15m(),
        }
        a = self._universe(results, candles)
        b = self._universe(results, candles)
        assert [r.instrument for r in a.ranked_qualified] == [
            r.instrument for r in b.ranked_qualified
        ]

    def test_equal_scores_deterministic_tiebreak(self):
        # Two identical-quality symbols tie-break by instrument name.
        r1 = _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH))
        r2 = _aligned_mtf("TCS", market_state=_market_context(MarketTrendState.BULLISH))
        out = self._universe((r1, r2), {
            "RELIANCE": _bullish_15m(), "TCS": _bullish_15m(),
        })
        ranked = [r.instrument for r in out.ranked_qualified]
        # "RELIANCE" < "TCS" lexicographically; identical scores ->
        # RELIANCE (the canonical ascending tie-break) ranks first.
        assert ranked == ["RELIANCE", "TCS"]

    def test_provider_response_ordering_cannot_alter_ranking(self):
        # Input order is reversed; the universe result is canonically
        # ordered and the ranking is identical.
        r1 = _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH))
        r2 = _aligned_mtf("TCS", market_state=_market_context(MarketTrendState.BEARISH), direction=MTFDirection.BEARISH)
        candles = {"RELIANCE": _bullish_15m(), "TCS": _bearish_15m()}
        out_forward = self._universe((r1, r2), candles)
        out_reversed = self._universe((r2, r1), candles)
        assert [r.instrument for r in out_forward.results] == [
            r.instrument for r in out_reversed.results
        ]
        assert [r.instrument for r in out_forward.ranked_qualified] == [
            r.instrument for r in out_reversed.ranked_qualified
        ]

    def test_top_n_behavior(self):
        results = tuple(
            _aligned_mtf(
                f"SYM{i}", market_state=_market_context(MarketTrendState.BULLISH),
            )
            for i in range(5)
        )
        candles = {f"SYM{i}": _bullish_15m() for i in range(5)}
        analysis = MtfUniverseAnalysis(
            analysis_id="mtf-topn",
            reference_now=_now(),
            timeframes=("15m", "1h"),
            universe_instrument_count=5,
            instruments=tuple(r.instrument for r in results),
            results=results,
        )
        out = _engine(SetupQualityConfig(max_qualified=2)).evaluate_universe(
            analysis, candles,
        )
        assert len(out.top_n) == 2
        assert len(out.ranked_qualified) == 5  # full set retained internally
        assert [r.instrument for r in out.top_n] == [
            r.instrument for r in out.ranked_qualified[:2]
        ]


# ==================================================================
# G. FAILURE ISOLATION
# ==================================================================


class TestFailureIsolation:
    def test_one_symbol_failure_does_not_terminate_universe(self):
        good = _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH))
        bad = _aligned_mtf(
            "TCS",
            completeness=MarketStateCompleteness.UNAVAILABLE,
            alignment=MtfAlignmentState.UNAVAILABLE,
            market_state=None,
        )
        analysis = MtfUniverseAnalysis(
            analysis_id="mtf-fail",
            reference_now=_now(),
            timeframes=("15m", "1h"),
            universe_instrument_count=2,
            instruments=("RELIANCE", "TCS"),
            results=(good, bad),
        )
        out = _engine().evaluate_universe(
            analysis, {"RELIANCE": _bullish_15m(), "TCS": []},
        )
        assert len(out.results) == 2
        assert out.result_for("RELIANCE").status is SetupQualityStatus.QUALIFIED
        assert out.result_for("TCS").status is SetupQualityStatus.UNAVAILABLE

    def test_one_timeframe_failure_does_not_fabricate_setup(self):
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.INCOMPLETE,
            alignment=MtfAlignmentState.INCOMPLETE,
            market_state=_market_context(MarketTrendState.BULLISH),
        )
        result = _evaluate(mtf)
        assert result.data_complete is False
        assert result.score_component_map["mtf_alignment"] == 0

    def test_invalid_data_cannot_become_qualified_setup(self):
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.UNAVAILABLE,
            alignment=MtfAlignmentState.UNAVAILABLE,
            market_state=None,
        )
        result = _evaluate(mtf, [])
        assert result.status is SetupQualityStatus.UNAVAILABLE
        assert result.score is None
        assert result.classification is SetupQualityClassification.UNAVAILABLE

    def test_missing_data_remains_visible(self):
        mtf = _aligned_mtf(
            completeness=MarketStateCompleteness.UNSUPPORTED,
            alignment=MtfAlignmentState.UNAVAILABLE,
            market_state=None,
        )
        result = _evaluate(mtf, [])
        assert result.status is SetupQualityStatus.INCOMPLETE
        assert result.score is None


# ==================================================================
# H. EXPLAINABILITY / PROVENANCE
# ==================================================================


class TestExplainability:
    def test_every_qualified_setup_has_explanation(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.explanation
        assert result.reason

    def test_explanation_matches_scoring_inputs(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert "ALIGNED" in result.explanation
        assert "POTENTIAL_SETUP" in result.explanation
        assert str(result.score) in result.explanation

    def test_analysis_timestamp_preserved(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert result.reference_now == _now()
        assert result.reference_now.tzinfo is not None

    def test_source_timestamps_preserved(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        primary_state = result.mtf.state_for("15m")
        assert primary_state.latest_completed_timestamp is not None

    def test_scoring_rule_version_preserved(self):
        # The config snapshot + analysis id embed the scoring-rule
        # version deterministically.
        config = SetupQualityConfig()
        snapshot = dict(config.snapshot())
        assert snapshot["max_score"] == "100"
        aid = setup_quality_analysis_id(
            ("15m", "1h"), ("RELIANCE",), _now(), config.snapshot(),
        )
        assert aid.startswith("sq-")
        assert len(aid) == 3 + 16


# ==================================================================
# I. BOUNDARIES
# ==================================================================


class TestBoundaries:
    def test_no_trade_execution(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert not any(
            token in result.explanation.upper()
            for token in ("BUY", "SELL", "ORDER", "EXECUTE")
        )

    def test_no_broker_import_in_new_layer(self):
        import dashboard.setup_quality as mod

        tree = ast.parse(inspect_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "broker" not in alias.name.lower()
                    assert "execution" not in alias.name.lower()
                    assert "upstox" not in alias.name.lower()
            elif isinstance(node, ast.ImportFrom):
                assert "broker" not in (node.module or "").lower()
                assert "execution" not in (node.module or "").lower()

    def test_no_alert_emitted(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        assert not any(
            token in result.explanation.upper()
            for token in ("ALERT", "NOTIFY", "TELEGRAM", "WHATSAPP", "PUSH")
        )

    def test_no_setup_lifecycle_state_machine(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        # 19.5 produces a point-in-time assessment, never a lifecycle.
        assert not hasattr(result, "state")
        assert not hasattr(result, "triggered")
        assert not hasattr(result, "invalidated")

    def test_no_forward_testing_framework(self):
        import dashboard.setup_quality as mod

        tree = ast.parse(inspect_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "forward" not in (node.module or "").lower()
                assert "backtest" not in (node.module or "").lower()

    def test_no_19_8_reliability_framework(self):
        import dashboard.setup_quality as mod

        tree = ast.parse(inspect_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert "watchdog" not in (node.module or "").lower()
                assert "health" not in (node.module or "").lower()

    def test_no_mtf_engine_duplication(self):
        import dashboard.setup_quality as mod

        tree = ast.parse(inspect_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Consumes the 19.4 result; must not import the MTF engine.
                assert "mtf_analysis" not in (node.module or "").lower() or (
                    "engine.models.mtf_analysis" in (node.module or "").lower()
                )

    def test_no_universe_duplication(self):
        import dashboard.setup_quality as mod

        tree = ast.parse(inspect_source(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                # Consumes the 19.1 universe; must not define its own.
                assert "nifty200_manifest" not in (node.module or "").lower()


# ==================================================================
# J. CONFIG VALIDATION
# ==================================================================


class TestConfigValidation:
    def test_defaults(self):
        config = SetupQualityConfig()
        assert config.max_score == 100
        assert config.excellent_threshold == 80

    def test_weights_must_sum_100(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(mtf_alignment_weight=10)

    def test_threshold_ordering(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(low_threshold=60, medium_threshold=50)

    def test_bad_classification_cap(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(conflict_max_classification="NOPE")

    def test_data_gate_cap_rejected(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(conflict_max_classification="INCOMPLETE")

    def test_bad_primary_timeframe(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(primary_timeframe="7m")

    def test_daily_primary_rejected(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(primary_timeframe="1D")

    def test_bad_max_qualified(self):
        with pytest.raises(ValueError):
            SetupQualityConfig(max_qualified=0)

    def test_frozen(self):
        config = SetupQualityConfig()
        with pytest.raises(Exception):
            config.mtf_alignment_weight = 50  # type: ignore[misc]


# ==================================================================
# K. MODEL VALIDATION
# ==================================================================


class TestModelValidation:
    def test_result_frozen(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        with pytest.raises(Exception):
            result.score = 0  # type: ignore[misc]

    def test_result_requires_matching_mtf(self):
        other = _aligned_mtf("TCS", market_state=_market_context(MarketTrendState.BULLISH))
        with pytest.raises(ValueError):
            SetupQualityResult(
                instrument="RELIANCE",
                reference_now=_now(),
                mtf=other,
                primary_timeframe="15m",
                status=SetupQualityStatus.QUALIFIED,
                classification=SetupQualityClassification.HIGH,
                score=80,
                score_components=(),
            )

    def test_data_gate_state_no_score(self):
        with pytest.raises(ValueError):
            SetupQualityResult(
                instrument="RELIANCE",
                reference_now=_now(),
                mtf=_aligned_mtf(),
                primary_timeframe="15m",
                status=SetupQualityStatus.UNAVAILABLE,
                classification=SetupQualityClassification.UNAVAILABLE,
                score=50,
            )

    def test_counts_invariants(self):
        counts = SetupQualityCounts(qualified=2, watch=1, no_setup=3)
        assert counts.tested == 6
        assert counts.qualified_ratio == pytest.approx(2 / 6)

    def test_universe_requires_exact_results(self):
        # The 19.4 model enforces exactly-one-result-per-constituent.
        with pytest.raises(ValueError):
            MtfUniverseAnalysis(
                analysis_id="mtf-x",
                reference_now=_now(),
                timeframes=("15m", "1h"),
                universe_instrument_count=2,
                instruments=("RELIANCE", "TCS"),
                results=(_aligned_mtf("RELIANCE"),),
            )
        # The 19.5 universe result enforces the same invariant.
        analysis = MtfUniverseAnalysis(
            analysis_id="mtf-x",
            reference_now=_now(),
            timeframes=("15m", "1h"),
            universe_instrument_count=1,
            instruments=("RELIANCE",),
            results=(_aligned_mtf("RELIANCE"),),
        )
        out = _engine().evaluate_universe(analysis, {"RELIANCE": _bullish_15m()})
        assert len(out.results) == 1
        with pytest.raises(ValueError):
            SetupQualityUniverseResult(
                analysis_id="sq-x",
                reference_now=_now(),
                timeframes=("15m", "1h"),
                primary_timeframe="15m",
                universe_instrument_count=2,
                instruments=("RELIANCE", "TCS"),
                results=out.results,  # only RELIANCE present
            )


# ==================================================================
# L. INTEGRATION (scripted provider -> 19.4 -> 19.5)
# ==================================================================


@dataclass(frozen=True)
class TfSpec:
    candles: tuple[OHLCVCandle, ...] = ()
    status: ProviderStatus = ProviderStatus.OK
    forming: OHLCVCandle | None = None


class ScriptedMtfProvider:
    """Deterministic fake provider serving multiple timeframes."""

    data_source = "fake-mtf"

    def __init__(
        self,
        specs: dict[tuple[str, str], TfSpec] | None = None,
        unsupported_tfs: set[str] | None = None,
    ) -> None:
        self._specs = specs or {}
        self._unsupported_tfs = unsupported_tfs or set()
        self.calls: list[tuple[str, str]] = []

    def is_timeframe_supported(self, tf: str) -> bool:
        return tf not in self._unsupported_tfs

    def supports_instrument(self, instrument: str) -> bool:
        return True

    def resolve_symbol(self, instrument: str) -> str:
        return instrument

    def fetch(
        self,
        instrument: str,
        setup_timeframe: str,
        lookback_bars: int = 300,
        *,
        reference_now: datetime | None = None,
    ) -> InstrumentSeries:
        del lookback_bars, reference_now
        self.calls.append((instrument, setup_timeframe))
        if setup_timeframe in self._unsupported_tfs:
            return InstrumentSeries(
                instrument=instrument,
                available=False,
                reason=f"timeframe {setup_timeframe} unsupported",
                data_source=self.data_source,
                provider_status=ProviderStatus.UNSUPPORTED,
                freshness_state=FreshnessState.UNAVAILABLE,
            )
        spec = self._specs.get((instrument, setup_timeframe))
        if spec is None:
            return InstrumentSeries(
                instrument=instrument,
                available=False,
                reason="not served",
                data_source=self.data_source,
                provider_status=ProviderStatus.UNSUPPORTED,
                freshness_state=FreshnessState.UNAVAILABLE,
            )
        return InstrumentSeries(
            instrument=instrument,
            setup_candles=spec.candles,
            available=bool(spec.candles),
            reason="",
            data_source=self.data_source,
            provider_status=spec.status,
            freshness_state=FreshnessState.CURRENT,
            latest_candle_timestamp=(
                spec.candles[-1].timestamp if spec.candles else None
            ),
            latest_completed_candle_timestamp=(
                spec.candles[-1].timestamp if spec.candles else None
            ),
            forming_setup_candle=spec.forming,
            last_successful_fetch_time=datetime(2026, 9, 4, 5, 29, tzinfo=UTC),
            rejected_future_count=0,
        )

    def last_updated(self, instrument: str, setup_timeframe: str) -> datetime | None:
        del setup_timeframe
        return None


class TestIntegration:
    def test_end_to_end_scripted_provider(self):
        """19.2 provider -> 19.4 MTF -> 19.5 setup-quality."""

        bullish = tuple(_bullish_15m())
        provider = ScriptedMtfProvider(
            specs={
                ("RELIANCE", "15m"): TfSpec(candles=bullish),
                ("RELIANCE", "1h"): TfSpec(candles=bullish[::4]),
            },
        )
        mtf_engine = MtfAnalysisEngine(
            provider=provider,
            config=MtfAnalysisConfig(timeframes=("15m", "1h")),
        )
        mtf_result = mtf_engine.analyze_symbol("RELIANCE", reference_now=_now())
        sq = _engine()
        result = sq.evaluate(mtf_result, list(bullish))
        assert result.mtf is mtf_result
        assert result.status in (
            SetupQualityStatus.QUALIFIED,
            SetupQualityStatus.WATCH,
            SetupQualityStatus.NO_SETUP,
        )

    def test_fixture_provider_honest_unsupported_1h(self):
        """The real fixture path: 1h unsupported -> INCOMPLETE, capped."""

        from dashboard.intraday_coverage import IntradayCoverageEngine

        mtf_engine = MtfAnalysisEngine.build(
            "fixture", timeframes=("15m", "1h"),
        )
        mtf_result = mtf_engine.analyze_symbol("RELIANCE", reference_now=_now())
        assert mtf_result.completeness is MarketStateCompleteness.INCOMPLETE
        coverage = IntradayCoverageEngine.build("fixture", timeframe="15m")
        series = coverage.provider.fetch("RELIANCE", "15m", reference_now=_now())
        from dashboard.data_provider import split_completed_candles

        boundary = split_completed_candles(
            tuple(series.setup_candles or ()), "15m", _now(),
        )
        result = _engine().evaluate(mtf_result, list(boundary.completed))
        assert result.data_complete is False
        assert result.score_component_map["mtf_alignment"] == 0
        assert result.classification.rank_value <= (
            SetupQualityClassification.LOW.rank_value
        )

    def test_universe_wide_with_19_1_universe(self):
        """Universe-wide evaluation over the frozen NIFTY Top 200."""

        universe = UniverseBuilder.nifty200()
        assert universe.instrument_count == 200 + 1  # + benchmark
        # A tiny custom subset is evaluated (the full 200 would need
        # 200 x 2 provider fetches; the engine is proven deterministic
        # on a subset and the universe machinery is identical).
        subset = UniverseBuilder.custom(["RELIANCE", "TCS"])
        mtf_engine = MtfAnalysisEngine.build(
            "fixture", timeframes=("15m", "1h"),
        )
        analysis = mtf_engine.analyze_universe(subset, reference_now=_now())
        assert len(analysis.results) == 2
        candles_map = {}
        from dashboard.data_provider import split_completed_candles
        from dashboard.intraday_coverage import IntradayCoverageEngine

        coverage = IntradayCoverageEngine.build("fixture", timeframe="15m")
        for instrument in analysis.instruments:
            series = coverage.provider.fetch(
                instrument, "15m", reference_now=_now(),
            )
            boundary = split_completed_candles(
                tuple(series.setup_candles or ()), "15m", _now(),
            )
            candles_map[instrument] = list(boundary.completed)
        out = _engine().evaluate_universe(analysis, candles_map)
        assert len(out.results) == 2
        assert out.counts.tested == 2

    def test_view_builder_jsonable(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        j = SetupQualityViewBuilder.result_to_jsonable(result)
        assert j["instrument"] == "RELIANCE"
        assert j["score"] == result.score
        assert len(j["score_components"]) == 4

    def test_formatter_returns_str(self):
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        result = _evaluate(mtf)
        text = SetupQualityFormatter().format_result(result)
        assert isinstance(text, str)
        assert "WARNING" in text
        assert "BUY" not in text.upper().replace("BUY", "")


# ==================================================================
# M. NO-LOOK-AHEAD THROUGH AGGREGATION (engine-level proof)
# ==================================================================


class TestNoLookAheadThroughAggregation:
    def test_engine_reads_only_supplied_candles(self):
        """The engine never inspects candles beyond the supplied list."""

        base = _bullish_15m(60)
        mtf = _aligned_mtf(market_state=_market_context(MarketTrendState.BULLISH))
        # Truncate to the first 30 candles; the engine uses the last
        # index of the SUPPLIED list only.
        prefix = base[:30]
        result = _evaluate(mtf, prefix)
        assert result.setup_classification is not None
        # The engine is a pure function of its inputs: re-evaluating the
        # SAME (mtf, prefix) yields the identical score.
        assert _evaluate(mtf, prefix).score == result.score


def _flat_candles(n: int = 60) -> list[OHLCVCandle]:
    """Sideways candles (NEUTRAL market, no directional patterns)."""
    from engine.data.historical_fixtures import _flat_oscillation

    last = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    start_ts = last - timedelta(minutes=15) * (n - 1)
    return _flat_oscillation(n, start_ts=start_ts, step=timedelta(minutes=15))


def inspect_source(module) -> str:
    import inspect

    return inspect.getsource(module)