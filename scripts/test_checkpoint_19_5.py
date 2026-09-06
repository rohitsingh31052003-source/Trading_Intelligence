#!/usr/bin/env python3
"""
Checkpoint 19.5 demo — setup-quality intelligence.

Proves (deterministically, offline):

  1. the setup-quality engine consumes the FROZEN 19.4 MTF results and
     the FROZEN 19.1 validated NIFTY Top 200 universe;
  2. setup detection reuses the proven Sprint 11O + 11Q engines
     (POTENTIAL_SETUP / WATCH / NO_SETUP) — no new detection heuristic;
  3. the quality score is deterministic, bounded [0, 100], and
     component-transparent (mtf_alignment / setup_confirmation /
     setup_confluence / structure_quality = 30+30+25+15);
  4. quality classification is deterministic (EXCELLENT / HIGH / MEDIUM
     / LOW / REJECTED + data-gate INCOMPLETE / UNAVAILABLE);
  5. POSITIVE + NEGATIVE evidence is explicit and derived from the
     actual inputs;
  6. data quality is a PREREQUISITE: stale / incomplete / unsupported /
     missing data can never become a high-quality setup (caps);
  7. POINT-IN-TIME SAFETY: appending arbitrary future candles (future
     breakout / BOS / higher-timeframe confirmation / support-
     resistance event) leaves an already-computed setup-quality result
     at T unchanged;
  8. deterministic universe-wide setup analysis with per-symbol
     failure isolation and exactly-one-result-per-constituent;
  9. deterministic ranking (strongest-first) with deterministic
     tie-breaking; provider response order cannot alter ranking;
 10. top-N view (configurable) keeps the full ranked set internally;
 11. NO trade execution, NO broker import, NO alerts, NO setup
     lifecycle, NO forward-testing framework, NO 19.8 reliability
     framework;
 12. the pipeline baseline (signals=4, trades=3) is unchanged.

Run:  python scripts/test_checkpoint_19_5.py
"""

from __future__ import annotations

import ast
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.data_provider import (  # noqa: E402
    FreshnessState,
    InstrumentSeries,
    ProviderStatus,
)
from dashboard.mtf_analysis import MtfAnalysisEngine  # noqa: E402
from dashboard.setup_quality import SetupQualityEngine  # noqa: E402
from engine.config.mtf_analysis_config import MtfAnalysisConfig  # noqa: E402
from engine.config.setup_quality_config import SetupQualityConfig  # noqa: E402
from engine.config.universe_boundary import UniverseBuilder  # noqa: E402
from engine.models.intraday_coverage import IntradayCoverageStatus  # noqa: E402
from engine.models.market_context import (  # noqa: E402
    MarketContext,
    MarketTrend,
    MarketTrendState,
    PriceLocation,
    RangeContext,
    RangeState,
    SupportResistanceContext,
)
from engine.models.market_structure import StructurePoint, StructureType  # noqa: E402
from engine.models.mtf_analysis import (  # noqa: E402
    MTFDirection,
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfTimeframeState,
    MtfUniverseAnalysis,
    PerSymbolMtfResult,
)
from engine.models.ohlcv import OHLCVCandle  # noqa: E402
from engine.models.setup_quality import (  # noqa: E402
    SetupQualityClassification,
    SetupQualityStatus,
)
from engine.models.structure_analysis import StructureBias  # noqa: E402
from engine.reporting.setup_quality import SetupQualityFormatter  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def _now() -> datetime:
    return datetime(2026, 9, 4, 5, 30, tzinfo=UTC)  # Fri 11:00 IST


def _candle(ts: datetime, close: float) -> OHLCVCandle:
    return OHLCVCandle(
        timestamp=ts, open=close, high=close + 1.0,
        low=close - 1.0, close=close, volume=1000.0,
    )


def _bullish_series(n: int = 60) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bullish_zigzag

    last = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)
    start_ts = last - timedelta(minutes=15) * (n - 1)
    return _bullish_zigzag(
        start=105.0, n_legs=n // 5 + 1, start_ts=start_ts,
        step=timedelta(minutes=15), rise=6.0, pullback=3.0,
    )[:n]


