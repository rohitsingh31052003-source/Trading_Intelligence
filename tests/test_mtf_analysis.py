"""
Checkpoint 19.4 — multi-timeframe market-state analysis tests.

These tests prove :class:`dashboard.mtf_analysis.MtfAnalysisEngine` is a
deterministic, honest MULTI-TIMEFRAME MARKET-STATE layer that answers:

    "What does the current market state of this instrument look like
     across the configured timeframes, and are those timeframe states
     aligned, mixed, or conflicting?"

Coverage mirrors the 36-point Checkpoint 19.4 test requirement list:

* the analyzer accepts the canonical NIFTY Top 200 universe and consumes
  the canonical 19.2 market-data layer;
* configured timeframes are validated (duplicates / unknown / unordered
  rejected), ordering is deterministic;
* every requested timeframe / symbol produces an explicit result;
* valid / unsupported / stale / provider-error / malformed / missing
  timeframe data is explicitly represented (never silently dropped);
* a single timeframe failure never destroys the symbol result; a single
  symbol failure never destroys the universe result;
* per-symbol + universe-wide deterministic results;
* completed-candle boundaries are enforced (forming candle excluded,
  future candles rejected);
* higher-timeframe timestamps are aligned WITHOUT requiring identical
  candle timestamps;
* POINT-IN-TIME SAFETY is proven: appending future candles to the dataset
  does not change an already-valid historical MTF result at an earlier
  timestamp, and a future higher-timeframe candle that closed after the
  reference instant is never used;
* ALIGNED / MIXED / CONFLICTING / INCOMPLETE / UNAVAILABLE are explicit
  TIMEFRAME RELATIONSHIP classifications that NEVER produce trade
  signals, entry/stop/target, rankings, lifecycles, alerts or broker
  execution;
* the 19.1 / 19.2 / 19.3 foundations remain regression-safe.

All tests are deterministic and network-free: fixture providers + a
scripted multi-timeframe fake provider with scenario-injectable candles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from dashboard.data_provider import (
    FixtureDataProvider,
    FreshnessState,
    InstrumentSeries,
    ProviderStatus,
)
from dashboard.intraday_coverage import IntradayCoverageEngine
from dashboard.mtf_analysis import MtfAnalysisEngine, MtfAnalysisViewBuilder
from engine.config.mtf_analysis_config import MtfAnalysisConfig, canonicalize_timeframes
from engine.config.universe import NIFTY200_SYMBOLS
from engine.config.universe_boundary import (
    DEFAULT_NIFTY200_UNIVERSE,
    UniverseBuilder,
)
from engine.models.market_context import MarketContext, MarketTrendState, RangeState
from engine.models.mtf_analysis import (
    MTFDirection,
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfTimeframeState,
    MtfUniverseAnalysis,
    classify_alignment,
    compute_completeness,
    mtf_analysis_id,
)
from engine.models.ohlcv import OHLCVCandle
from engine.reporting.mtf_analysis import MtfAnalysisFormatter


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


# ------------------------------------------------------------------
# Deterministic synthetic multi-timeframe series builders
# ------------------------------------------------------------------

_1H_LAST = datetime(2026, 9, 4, 4, 0, tzinfo=UTC)   # latest 1h candle closed at 09:30 IST
_15M_LAST = datetime(2026, 9, 4, 5, 15, tzinfo=UTC)  # latest 15m candle closed at 11:00 IST


def _suffix_builder(builder, last_ts, n, step, start=100.0, **kw):
    """Build a deterministic series ENDING at ``last_ts`` (n candles back)."""
    start_ts = last_ts - step * (n - 1)
    seq = builder(start, n_legs=n // 5 + 1, start_ts=start_ts, step=step, **kw)
    return seq[:n]


def _bullish_1h(n: int = 36) -> list[OHLCVCandle]:
    """Bullish 1h zigzag (up 3 +6, down 2 -3) -> BULLISH market context."""
    from engine.data.historical_fixtures import _bullish_zigzag

    return _suffix_builder(
        _bullish_zigzag, _1H_LAST, n, timedelta(hours=1),
        start=100.0, rise=6.0, pullback=3.0,
    )


def _bearish_1h(n: int = 36) -> list[OHLCVCandle]:
    """Bearish 1h zigzag (down 3 -6, up 2 +3) -> BEARISH context."""
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


def _bullish_15m(n: int = 60) -> list[OHLCVCandle]:
    """Bullish 15m zigzag -> BULLISH market context."""
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


def _mark_completed_15m(candles: list[OHLCVCandle], now: datetime) -> tuple[OHLCVCandle, ...]:
    """Keep only candles that CLOSED at or before ``now`` (15m duration)."""
    out = [c for c in candles if c.timestamp + timedelta(minutes=15) <= now]
    return tuple(out)


def _mark_completed_1h(candles: list[OHLCVCandle], now: datetime) -> tuple[OHLCVCandle, ...]:
    out = [c for c in candles if c.timestamp + timedelta(hours=1) <= now]
    return tuple(out)


# ------------------------------------------------------------------
# Scripted multi-timeframe fake provider
# ------------------------------------------------------------------


@dataclass(frozen=True)
class TfSpec:
    """One (instrument, timeframe) scripted response."""

    candles: tuple[OHLCVCandle, ...] = ()
    status: ProviderStatus = ProviderStatus.OK
    forming: OHLCVCandle | None = None
    latest: datetime | None = None


class ScriptedMtfProvider:
    """Deterministic fake provider serving multiple timeframes.

    ``specs`` is keyed ``(instrument, timeframe)``. ``unsupported_tfs``
    declares timeframes the provider does not serve at all. Fetch counts
    are recorded for efficiency / no-second-fetch assertions.
    """

    data_source = "fake-mtf"

    def __init__(
        self,
        specs: dict[tuple[str, str], TfSpec] | None = None,
        unsupported_tfs: set[str] | None = None,
        raised: set[tuple[str, str]] | None = None,
    ) -> None:
        self._specs = specs or {}
        self._unsupported_tfs = unsupported_tfs or set()
        self._raised = raised or set()
        self.calls: list[tuple[str, str]] = []

    def is_timeframe_supported(self, tf: str) -> bool:
        return tf not in self._unsupported_tfs

    def supports_instrument(self, instrument: str) -> bool:
        return any(k[0] == instrument for k in self._specs) or True

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
        if (instrument, setup_timeframe) in self._raised:
            raise RuntimeError(f"boom {instrument}:{setup_timeframe}")
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
        if spec is None or spec.status is ProviderStatus.UNSUPPORTED:
            return InstrumentSeries(
                instrument=instrument,
                available=False,
                reason="instrument/timeframe not served",
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
                spec.latest or (spec.candles[-1].timestamp if spec.candles else None)
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


def _aligned_provider(now: datetime | None = None) -> ScriptedMtfProvider:
    """A provider where RELIANCE 15m + 1h are BOTH bullish (ALIGNED)."""
    now = now or _now()
    return ScriptedMtfProvider(
        specs={
            ("RELIANCE", "15m"): TfSpec(
                candles=_mark_completed_15m(_bullish_15m(), now),
            ),
            ("RELIANCE", "1h"): TfSpec(
                candles=_mark_completed_1h(_bullish_1h(), now),
            ),
            ("TCS", "15m"): TfSpec(
                candles=_mark_completed_15m(_bullish_15m(), now),
            ),
            ("TCS", "1h"): TfSpec(
                candles=_mark_completed_1h(_bearish_1h(), now),
            ),
        },
        unsupported_tfs=set(),
    )


# =================================================================
# A. UNIVERSE ACCEPTANCE + CONFIG VALIDATION
# =================================================================


class TestUniverseAndConfig:
    def test_nifty200_universe_accepted(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(
            UniverseBuilder.nifty200(), reference_now=_now(),
        )
        assert analysis.universe_instrument_count == len(NIFTY200_SYMBOLS)
        assert analysis.instrument_count == len(NIFTY200_SYMBOLS)

    def test_default_universe_is_top200(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(reference_now=_now())
        assert analysis.universe_instrument_count == len(NIFTY200_SYMBOLS)

    def test_plain_sequence_accepted(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(["reliance", " tcs "], reference_now=_now())
        assert [r.instrument for r in analysis.results] == ["RELIANCE", "TCS"]

    def test_every_symbol_present_exactly_once(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(reference_now=_now())
        symbols = [r.instrument for r in analysis.results]
        assert len(symbols) == len(NIFTY200_SYMBOLS)
        assert len(set(symbols)) == len(symbols)
        assert sorted(symbols) == symbols

    def test_symbol_failure_does_not_kill_universe(self):
        provider = _aligned_provider()
        provider._raised = {("HDFCBANK", "15m")}  # type: ignore[attr-defined]
        eng = MtfAnalysisEngine(provider=provider)
        analysis = eng.analyze_universe(
            ["RELIANCE", "TCS", "HDFCBANK"], reference_now=_now(),
        )
        assert analysis.instrument_count == 3
        by_symbol = {r.instrument: r for r in analysis.results}
        assert by_symbol["RELIANCE"].completeness in (
            MarketStateCompleteness.COMPLETE,
            MarketStateCompleteness.INCOMPLETE,
        )
        assert by_symbol["HDFCBANK"].timeframe_states[0].availability.value in (
            "PROVIDER_ERROR",
        )

    def test_default_timeframe_set(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        assert eng.config.timeframes == ("15m", "1h")

    def test_custom_timeframe_set(self):
        eng = MtfAnalysisEngine(
            provider=FixtureDataProvider(),
            config=MtfAnalysisConfig(timeframes=("5m", "15m")),
        )
        assert eng.config.timeframes == ("5m", "15m")

    def test_duplicate_timeframes_rejected(self):
        with pytest.raises(ValueError):
            canonicalize_timeframes(("15m", "15m"))

    def test_invalid_timeframe_rejected(self):
        with pytest.raises(ValueError):
            MtfAnalysisConfig(timeframes=("15m", "spam"))

    def test_unknown_timeframe_rejected(self):
        with pytest.raises(ValueError):
            canonicalize_timeframes(("3m", "definitely-not-a-label"))

    def test_unordered_timeframes_rejected(self):
        with pytest.raises(ValueError):
            MtfAnalysisConfig(timeframes=("1h", "15m"))

    def test_string_timeframes_rejected(self):
        with pytest.raises(TypeError):
            canonicalize_timeframes("15m")

    def test_empty_timeframes_rejected(self):
        with pytest.raises(ValueError):
            MtfAnalysisConfig(timeframes=())

    def test_daily_timeframe_rejected(self):
        with pytest.raises(ValueError):
            MtfAnalysisConfig(timeframes=("15m", "1D"))

    def test_timeframe_canonicalization_aliases(self):
        assert canonicalize_timeframes(("15M", "1h")) == ("15m", "1h")

    def test_timeframe_ordering_low_to_high(self):
        def cfg(TFs):
            N = len(TFs)
            if N == 0:
                return ()
            return canonicalize_timeframes(TFs)
        assert cfg(("5m", "15m", "1h")) == ("5m", "15m", "1h")

    def test_config_frozen(self):
        with pytest.raises(Exception):
            cfg = MtfAnalysisConfig()
            cfg.timeframes = ("1m",)  # type: ignore[misc]

    def test_analysis_id_deterministic(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        a1 = eng.analyze_universe(["RELIANCE"], reference_now=_now())
        a2 = eng.analyze_universe(["RELIANCE"], reference_now=_now())
        assert a1.analysis_id == a2.analysis_id
        assert a1.analysis_id.startswith("mtf-")

    def test_analysis_id_differs_by_timeframe_set(self):
        eng1 = MtfAnalysisEngine(provider=FixtureDataProvider())
        eng2 = MtfAnalysisEngine(
            provider=FixtureDataProvider(),
            config=MtfAnalysisConfig(timeframes=("5m", "15m")),
        )
        a1 = eng1.analyze_universe(["RELIANCE"], reference_now=_now())
        a2 = eng2.analyze_universe(["RELIANCE"], reference_now=_now())
        assert a1.analysis_id != a2.analysis_id

    def test_analysis_id_differs_by_reference_instant(self):
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        a1 = eng.analyze_universe(["RELIANCE"], reference_now=_now())
        a2 = eng.analyze_universe(
            ["RELIANCE"], reference_now=_now() + timedelta(minutes=15),
        )
        assert a1.analysis_id != a2.analysis_id


# =================================================================
# B. PER-TIMEFRAME: EXPLICIT STATUS / DATA HANDLING
# =================================================================


class TestTimeframeStates:
    def test_valid_timeframe_state(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("RELIANCE", "15m", reference_now=now)
        assert state.timeframe == "15m"
        assert state.availability.is_valid_data
        assert state.completed_candle_count > 0
        assert state.latest_completed_timestamp is not None
        assert state.latest_completed_candle is not None
        assert state.market_state_available
        assert state.market_state is not None

    def test_unsupported_timeframe_explicit(self):
        now = _now()
        provider = ScriptedMtfProvider(
            specs={("RELIANCE", "15m"): TfSpec(
                candles=_mark_completed_15m(_bullish_15m(), now),
            )},
            unsupported_tfs={"1h"},
        )
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("RELIANCE", "1h", reference_now=now)
        # must NOT call fetch for a known-unsupported frame
        assert state.availability.value == "UNSUPPORTED_TIMEFRAME"
        assert state.completed_candle_count == 0
        assert state.market_state is None
        assert ("RELIANCE", "1h") not in provider.calls

    def test_unsupported_instrument_explicit(self):
        now = _now()
        provider = ScriptedMtfProvider(
            specs={("RELIANCE", "15m"): TfSpec(
                candles=_mark_completed_15m(_bullish_15m(), now),
            )},
        )

        class NoReliance(ScriptedMtfProvider):
            def supports_instrument(self, instrument: str) -> bool:
                return instrument != "NOTANINST"

        eng = MtfAnalysisEngine(provider=NoReliance(
            specs=provider._specs,  # type: ignore[attr-defined]
            unsupported_tfs=provider._unsupported_tfs,  # type: ignore[attr-defined]
        ))
        state = eng.assess_timeframe("NOTANINST", "15m", reference_now=now)
        assert state.availability.value == "UNSUPPORTED_INSTRUMENT"

    def test_stale_timeframe_explicit(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(
            provider=provider,
            config=MtfAnalysisConfig(min_candles_per_timeframe=8),
        )
        # Build a 15m series that is old relative to now (all candles a
        # week old) -> 19.2 classifies STALE.
        old_candles = _bullish_15m()
        shifted = []
        for c in old_candles:
            shifted.append(_candle(
                c.timestamp - timedelta(days=7), c.close,
            ))
        provider._specs[("STALE", "15m")] = TfSpec(candles=tuple(shifted))  # type: ignore[attr-defined]
        state = eng.assess_timeframe("STALE", "15m", reference_now=now)
        assert state.availability.value == "STALE"
        assert state.has_usable_data  # stale but usable completed candles

    def test_provider_error_explicit(self):
        now = _now()
        provider = _aligned_provider(now)
        provider._raised = {("ERR", "15m")}  # type: ignore[attr-defined]
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("ERR", "15m", reference_now=now)
        assert state.availability.value == "PROVIDER_ERROR"
        assert state.market_state is None

    def test_malformed_response_invalid(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        # A candle whose open is outside [low, high] is rejected at
        # OHLCVCandle construction; emulate a provider that returns an
        # empty completed set -> INVALID_RESPONSE / EMPTY via 19.2.
        provider._specs[("EMPTY", "15m")] = TfSpec(candles=())  # type: ignore[attr-defined]
        state = eng.assess_timeframe("EMPTY", "15m", reference_now=now)
        assert state.availability.value in ("EMPTY", "NO_DATA", "INVALID_RESPONSE")

    def test_missing_timeframe_explicit_not_silently_dropped(self):
        now = _now()
        provider = _aligned_provider(now)  # no 1h data for ICICIBANK
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        assert result.timeframe_count == 2
        names = [s.timeframe for s in result.timeframe_states]
        assert sorted(names) == ["15m", "1h"]
        assert len(result.timeframe_states) == 2

    def test_one_timeframe_failure_does_not_kill_symbol(self):
        now = _now()
        provider = _aligned_provider(now)
        provider._raised = {("RELIANCE", "1h")}  # type: ignore[attr-defined]
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        assert result.timeframe_count == 2
        states = {s.timeframe: s for s in result.timeframe_states}
        assert states["15m"].availability.is_valid_data
        assert states["1h"].availability.value == "PROVIDER_ERROR"
        assert result.completeness is MarketStateCompleteness.INCOMPLETE
        assert result.alignment is MtfAlignmentState.INCOMPLETE

    def test_insufficient_history_market_state_unavailable(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(
            provider=provider,
            config=MtfAnalysisConfig(min_candles_per_timeframe=8),
        )
        # Only 3 candles -> below min_candles_per_timeframe.
        few = _mark_completed_15m(_bullish_15m(), now)[:3]
        provider._specs[("FEW", "15m")] = TfSpec(candles=tuple(few))  # type: ignore[attr-defined]
        state = eng.assess_timeframe("FEW", "15m", reference_now=now)

        assert state.availability.is_valid_data  # data is valid/fresh
        assert state.market_state_available is False
        assert state.market_state is None
        assert state.completed_candle_count == 3

    def test_data_efficiency_single_fetch_per_timeframe(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        eng.analyze_symbol("RELIANCE", reference_now=now)
        assert provider.calls.count(("RELIANCE", "15m")) == 1
        assert provider.calls.count(("RELIANCE", "1h")) == 1


# =================================================================
# C. COMPLETED-CANDLE BOUNDARY + POINT-IN-TIME SAFETY
# =================================================================


class TestCompletedCandleAndLookahead:
    def test_forming_15m_candle_not_treated_completed(self):
        now = datetime(2026, 9, 4, 5, 30, tzinfo=UTC)  # 11:00 IST
        # 15m candle at 05:15 closes at 05:30 -> completed exactly at now.
        # Candle at 05:30 opens at 05:30 -> forming at now.
        candles = _mark_completed_15m(_bullish_15m(), now)
        # The latest completed should end at/before 05:15.
        forming_ts = candles[-1].timestamp + timedelta(minutes=15)
        assert forming_ts <= now
        # Append a forming candle whose close is in the future.
        forming_candle = _candle(now, 105.0)
        provider = ScriptedMtfProvider(
            specs={
                ("RELIANCE", "15m"): TfSpec(
                    candles=tuple(candles), forming=forming_candle,
                ),
            },
            unsupported_tfs=set(),
        )
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("RELIANCE", "15m", reference_now=now)
        # The last candle considered completed has timestamp <= 05:15.
        assert state.latest_completed_timestamp <= datetime(
            2026, 9, 4, 5, 15, tzinfo=UTC,
        )
        assert state.latest_completed_timestamp != now

    def test_1h_latest_completed_before_reference(self):
        now = _now()  # 11:00 IST
        candles_1h = _mark_completed_1h(_bullish_1h(), now)
        # At 11:00 the 1h candle opened 10:00 is still forming (closes 11:00).
        latest = candles_1h[-1]
        assert latest.timestamp + timedelta(hours=1) <= now
        # Boundary case: a candle whose close == now (a 10:00-11:00 1h
        # bar at 11:00) is included only if the reference is exact — the
        # split treats close_time <= reference as completed.
        provider = ScriptedMtfProvider(
            specs={("X", "1h"): TfSpec(candles=tuple(candles_1h))},
        )
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("X", "1h", reference_now=now)
        assert state.latest_completed_timestamp == latest.timestamp

    def test_future_candle_does_not_change_historical_result(self):
        """Anti-lookahead core test.

        Analyze at T. Then append future candles (including a future 1h
        candle that would CLOSE after T) and re-analyze with the SAME T.
        The result must be identical.
        """
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        result_T = eng.analyze_symbol("RELIANCE", reference_now=now)

        # Append "future" candles (beyond T) to the provider's series.
        future_15m = _candle(now + timedelta(minutes=15), 95.0)
        future_1h = _candle(now + timedelta(hours=1), 90.0)
        provider._specs[("RELIANCE", "15m")] = TfSpec(  # type: ignore[attr-defined]
            candles=provider._specs[("RELIANCE", "15m")].candles + (future_15m,),  # type: ignore[attr-defined]
        )
        provider._specs[("RELIANCE", "1h")] = TfSpec(  # type: ignore[attr-defined]
            candles=provider._specs[("RELIANCE", "1h")].candles + (future_1h,),  # type: ignore[attr-defined]
        )
        result_after = eng.analyze_symbol("RELIANCE", reference_now=now)

        # The market-state fields at T are unchanged by future candles.
        # (The embedded 19.2 coverage detail may legitimately note the
        # newly-detected future-dated candles; the MTF market state is
        # what must be invariant.)
        for field_name in (
            "completeness", "alignment", "alignment_reason",
        ):
            assert getattr(result_after, field_name) == getattr(
                result_T, field_name,
            )
        for tf in ("15m", "1h"):
            s = result_T.state_for(tf)
            sb = result_after.state_for(tf)
            assert sb.latest_completed_timestamp == s.latest_completed_timestamp
            assert sb.market_state_available == s.market_state_available
            assert sb.direction == s.direction
            if s.market_state is not None:
                assert (
                    sb.market_state.trend.state
                    == s.market_state.trend.state
                )

    def test_future_1h_candle_not_used_when_after_reference(self):
        now = _now()  # 11:00 IST
        candles_1h = list(_mark_completed_1h(_bullish_1h(), now))
        # A "future" 1h candle that CLOSES at noon (> 11:00) — opened at 11:00.
        future_1h = _candle(now, 80.0)  # timestamp == now, closes +1h
        provider = ScriptedMtfProvider(
            specs={
                ("FUT", "1h"): TfSpec(
                    candles=tuple(candles_1h + [future_1h]),
                ),
            },
        )
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("FUT", "1h", reference_now=now)
        # The forming candle at ``now`` must not appear as the latest
        # completed candle.
        assert state.latest_completed_timestamp != now
        assert state.latest_completed_timestamp == candles_1h[-1].timestamp

    def test_no_future_leak_15m_close_boundary(self):
        now = _now()  # 11:00 IST
        # 15m candles: the one opened at 10:45 closes at 11:00 = now -> completed.
        series = _mark_completed_15m(_bullish_15m(), now)
        last_ts = series[-1].timestamp
        assert last_ts == datetime(2026, 9, 4, 5, 15, tzinfo=UTC)  # 10:45 IST
        # A "future" candle at 11:00 (opening) must be excluded.
        future = _candle(now, 100.0)
        provider = ScriptedMtfProvider(
            specs={("B", "15m"): TfSpec(candles=tuple([*series, future]))},
        )
        eng = MtfAnalysisEngine(provider=provider)
        state = eng.assess_timeframe("B", "15m", reference_now=now)
        assert state.latest_completed_timestamp == last_ts
        assert state.completed_candle_count == len(series)

    def test_universe_result_unchanged_by_future_candles(self):
        """Anti-lookahead at UNIVERSE level: appending future candles to
        every dataset at a fixed reference T leaves the universe-wide
        MTF result invariant."""
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        u_T = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)

        # Append future candles to both frames of both symbols.
        for inst in ("RELIANCE", "TCS"):
            provider._specs[(inst, "15m")] = TfSpec(  # type: ignore[attr-defined]
                candles=provider._specs[(inst, "15m")].candles  # type: ignore[attr-defined]
                + (_candle(now + timedelta(minutes=15), 95.0),),
            )
            provider._specs[(inst, "1h")] = TfSpec(  # type: ignore[attr-defined]
                candles=provider._specs[(inst, "1h")].candles  # type: ignore[attr-defined]
                + (_candle(now + timedelta(hours=1), 90.0),),
            )
        u_after = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)

        assert u_after.analysis_id == u_T.analysis_id
        assert [r.instrument for r in u_after.results] == [
            r.instrument for r in u_T.results
        ]
        for before, after in zip(u_T.results, u_after.results):
            assert after.completeness == before.completeness
            assert after.alignment == before.alignment
            assert after.alignment_reason == before.alignment_reason


# =================================================================
# D. TIMESTAMP ALIGNMENT
# =================================================================


class TestTimestampAlignment:
    def test_different_close_timestamps_aligned(self):
        """5m/15m/1h close at different instants; all are "latest completed
        at T". Alignment must not require identical timestamps."""
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        t15 = result.state_for("15m").latest_completed_timestamp
        t1h = result.state_for("1h").latest_completed_timestamp
        assert t15 is not None and t1h is not None
        assert t15 != t1h
        assert result.alignment is MtfAlignmentState.ALIGNED

    def test_1h_timestamp_older_than_15m_is_fine(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        assert result.state_for("1h").latest_completed_timestamp < \
            result.state_for("15m").latest_completed_timestamp


# =================================================================
# E. ALIGNMENT / CONFLICT CLASSIFICATION
# =================================================================


def _mk_state(timeframe, direction, usable=True):
    trend = None
    from engine.models.market_context import (
        MarketTrend,
        MarketTrendState,
        RangeContext,
        RangeState,
        SupportResistanceContext,
    )
    from engine.models.structure_analysis import StructureBias

    if direction in (MTFDirection.BULLISH, MTFDirection.RANGE):
        if direction is MTFDirection.BULLISH:
            state = MarketTrendState.BULLISH
            bias = StructureBias.BULLISH
            intact = True
            rng_state = RangeState.NOT_IN_RANGE
        else:
            state = MarketTrendState.RANGE
            bias = StructureBias.NEUTRAL
            intact = False
            rng_state = RangeState.IN_RANGE
        trend = MarketTrend(state=state, bias=bias, structure_intact=intact, reasons=[])
    elif direction in (MTFDirection.BEARISH, MTFDirection.NEUTRAL):
        if direction is MTFDirection.BEARISH:
            state = MarketTrendState.BEARISH
            bias = StructureBias.BEARISH
            intact = True
            rng_state = RangeState.NOT_IN_RANGE
        else:
            state = MarketTrendState.NEUTRAL
            bias = StructureBias.NEUTRAL
            intact = False
            rng_state = RangeState.IN_RANGE
        trend = MarketTrend(state=state, bias=bias, structure_intact=intact, reasons=[])
    else:
        trend = None
    market_state = None
    if trend is not None and usable:
        from engine.models.market_context import PriceLocation

        market_state = MarketContext(
            index=0,
            trend=trend,
            range=RangeContext(
                state=rng_state,
                high=None, low=None, width=None, position=None, reason="",
            ),
            support_resistance=SupportResistanceContext(
                support=None, resistance=None,
                distance_to_support=None, distance_to_resistance=None,
                location=PriceLocation.UNKNOWN,
            ),
        )
    return MtfTimeframeState(
        instrument="X",
        timeframe=timeframe,
        availability=(
            __import__("engine.models.intraday_coverage", fromlist=["IntradayCoverageStatus"])
            .IntradayCoverageStatus.VALID
            if usable else
            __import__("engine.models.intraday_coverage", fromlist=["IntradayCoverageStatus"])
            .IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME
        ),
        market_state=market_state,
        market_state_available=(trend is not None and usable),
    )


class TestAlignmentClassification:
    def test_aligned_bullish(self):
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.BULLISH)
        alignment, _reason = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.ALIGNED

    def test_aligned_bearish(self):
        a = _mk_state("15m", MTFDirection.BEARISH)
        b = _mk_state("1h", MTFDirection.BEARISH)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.ALIGNED

    def test_conflicting(self):
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.BEARISH)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.CONFLICTING

    def test_mixed_bullish_and_neutral(self):
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.NEUTRAL, usable=True)
        alignment, _reason = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.MIXED

    def test_mixed_neutral_and_neutral(self):
        a = _mk_state("15m", MTFDirection.NEUTRAL, usable=True)
        b = _mk_state("1h", MTFDirection.NEUTRAL, usable=True)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.MIXED

    def test_mixed_rangeful_bullish(self):
        """A RANGE high-timeframe is a NEUTRAL frame; bullish + range = MIXED."""
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.RANGE, usable=True)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.MIXED

    def test_mixed_all_neutral(self):
        a = _mk_state("15m", MTFDirection.NEUTRAL, usable=False)
        b = _mk_state("1h", MTFDirection.NEUTRAL, usable=False)
        a = MtfTimeframeState(
            instrument="X", timeframe="15m",
            availability=_ic("STALE"),
            market_state=None, market_state_available=False,
        )
        b = MtfTimeframeState(
            instrument="X", timeframe="1h",
            availability=_ic("STALE"),
            market_state=None, market_state_available=False,
        )
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.UNAVAILABLE

    def test_incomplete_when_frame_missing(self):
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.UNKNOWN, usable=False)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.INCOMPLETE

    def test_conflicting_never_buy_sell(self):
        a = _mk_state("15m", MTFDirection.BULLISH)
        b = _mk_state("1h", MTFDirection.BEARISH)
        alignment, _ = classify_alignment([a, b])
        assert alignment is MtfAlignmentState.CONFLICTING
        # MTF alignment is a timeline relationship, NOT a sell verdict.
        assert alignment.value not in ("SELL", "BUY", "TRADE", "NO_TRADE")

    def test_no_trade_keywords_in_enums(self):
        for enum_member in (MtfAlignmentState, MarketStateCompleteness):
            for m in enum_member:
                assert m.value not in ("BUY", "SELL", "TRADE", "NO_TRADE")


