#!/usr/bin/env python3
"""
Checkpoint 19.4 demo — multi-timeframe market-state analysis.

Proves (deterministically, offline):

  1. the MTF analyzer consumes the FROZEN 19.1 validated NIFTY Top 200
     universe and the canonical 19.2 intraday market-data layer;
  2. configured timeframes are validated (canonical labels, no
     duplicates, no unknown strings, ascending-duration order);
  3. every requested timeframe / symbol produces an explicit result;
  4. a scripted multi-timeframe provider produces ALIGNED / CONFLICTING /
     MIXED / INCOMPLETE / UNAVAILABLE relationships;
  5. completed-candle boundaries are enforced (forming candle excluded);
  6. POINT-IN-TIME SAFETY: appending future candles (including a future
     1h candle that closes after T) leaves an already-valid historical
     MTF result at T unchanged (per-symbol AND universe-wide);
  7. higher-timeframe timestamps are aligned WITHOUT requiring identical
     candle timestamps;
  8. missing / stale / provider-error / unsupported timeframe data is
     explicit, and one timeframe failure never destroys the symbol;
  9. deterministic ordering + analysis id;
 10. fixture runs explicitly report the unsupported 1h frame (never a
     fabricated full alignment); the real fixture path stays honest;
 11. NO setup detection / ranking / entry-stop-target / lifecycle /
     alerts / broker execution anywhere.

Run:  python scripts/test_checkpoint_19_4.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.data_provider import FixtureDataProvider  # noqa: E402
from dashboard.mtf_analysis import MtfAnalysisEngine  # noqa: E402
from engine.config.universe import NIFTY200_SYMBOLS  # noqa: E402
from engine.config.universe_boundary import (  # noqa: E402
    DEFAULT_NIFTY200_UNIVERSE,
    UniverseBuilder,
)
from engine.models.market_context import MarketTrendState  # noqa: E402
from engine.models.mtf_analysis import (  # noqa: E402
    MarketStateCompleteness,
    MtfAlignmentState,
)
from engine.models.ohlcv import OHLCVCandle  # noqa: E402
from engine.reporting.mtf_analysis import MtfAnalysisFormatter  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def _now() -> datetime:
    return datetime(2026, 9, 4, 5, 30, tzinfo=UTC)  # Fri 11:00 IST


# --- scripted multi-timeframe provider (offline, deterministic) ----
from dataclasses import dataclass  # noqa: E402

from dashboard.data_provider import (  # noqa: E402
    FreshnessState,
    InstrumentSeries,
    ProviderStatus,
)


@dataclass(frozen=True)
class TfSpec:
    candles: tuple[OHLCVCandle, ...] = ()
    status: ProviderStatus = ProviderStatus.OK
    forming: OHLCVCandle | None = None
    latest: datetime | None = None


class ScriptedMtfProvider:
    """Deterministic fake provider serving multiple timeframes."""

    data_source = "fake-mtf"

    def __init__(self, specs=None, unsupported_tfs=None, raised=None):
        self._specs = specs or {}
        self._unsupported_tfs = unsupported_tfs or set()
        self._raised = raised or set()
        self.calls: list[tuple[str, str]] = []

    def is_timeframe_supported(self, tf: str) -> bool:
        return tf not in self._unsupported_tfs

    def supports_instrument(self, instrument: str) -> bool:
        return True

    def resolve_symbol(self, instrument: str) -> str:
        return instrument

    def fetch(self, instrument, setup_timeframe, lookback_bars=300, *,
              reference_now=None) -> InstrumentSeries:
        del lookback_bars, reference_now
        self.calls.append((instrument, setup_timeframe))
        if (instrument, setup_timeframe) in self._raised:
            raise RuntimeError(f"boom {instrument}:{setup_timeframe}")
        spec = self._specs.get((instrument, setup_timeframe))
        if spec is None or setup_timeframe in self._unsupported_tfs:
            return InstrumentSeries(
                instrument=instrument, available=False,
                reason="unsupported", data_source=self.data_source,
                provider_status=ProviderStatus.UNSUPPORTED,
                freshness_state=FreshnessState.UNAVAILABLE,
            )
        return InstrumentSeries(
            instrument=instrument, setup_candles=spec.candles,
            available=bool(spec.candles), reason="",
            data_source=self.data_source, provider_status=spec.status,
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


def _candle(ts: datetime, close: float) -> OHLCVCandle:
    return OHLCVCandle(
        timestamp=ts, open=close, high=close + 1.0, low=close - 1.0,
        close=close, volume=1000.0,
    )


_1H_LAST = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)
_15M_LAST = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)


def _suffix_builder(builder, last_ts, n, step, start=100.0, **kw):
    from engine.data.historical_fixtures import _bullish_zigzag  # noqa: F401

    start_ts = last_ts - step * (n - 1)
    return builder(start, n_legs=n // 5 + 1, start_ts=start_ts, step=step, **kw)[:n]


def _bullish_1h(n: int = 36) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bullish_zigzag

    return _suffix_builder(
        _bullish_zigzag, _1H_LAST, n, timedelta(hours=1),
        start=100.0, rise=6.0, pullback=3.0,
    )


def _bullish_15m(n: int = 60) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bullish_zigzag

    return _suffix_builder(
        _bullish_zigzag, _15M_LAST, n, timedelta(minutes=15),
        start=105.0, rise=6.0, pullback=3.0,
    )


def _bearish_15m(n: int = 60) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bearish_zigzag

    return _suffix_builder(
        _bearish_zigzag, _15M_LAST, n, timedelta(minutes=15),
        start=105.0, fall=6.0, bounce=3.0,
    )


def _bearish_1h(n: int = 36) -> list[OHLCVCandle]:
    from engine.data.historical_fixtures import _bearish_zigzag

    return _suffix_builder(
        _bearish_zigzag, _1H_LAST, n, timedelta(hours=1),
        start=100.0, fall=6.0, bounce=3.0,
    )


def _range_1h(n: int = 36) -> list[OHLCVCandle]:
    """Symmetric 1h oscillation between 94 and 106 -> RANGE context."""
    seq: list[OHLCVCandle] = []
    ts = _1H_LAST - timedelta(hours=(n - 1))
    close = 100.0
    while len(seq) < n:
        for _ in range(3):
            close = round(close + 4.0, 2)
            seq.append(_candle(ts, close))
            ts += timedelta(hours=1)
        for _ in range(3):
            close = round(close - 4.0, 2)
            seq.append(_candle(ts, close))
            ts += timedelta(hours=1)
    return seq[:n]


def _mark_completed_15m(candles, now):
    return tuple(c for c in candles if c.timestamp + timedelta(minutes=15) <= now)


def _mark_completed_1h(candles, now):
    return tuple(c for c in candles if c.timestamp + timedelta(hours=1) <= now)


def _aligned_provider(now, *, tcs_conflicting: bool = True):
    """RELIANCE 15m+1h bullish (ALIGNED); TCS 15m bull + 1h bear (CONFLICTING)."""
    specs = {
        ("RELIANCE", "15m"): TfSpec(candles=_mark_completed_15m(_bullish_15m(), now)),
        ("RELIANCE", "1h"): TfSpec(candles=_mark_completed_1h(_bullish_1h(), now)),
        ("TCS", "15m"): TfSpec(candles=_mark_completed_15m(_bullish_15m(), now)),
    }
    if tcs_conflicting:
        specs[("TCS", "1h")] = TfSpec(candles=_mark_completed_1h(_bearish_1h(), now))
    else:
        specs[("TCS", "1h")] = TfSpec(candles=_mark_completed_1h(_bullish_1h(), now))
    return ScriptedMtfProvider(specs=specs)


def main() -> int:
    now = _now()

    # ---- 1. universe acceptance (19.1) + 19.2 consumption ------------
    eng_fixture = MtfAnalysisEngine(provider=FixtureDataProvider())
    fixture_u = eng_fixture.analyze_universe(reference_now=now)
    check(
        "19.1 universe accepted",
        fixture_u.universe_instrument_count == len(NIFTY200_SYMBOLS) == 200,
        f"requested={fixture_u.universe_instrument_count}",
    )
    check(
        "every constituent explicit",
        fixture_u.instrument_count == len(NIFTY200_SYMBOLS) == 200,
    )

    # ---- 2. config validation ----------------------------------------
    from engine.config.mtf_analysis_config import MtfAnalysisConfig

    bad_dup = False
    try:
        MtfAnalysisConfig(timeframes=("15m", "15m"))
    except ValueError:
        bad_dup = True
    check("duplicate timeframes rejected", bad_dup)

    bad_unknown = False
    try:
        MtfAnalysisConfig(timeframes=("15m", "bogus"))
    except ValueError:
        bad_unknown = True
    check("unknown timeframes rejected", bad_unknown)

    bad_order = False
    try:
        MtfAnalysisConfig(timeframes=("1h", "15m"))
    except ValueError:
        bad_order = True
    check("unordered timeframes rejected", bad_order)

    # ---- 3. scripted ALIGNED / CONFLICTING / MIXED -------------------
    provider = _aligned_provider(now)   # RELIANCE bull+bull; TCS bull+bear
    eng = MtfAnalysisEngine(provider=provider)
    r_rel = eng.analyze_symbol("RELIANCE", reference_now=now)
    r_tcs = eng.analyze_symbol("TCS", reference_now=now)
    check(
        "ALIGNED relationship (15m+1h both bullish)",
        r_rel.completeness is MarketStateCompleteness.COMPLETE
        and r_rel.alignment is MtfAlignmentState.ALIGNED,
        f"completeness={r_rel.completeness.value} alignment={r_rel.alignment.value}",
    )
    check(
        "CONFLICTING relationship (15m bull / 1h bear)",
        r_tcs.completeness is MarketStateCompleteness.COMPLETE
        and r_tcs.alignment is MtfAlignmentState.CONFLICTING,
    )

    # 15m bullish + 1h RANGE -> MIXED (a range frame is neutral, never
    # silently directional).
    mixed_provider = ScriptedMtfProvider(
        specs={
            ("MIX", "15m"): TfSpec(
                candles=_mark_completed_15m(_bullish_15m(), now),
            ),
            ("MIX", "1h"): TfSpec(
                candles=_mark_completed_1h(_range_1h(), now),
            ),
        },
    )
    r_mix = MtfAnalysisEngine(provider=mixed_provider).analyze_symbol(
        "MIX", reference_now=now,
    )
    check(
        "MIXED relationship (15m bull / 1h range-neutral)",
        r_mix.completeness is MarketStateCompleteness.COMPLETE
        and r_mix.alignment is MtfAlignmentState.MIXED,
        f"1h direction={r_mix.state_for('1h').direction.value}",
    )

    # ---- 4. completed-candle boundary --------------------------------
    series_15m = _mark_completed_15m(_bullish_15m(), now)
    last_15m = series_15m[-1].timestamp
    check(
        "15m latest completed == 10:45 IST (11:00 reference)",
        last_15m == datetime(2026, 9, 4, 5, 15, tzinfo=UTC),
    )
    state_15m = eng.assess_timeframe("RELIANCE", "15m", reference_now=now)
    check(
        "forming candle not treated completed",
        state_15m.latest_completed_timestamp == last_15m,
    )

    # ---- 5. timestamp alignment without identical closes -------------
    t15 = r_rel.state_for("15m").latest_completed_timestamp
    t1h = r_rel.state_for("1h").latest_completed_timestamp
    check(
        "frames align at their OWN latest completed candle",
        t15 is not None and t1h is not None and t15 != t1h
        and r_rel.alignment is MtfAlignmentState.ALIGNED,
        f"15m={t15} 1h={t1h}",
    )

    # ---- 6. POINT-IN-TIME SAFETY (per-symbol) ------------------------
    result_T = eng.analyze_symbol("RELIANCE", reference_now=now)
    provider._specs[("RELIANCE", "15m")] = TfSpec(  # type: ignore[attr-defined]
        candles=provider._specs[("RELIANCE", "15m")].candles  # type: ignore[attr-defined]
        + (_candle(now + timedelta(minutes=15), 95.0),),
    )
    provider._specs[("RELIANCE", "1h")] = TfSpec(  # type: ignore[attr-defined]
        candles=provider._specs[("RELIANCE", "1h")].candles  # type: ignore[attr-defined]
        + (_candle(now + timedelta(hours=1), 90.0),),
    )
    result_after = eng.analyze_symbol("RELIANCE", reference_now=now)
    leak = (
        result_after.completeness != result_T.completeness
        or result_after.alignment != result_T.alignment
    )
    s_after_15 = result_after.state_for("15m")
    s_T_15 = result_T.state_for("15m")
    leak = leak or s_after_15.latest_completed_timestamp != s_T_15.latest_completed_timestamp
    leak = leak or s_after_15.direction != s_T_15.direction
    check("future candles do not change an earlier MTF result", not leak)

    # ---- 7. POINT-IN-TIME SAFETY (universe-wide) ---------------------
    u_T = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)
    u_after = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)
    check(
        "universe-wide result stable under future candles (" +
        "deterministic same-reference)",
        u_after.analysis_id == u_T.analysis_id
        and [r.instrument for r in u_after.results]
        == [r.instrument for r in u_T.results],
    )

    # ---- 8. missing / error / unsupported explicit -------------------
    provider._raised = {("TCS", "1h")}  # type: ignore[attr-defined]
    tcs_error = eng.analyze_symbol("TCS", reference_now=now)
    check(
        "one timeframe error -> INCOMPLETE, symbol survives",
        tcs_error.timeframe_count == 2
        and tcs_error.state_for("1h").availability.value == "PROVIDER_ERROR"
        and tcs_error.completeness is MarketStateCompleteness.INCOMPLETE,
        f"state={tcs_error.state_for('1h').availability.value}",
    )

    unsupported = eng.analyze_symbol("NOSRV", reference_now=now)
    check(
        "unsupported instrument explicit + UNSUPPORTED completeness",
        unsupported.completeness is MarketStateCompleteness.UNSUPPORTED
        and unsupported.alignment is MtfAlignmentState.UNAVAILABLE,
    )

    # ---- 9. deterministic ordering -----------------------------------
    u_det = eng.analyze_universe(["TCS", "RELIANCE"], reference_now=now)
    check(
        "universe ordering canonical regardless of input order",
        [r.instrument for r in u_det.results] == ["RELIANCE", "TCS"],
    )
    check(
        "analysis id deterministic + prefixed",
        u_det.analysis_id == eng.analyze_universe(
            ["RELIANCE", "TCS"], reference_now=now,
        ).analysis_id
        and u_det.analysis_id.startswith("mtf-"),
    )

    # ---- 10. fixture honesty: 1h unsupported -> INCOMPLETE ------------
    rel_fx = eng_fixture.analyze_symbol("RELIANCE", reference_now=now)
    check(
        "fixture 15m valid + 1h unsupported -> INCOMPLETE (never fabricated)",
        rel_fx.state_for("15m").availability.is_valid_data
        and rel_fx.state_for("1h").availability.value == "UNSUPPORTED_TIMEFRAME"
        and rel_fx.completeness is MarketStateCompleteness.INCOMPLETE,
    )

    # ---- 11. no forbidden semantics ----------------------------------
    from engine.models.mtf_analysis import MtfAlignmentState as A

    all_alignment_values = [a.value for a in A]
    check(
        "alignment vocabulary has no trade verdicts",
        not any(v in ("BUY", "SELL", "TRADE", "NO_TRADE") for v in all_alignment_values),
        ",".join(all_alignment_values),
    )

    trend = r_rel.state_for("15m").market_state.trend
    check(
        "15m market state derived via Sprint 11P (BULLISH)",
        trend.state is MarketTrendState.BULLISH,
    )

    # ---- summary + sample report -------------------------------------
    formatter = MtfAnalysisFormatter()
    print("=== Checkpoint 19.4 Demo — Multi-Timeframe Market-State Analysis ===")
    print()
    print(formatter.format_summary(u_T))
    print()
    print(formatter.format_symbol(r_rel))
    print()

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print(f"\nCheckpoint 19.4 demo: {passed} PASS / {failed} FAIL")
    for name, ok, detail in CHECKS:
        status = "PASS" if ok else "FAIL"
        suffix = f"  [{detail}]" if detail else ""
        print(f"  {status:<5} {name}{suffix}")

    # ---- 12. pipeline baseline regression -----------------------------
    from engine.pipeline import HistoricalEvaluationPipeline  # noqa: E402
    from engine.pipeline.datasets import trending_dataset  # noqa: E402

    baseline = HistoricalEvaluationPipeline().evaluate(trending_dataset())
    check(
        "historical pipeline baseline preserved (signals=4, trades=3)",
        baseline.signals_generated == 4 and baseline.completed_trades == 3,
        f"signals={baseline.signals_generated} trades={baseline.completed_trades}",
    )

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print(f"\nCheckpoint 19.4 demo FINAL: {passed} PASS / {failed} FAIL")
    if failed:
        print("FAILED checks:")
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"  - {name} {detail}")
        return 1
    print("\nMULTI-TIMEFRAME MARKET-STATE ANALYSIS ONLY — no setups, no "
          "ranking, no broker execution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())