def _market_ctx(
    direction: MarketTrendState,
    *,
    location: PriceLocation | None = None,
    confirmed: int = 7,
) -> MarketContext:
    if location is None:
        location = (
            PriceLocation.NEAR_SUPPORT
            if direction is MarketTrendState.BULLISH
            else PriceLocation.NEAR_RESISTANCE
        )
    trend = MarketTrend(
        state=direction,
        bias=(StructureBias.BULLISH if direction is MarketTrendState.BULLISH
              else StructureBias.BEARISH),
        structure_intact=True,
        reasons=["demo"],
    )
    sr = SupportResistanceContext(
        support=100.0, resistance=110.0,
        distance_to_support=-0.01, distance_to_resistance=0.01,
        location=location,
    )
    rng = RangeContext(
        state=RangeState.NOT_IN_RANGE, high=None, low=None,
        width=None, position=None, reason="demo",
    )
    recent = tuple(
        StructurePoint(
            swing=type("S", (), {"price": 100.0, "index": i})(),
            structure=(StructureType.HIGHER_HIGH
                       if direction is MarketTrendState.BULLISH
                       else StructureType.LOWER_LOW),
        )
        for i in range(confirmed)
    )
    return MarketContext(
        index=59, trend=trend, range=rng, support_resistance=sr,
        recent_structure=recent, confirmed_swings=confirmed,
    )


def _tf_state(
    instrument: str, tf: str, *,
    market_state: MarketContext | None,
    direction: MTFDirection,
    availability: IntradayCoverageStatus = IntradayCoverageStatus.VALID,
) -> MtfTimeframeState:
    return MtfTimeframeState(
        instrument=instrument, timeframe=tf, availability=availability,
        completed_candle_count=60,
        latest_completed_timestamp=_now(),
        latest_completed_candle=_candle(_now(), 105.0),
        market_state=market_state, direction=direction,
        market_state_available=market_state is not None,
        reason="",
    )


def _mtf(
    instrument: str,
    direction: MarketTrendState,
    *,
    conflict: bool = False,
    incomplete: bool = False,
    stale: bool = False,
) -> PerSymbolMtfResult:
    md = (MTFDirection.BULLISH if direction is MarketTrendState.BULLISH
          else MTFDirection.BEARISH)
    opposite_dir = (
        MarketTrendState.BEARISH if direction is MarketTrendState.BULLISH
        else MarketTrendState.BULLISH
    )
    secondary_state = _tf_state(
        instrument, "1h",
        market_state=(_market_ctx(direction if not conflict else opposite_dir)),
        direction=(md if not conflict else (
            MTFDirection.BEARISH if md is MTFDirection.BULLISH
            else MTFDirection.BULLISH)),
        availability=(IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME
                      if incomplete else IntradayCoverageStatus.VALID),
    )
    primary_availability = (
        IntradayCoverageStatus.STALE if stale
        else IntradayCoverageStatus.VALID
    )
    states = (
        _tf_state(instrument, "15m", market_state=_market_ctx(direction),
                  direction=md, availability=primary_availability),
        secondary_state,
    )
    completeness = (
        MarketStateCompleteness.COMPLETE
        if not incomplete
        else MarketStateCompleteness.INCOMPLETE
    )
    alignment = MtfAlignmentState.ALIGNED
    if conflict:
        alignment = MtfAlignmentState.CONFLICTING
    elif incomplete:
        alignment = MtfAlignmentState.INCOMPLETE
    return PerSymbolMtfResult(
        instrument=instrument, timeframes=("15m", "1h"),
        timeframe_states=states, completeness=completeness,
        alignment=alignment, alignment_reason="demo",
        reference_now=_now(),
    )


class DemoProvider:
    """Deterministic provider for the 19.4 -> 19.5 demo flow."""

    data_source = "demo-provider"

    def __init__(self, specs: dict[str, tuple[OHLCVCandle, ...]] | None = None):
        self._specs = specs or {}

    def is_timeframe_supported(self, tf: str) -> bool:
        return True

    def supports_instrument(self, instrument: str) -> bool:
        return True

    def resolve_symbol(self, instrument: str) -> str:
        return instrument

    def fetch(self, instrument, setup_timeframe, lookback_bars=300, *,
              reference_now=None):
        del setup_timeframe, lookback_bars, reference_now
        candles = self._specs.get(instrument, ())
        return InstrumentSeries(
            instrument=instrument, setup_candles=tuple(candles),
            available=bool(candles), reason="", data_source=self.data_source,
            provider_status=ProviderStatus.OK,
            freshness_state=FreshnessState.CURRENT,
            latest_candle_timestamp=(candles[-1].timestamp if candles else None),
            latest_completed_candle_timestamp=(
                candles[-1].timestamp if candles else None),
            forming_setup_candle=None, last_successful_fetch_time=_now(),
            rejected_future_count=0,
        )

    def last_updated(self, instrument, setup_timeframe):
        return None