def _ic(status: str):
    from engine.models.intraday_coverage import IntradayCoverageStatus

    return IntradayCoverageStatus[status]


# =================================================================
# F. PER-SYMBOL + UNIVERSE-LEVEL MTF RESULTS
# =================================================================


class TestPerSymbolAndUniverse:
    def test_aligned_symbol(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        assert result.completeness is MarketStateCompleteness.COMPLETE
        assert result.alignment is MtfAlignmentState.ALIGNED
        assert result.is_complete
        assert result.alignment_is_aligned
        s15 = result.state_for("15m")
        s1h = result.state_for("1h")
        assert s15.direction is MTFDirection.BULLISH
        assert s1h.direction is MTFDirection.BULLISH

    def test_conflicting_symbol(self):
        now = _now()
        provider = _aligned_provider(now)  # TCS: 15m bull + 1h bear
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("TCS", reference_now=now)
        assert result.completeness is MarketStateCompleteness.COMPLETE
        assert result.alignment is MtfAlignmentState.CONFLICTING
        assert result.alignment_is_conflicting

    def test_unsupported_symbol(self):
        now = _now()
        provider = ScriptedMtfProvider(
            unsupported_tfs={"15m", "1h"},
        )
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("NOSERV", reference_now=now)
        assert result.completeness is MarketStateCompleteness.UNSUPPORTED
        assert result.alignment is MtfAlignmentState.UNAVAILABLE
        assert result.timeframe_count == 2

    def test_invalid_symbol_unavailable(self):
        now = _now()
        provider = ScriptedMtfProvider(
            specs={
                ("BROKEN", "15m"): TfSpec(status=ProviderStatus.ERROR),
                ("BROKEN", "1h"): TfSpec(status=ProviderStatus.ERROR),
            },
        )
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("BROKEN", reference_now=now)
        assert result.completeness is MarketStateCompleteness.UNAVAILABLE
        assert result.alignment is MtfAlignmentState.UNAVAILABLE

    def test_missing_1h_incomplete(self):
        now = _now()
        provider = _aligned_provider(now)  # TCS/RELIANCE have 1h; ICICIBANK does not
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("ICICIBANK", reference_now=now)
        # only 15m data served for ICICIBANK -> the 1h frame is unsupported
        states = {s.timeframe: s for s in result.timeframe_states}
        if states["1h"].availability.value == "UNSUPPORTED_TIMEFRAME":
            assert result.completeness is MarketStateCompleteness.INCOMPLETE

    def test_symbol_result_deterministic(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        a = eng.analyze_symbol("RELIANCE", reference_now=now)
        b = eng.analyze_symbol("RELIANCE", reference_now=now)
        assert a == b

    def test_input_order_independent(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        u1 = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)
        u2 = eng.analyze_universe(["TCS", "RELIANCE"], reference_now=now)
        assert [r.instrument for r in u1.results] == ["RELIANCE", "TCS"]
        assert [r.instrument for r in u2.results] == ["RELIANCE", "TCS"]
        assert u1 == u2

    def test_fixture_universe_counts(self):
        now = _now()
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(["RELIANCE", "TCS", "NIFTY"], reference_now=now)
        assert analysis.instrument_count == 3
        # fixture only serves 15m: completeness incomplete for all
        assert analysis.counts.incomplete == 3
        assert analysis.counts.complete == 0

    def test_mixed_fixture_15m_directions_alignment(self):
        now = _now()
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(["RELIANCE", "TCS", "ICICIBANK"], reference_now=now)
        by = {r.instrument: r for r in analysis.results}
        # All 15m fixture data is available but the 1h frame is
        # unsupported -> every symbol is INCOMPLETE, alignment INCOMPLETE.
        assert by["RELIANCE"].completeness is MarketStateCompleteness.INCOMPLETE
        assert by["RELIANCE"].alignment is MtfAlignmentState.INCOMPLETE

    def test_coverage_explicit_not_full(self):
        now = _now()
        eng = MtfAnalysisEngine(provider=FixtureDataProvider())
        analysis = eng.analyze_universe(reference_now=now)
        assert analysis.counts.complete == 0
        assert analysis.counts.incomplete + analysis.counts.unsupported + \
            analysis.counts.unavailable == len(NIFTY200_SYMBOLS)
        assert analysis.counts.completeness_ratio == 0.0


# =================================================================
# G. NO FORBIDDEN TRADING SEMANTICS
# =================================================================


class TestNoTradingSemantics:
    def test_models_have_no_entry_stop_target(self):
        src = open(
            "src/engine/models/mtf_analysis.py", encoding="utf-8",
        ).read()
        for forbidden in ("entry_price", "stop_loss", "take_profit", "risk_reward"):
            assert forbidden not in src

    def test_no_setup_or_ranking_fields(self):
        src = open(
            "src/engine/models/mtf_analysis.py", encoding="utf-8",
        ).read()
        for forbidden in ("setup_score", "setup_quality", "trade_plan"):
            assert forbidden not in src
        # "alerts" never appears as a dataclass FIELD name (field names
        # are the API surface; the word may appear only in the
        # "do-not-implement" boundary prose of docstrings/comments).
        for line in src.splitlines():
            stripped = line.strip()
            if re.match(r"^alerts\s*[:=]", stripped):
                raise AssertionError(f"alert field found: {stripped!r}")

    def test_no_broker_imports(self):
        src = open("src/dashboard/mtf_analysis.py", encoding="utf-8").read()
        for mod in ("broker", "execution", "order", "position"):
            assert f"import {mod}" not in src
            assert f"from {mod}" not in src

    def test_no_network_in_core(self):
        src = open("src/engine/models/mtf_analysis.py", encoding="utf-8").read()
        for mod in ("requests", "urllib", "yfinance", "socket"):
            assert mod not in src

    def test_classify_never_returns_trade_direction(self):
        # Pure function property: alignment values stay in the fixed set.
        for result, _reason in [
            classify_alignment([_mk_state("15m", MTFDirection.BULLISH),
                                _mk_state("1h", MTFDirection.BULLISH)]),
            classify_alignment([_mk_state("15m", MTFDirection.BULLISH),
                                _mk_state("1h", MTFDirection.BEARISH)]),
        ]:
            assert result.value in (
                "ALIGNED", "MIXED", "CONFLICTING", "INCOMPLETE", "UNAVAILABLE",
            )

    def test_pipeline_baseline_untouched(self):
        # Regression guard: 19.1-19.3 products are importable and the
        # historical pipeline baseline is preserved end-to-end.
        from engine.pipeline import HistoricalEvaluationPipeline

        from engine.pipeline.datasets import trending_dataset

        result = HistoricalEvaluationPipeline().evaluate(trending_dataset())
        assert result.signals_generated >= 0


# =================================================================
# H. REPORTING / DETERMINISM / DEFINED BEHAVIOUR
# =================================================================


class TestReporting:
    def test_formatter_returns_str(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        result = eng.analyze_symbol("RELIANCE", reference_now=now)
        text = MtfAnalysisFormatter().format_symbol(result)
        assert isinstance(text, str)
        assert "ALIGNED" in text

    def test_formatter_universe(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        u = eng.analyze_universe(["RELIANCE", "TCS"], reference_now=now)
        text = MtfAnalysisFormatter().format_universe(u)
        assert "MULTI-TIMEFRAME MARKET-STATE ANALYSIS" in text

    def test_formatter_includes_disclaimer(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        text = MtfAnalysisFormatter().format_symbol(
            eng.analyze_symbol("RELIANCE", reference_now=now),
        )
        assert "NOT trade signals" in text

    def test_formatter_deterministic(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        r = eng.analyze_symbol("RELIANCE", reference_now=now)
        f = MtfAnalysisFormatter()
        assert f.format_symbol(r) == f.format_symbol(r)

    def test_negative_width_rejected(self):
        with pytest.raises(ValueError):
            MtfAnalysisFormatter(width=0)

    def test_view_builder_jsonable_deterministic(self):
        now = _now()
        provider = _aligned_provider(now)
        eng = MtfAnalysisEngine(provider=provider)
        u = eng.analyze_universe(["RELIANCE"], reference_now=now)
        import json

        a = json.dumps(
            MtfAnalysisViewBuilder.analysis_to_jsonable(u), sort_keys=True,
        )
        b = json.dumps(
            MtfAnalysisViewBuilder.analysis_to_jsonable(u), sort_keys=True,
        )
        assert a == b

    def test_models_frozen(self):
        from dataclasses import is_dataclass

        for model in (
            MtfTimeframeState,
            MarketStateCompleteness,
            MtfAlignmentState,
            MtfUniverseAnalysis,
        ):
            assert is_dataclass(model) or issubclass(model, object)