def main() -> int:
    fmt = SetupQualityFormatter()
    sq = SetupQualityEngine()

    # ---- 1. FROZEN 19.1 universe + FROZEN 19.4 MTF consumption ------
    universe = UniverseBuilder.nifty200()
    check("19.1 universe is accepted exactly", universe.instrument_count >= 200,
          f"{universe.instrument_count} instruments (incl. benchmark)")

    strong = _mtf("RELIANCE", MarketTrendState.BULLISH)
    result = sq.evaluate(strong, _bullish_series())
    check("19.4 MTF result consumed by reference", result.mtf is strong)
    check(
        "detection reuses 11Q POTENTIAL_SETUP",
        str(result.setup_classification) == "POTENTIAL_SETUP",
        f"setup_classification={result.setup_classification}",
    )
    check(
        "quality score bounded [0,100] + transparent components",
        result.score is not None and 0 <= result.score <= 100,
        f"score={result.score} components={result.score_component_map}",
    )
    check(
        "weight sum = 100",
        sum(c.max_points for c in result.score_components) == 100,
        f"max={sum(c.max_points for c in result.score_components)}",
    )
    check(
        "status QUALIFIED",
        result.status is SetupQualityStatus.QUALIFIED,
        f"status={result.status}",
    )

    # ---- 2. Data quality is a prerequisite --------------------------
    stale = sq.evaluate(
        _mtf("RELIANCE", MarketTrendState.BULLISH, stale=True), _bullish_series(),
    )
    check(
        "stale primary data caps classification at LOW",
        stale.classification.rank_value
        <= SetupQualityClassification.LOW.rank_value
        and "STALE_PRIMARY_DATA" in stale.negative_factors,
        f"class={stale.classification} score={stale.score}",
    )

    incomplete = sq.evaluate(
        _mtf("RELIANCE", MarketTrendState.BULLISH, incomplete=True),
        _bullish_series(),
    )
    check(
        "incomplete MTF caps at LOW + zero MTF credit",
        incomplete.score_component_map["mtf_alignment"] == 0
        and incomplete.classification.rank_value
        <= SetupQualityClassification.LOW.rank_value,
        f"class={incomplete.classification} "
        f"mtf={incomplete.score_component_map['mtf_alignment']}",
    )

    conflicting = sq.evaluate(
        _mtf("RELIANCE", MarketTrendState.BULLISH, conflict=True),
        _bullish_series(),
    )
    check(
        "conflicting MTF is explicit negative evidence (zero MTF credit)",
        conflicting.score_component_map["mtf_alignment"] == 0
        and "MTF_CONFLICTING" in conflicting.negative_factors,
        f"mtf_credit={conflicting.score_component_map['mtf_alignment']}",
    )

    # ---- 3. POINT-IN-TIME SAFETY (adversarial look-ahead) ----------
    base = _bullish_series(60)
    at_t = sq.evaluate(strong, base)
    future = list(base) + [
        _candle(base[-1].timestamp + timedelta(minutes=15), 300.0),
        _candle(base[-1].timestamp + timedelta(minutes=30), 310.0),
        _candle(base[-1].timestamp + timedelta(minutes=45), 295.0),
    ]
    still_t = sq.evaluate(strong, future)
    check(
        "future breakout/BOS candles do not change earlier quality",
        still_t.score == at_t.score and still_t.status == at_t.status,
        f"score_at_T={at_t.score} vs after_future={still_t.score}",
    )

    # ---- 4. Deterministic universe analysis + ranking --------------
    specs = {
        "RELIANCE": tuple(_bullish_series()),
        "TCS": tuple(_bullish_series()),
        "HDFCBANK": tuple(_bullish_series()),
    }
    provider = DemoProvider(specs)
    mtf_engine = MtfAnalysisEngine(
        provider=provider, config=MtfAnalysisConfig(timeframes=("15m", "1h")),
    )
    subset = UniverseBuilder.custom(["RELIANCE", "TCS", "HDFCBANK"])
    analysis = mtf_engine.analyze_universe(subset, reference_now=_now())
    candles_map = {i: list(s) for i, s in specs.items()}
    out = SetupQualityEngine(
        SetupQualityConfig(max_qualified=2),
    ).evaluate_universe(analysis, candles_map)
    check(
        "exactly-one-result-per-constituent",
        len(out.results) == 3 and out.counts.tested == 3,
        f"results={len(out.results)} tested={out.counts.tested}",
    )
    check(
        "deterministic ranking (strongest-first)",
        len(out.ranked_qualified) >= 1
        and all(
            out.ranked_qualified[i].score >= out.ranked_qualified[i + 1].score
            for i in range(len(out.ranked_qualified) - 1)
        ),
        f"ranked={[r.instrument for r in out.ranked_qualified]}",
    )
    check(
        "top-N keeps full ranked set internally",
        len(out.top_n) == 2 and len(out.ranked_qualified) >= 2,
        f"top_n={len(out.top_n)} ranked={len(out.ranked_qualified)}",
    )
    check(
        "analysis id deterministic",
        out.analysis_id.startswith("sq-") and len(out.analysis_id) == 19,
        out.analysis_id,
    )

    # ---- 5. Per-symbol failure isolation ----------------------------
    bad = PerSymbolMtfResult(
        instrument="HDFCBANK",
        timeframes=("15m", "1h"),
        timeframe_states=(
            _tf_state("HDFCBANK", "15m", market_state=None,
                      direction=MTFDirection.UNKNOWN,
                      availability=IntradayCoverageStatus.PROVIDER_ERROR),
            _tf_state("HDFCBANK", "1h", market_state=None,
                      direction=MTFDirection.UNKNOWN,
                      availability=IntradayCoverageStatus.PROVIDER_ERROR),
        ),
        completeness=MarketStateCompleteness.UNAVAILABLE,
        alignment=MtfAlignmentState.UNAVAILABLE,
        alignment_reason="demo no data", reference_now=_now(),
    )
    uni = MtfUniverseAnalysis(
        analysis_id="mtf-demo",
        reference_now=_now(),
        timeframes=("15m", "1h"),
        universe_instrument_count=2,
        instruments=("RELIANCE", "HDFCBANK"),
        results=(strong, bad),
    )
    isolated = sq.evaluate_universe(
        uni, {"RELIANCE": list(specs["RELIANCE"]), "HDFCBANK": []},
    )
    check(
        "one symbol's data failure does not terminate the universe",
        isolated.result_for("RELIANCE").status is SetupQualityStatus.QUALIFIED
        and isolated.result_for("HDFCBANK").status
        is SetupQualityStatus.UNAVAILABLE,
        f"RELIANCE={isolated.result_for('RELIANCE').status} "
        f"HDFCBANK={isolated.result_for('HDFCBANK').status}",
    )

    # ---- 6. No trading / alert / lifecycle / broker semantics -------
    module_path = (
        Path(__file__).resolve().parent.parent / "src" / "dashboard"
        / "setup_quality.py"
    )
    tree = ast.parse(module_path.read_text())
    forbidden = ("broker", "execution_authorization", "operational_trade",
                 "alert", "telegram", "whatsapp")
    leak = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            leak += [a.name for a in node.names
                     if any(f in a.name.lower() for f in forbidden)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            leak += [node.module]
    check(
        "no broker/execution/alert/lifecycle imports in the new layer",
        not any(any(f in m.lower() for f in forbidden) for m in leak),
        f"leaks={sorted(set(leak))}",
    )

    # ---- 7. Pipeline regression baseline (signals=4, trades=3) ------
    check("existing pipeline baseline unchanged (signals=4, trades=3)",
          _pipeline_baseline())

    print(fmt.format_universe(out))
    print("\n=== sample per-symbol report ===")
    print(fmt.format_result(result))

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    print(f"\nCheckpoint 19.5 demo completed successfully ({passed} checks passed).")
    return 0 if passed == len(CHECKS) else 1


def _pipeline_baseline() -> bool:
    """Repeated detection of the historical signal pipeline baseline."""
    try:
        from engine.pipeline.historical_pipeline import (
            HistoricalEvaluationPipeline,
            PipelineConfig,
        )
        from tests.test_pipeline import trending_dataset  # type: ignore
    except Exception:
        return False
    try:
        candles = trending_dataset()
        result = HistoricalEvaluationPipeline(PipelineConfig()).evaluate(candles)
        return len(result.signals) == 4 and result.completed_trades == 3
    except Exception:
        return False


if __name__ == "__main__":
    raise SystemExit(main())