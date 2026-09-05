"""
Multi-timeframe market-state analysis engine (Checkpoint 19.4).

This is the MTF-analysis half of Checkpoint 19.4. It consumes the
FROZEN 19.1 validated NIFTY Top 200 universe, the FROZEN 19.2 canonical
intraday market-data layer (:class:`dashboard.intraday_coverage.IntradayCoverageEngine`),
and the Sprint 11P descriptive market-context engine, and turns them
into a deterministic MULTI-TIMEFRAME MARKET-STATE result that answers:

    "What does the current market state of this instrument look like
     across the configured timeframes, and are those timeframe states
     aligned, mixed, or conflicting?"

The engine implements NO trading intelligence:

* NO setup detection, NO setup scoring, NO setup quality, NO trade
  ranking, NO entry signals, NO buy/sell decisions, NO entry/stop/target,
  NO risk/reward, NO trade plans, NO setup lifecycle, NO alerts,
  NO broker execution.
* An MTF result describes TIMEFRAME MARKET STATE and TIMEFRAME
  RELATIONSHIPS only. ``ALIGNED`` never means BUY; ``CONFLICTING`` never
  means SELL / NO-TRADE.

Design (the smallest architecture compatible with the project):

* :class:`MtfAnalysisEngine` is STATELESS and PURE. Per configured
  timeframe it performs exactly ONE provider fetch and derives the
  19.2 availability classification by REUSING the 19.2 layer's
  classification method (completed-candle boundary + validation +
  duplicate/out-of-order/future-rejection + session-aware freshness +
  gap refinement) — no classification logic is duplicated and no
  wasteful second fetch occurs. The market state for that timeframe is
  computed from the SAME completed candles via the reusable, point-in-
  time-safe Sprint 11P :class:`MarketContextEngine`.
* Provider capability discovery is the 19.2 capability gate: an
  unsupported timeframe/instrument is classified explicitly and never
  fetched; a missing/stale/error timeframe is preserved as an explicit
  per-timeframe status.
* Alignment/conflict classification is the pure, deterministic
  :func:`engine.models.mtf_analysis.classify_alignment` relation over
  the per-timeframe states (ALIGNED / MIXED / CONFLICTING / INCOMPLETE /
  UNAVAILABLE).
* Determinism: universe order, timeframe order, reference instant and
  status classifications are all explicit / canonical / sorted. The
  analysis id is ``mtf-<sha256[:16]>`` over (timeframes + sorted
  universe + reference instant).
* Session awareness is REUSED from 19.2 (``market_session`` module).
* One timeframe failure never destroys the symbol result; one symbol
  failure never destroys the universe result (per-symbol /
  per-timeframe failure isolation).

NO-LOOK-AHEAD (structural): every market state is computed from the
completed-candle slice ``split_completed_candles(...).completed`` at the
explicit reference instant ``T``; a forming candle and any future-dated
candle are never fed to the intelligence engine (the Sprint 11P market
context and the 19.2 classification are both functions of candles
available at ``T``). Tests prove that appending future candles does not
change an already-valid historical MTF result at an earlier timestamp.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from dashboard.data_provider import (
    DashboardDataProvider,
    InstrumentSeries,
    split_completed_candles,
)
from dashboard.intraday_coverage import (
    DEFAULT_INTRADAY_TIMEFRAME,
    IntradayCoverageConfig,
    IntradayCoverageEngine,
)
from engine.config.mtf_analysis_config import MtfAnalysisConfig
from engine.config.swing_config import SwingConfig
from engine.config.universe_boundary import (
    DEFAULT_NIFTY200_UNIVERSE,
    UniverseDefinition,
)
from engine.data.market_session import (
    market_session_state,
    seconds_until_next_open,
)
from engine.data.validator import DataValidator
from engine.intelligence.market_context_engine import MarketContextEngine
from engine.models.intraday_coverage import (
    IntradayCoverageStatus,
    IntradayInstrumentCoverage,
)
from engine.models.mtf_analysis import (
    MTFDirection,
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfTimeframeState,
    MtfUniverseAnalysis,
    MtfUniverseCounts,
    PerSymbolMtfResult,
    classify_alignment,
    compute_completeness,
    mtf_analysis_id,
)
from engine.models.ohlcv import OHLCVCandle


def _canonical_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _resolve_universe_names(
    universe: UniverseDefinition | Sequence[str] | None,
) -> tuple[str, ...]:
    """Canonical, sorted, de-duplicated instrument tuple from any input."""

    if universe is None:
        universe = DEFAULT_NIFTY200_UNIVERSE
    if isinstance(universe, UniverseDefinition):
        names = list(universe.symbols)
    else:
        names = [_canonical_name(n) for n in universe]
    return tuple(sorted(set(names)))


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )
    return value


def _validated_completed_candles(
    series: InstrumentSeries,
    timeframe: str,
    now: datetime,
) -> tuple[tuple[OHLCVCandle, ...], OHLCVCandle | None]:
    """
    Completed-candle boundary + defensive validation (the SAME canonical
    path the 19.2 classification uses internally).

    Returns ``(validated_completed, forming)``. A forming candle (open
    <= now, close > now) and future-dated candles are excluded from the
    completed list (display-only forming kept). Impossible candles are
    dropped. Never raises on candle-level problems.
    """

    raw = tuple(series.setup_candles or ())
    boundary = split_completed_candles(raw, timeframe, now)
    valid: list[OHLCVCandle] = []
    for candle in boundary.completed:
        try:
            DataValidator.validate_candle(candle)
            valid.append(candle)
        except (ValueError, TypeError):
            continue
    return tuple(valid), boundary.forming


class MtfAnalysisEngine:
    """
    Stateless, deterministic multi-timeframe market-state engine
    (Checkpoint 19.4).

    The engine accepts the validated 19.1 universe (or any sequence of
    canonical instrument names), evaluates each configured timeframe
    through the canonical 19.2 intraday classification + the reusable
    Sprint 11P market-context engine, and produces deterministic
    per-symbol + universe-wide MTF results.
    """

    def __init__(
        self,
        provider: DashboardDataProvider | None = None,
        config: MtfAnalysisConfig | None = None,
        coverage_engine: IntradayCoverageEngine | None = None,
    ) -> None:
        # A coverage engine may be injected directly (it already owns a
        # provider); otherwise build one over the supplied provider.
        if coverage_engine is not None:
            self.coverage = coverage_engine
        else:
            coverage_cfg = IntradayCoverageConfig(
                timeframe=(
                    config.timeframes[0]
                    if config is not None and config.timeframes
                    else DEFAULT_INTRADAY_TIMEFRAME
                ),
            )
            if provider is not None:
                self.coverage = IntradayCoverageEngine(
                    provider=provider,
                    config=coverage_cfg,
                )
            else:
                self.coverage = IntradayCoverageEngine(config=coverage_cfg)
        self.provider = self.coverage.provider
        self.config = config or MtfAnalysisConfig()

        # Reusable, untouched Sprint 11P descriptive market-context
        # engine (stateless, pure, point-in-time safe).
        self._market_context_engine = MarketContextEngine(
            swing_config=SwingConfig(),
        )

    @classmethod
    def build(
        cls,
        provider_name: str = "fixture",
        *,
        timeframes: Sequence[str] | None = None,
        reference_now: datetime | None = None,
    ) -> "MtfAnalysisEngine":
        """
        Stateless factory over the EXISTING 19.2 ``build`` factory.

        ``"fixture"`` (default) -> the deterministic offline fixture
        provider. ``"yahoo"`` -> the OPTIONAL live/near-live Yahoo
        provider (requires ``yfinance``; graceful on failure). Never
        silently falls back between providers. No broker provider is
        ever constructed here.
        """

        del reference_now  # retained for API symmetry; providers are built analytically
        from dashboard.intraday_coverage import IntradayCoverageEngine

        return cls(
            coverage_engine=IntradayCoverageEngine.build(
                provider_name=provider_name,
            ),
            config=MtfAnalysisConfig(
                timeframes=tuple(timeframes) if timeframes else ("15m", "1h"),
            ),
        )

    # ------------------------------------------------------------
    # PER-TIMEFRAME ASSESSMENT
    # ------------------------------------------------------------

    def assess_timeframe(
        self,
        instrument: str,
        timeframe: str,
        *,
        reference_now: datetime | None = None,
    ) -> MtfTimeframeState:
        """
        Evaluate ONE (instrument, timeframe) market-state snapshot
        (deterministic).

        Capability gate first (never fetch a known-unsupported frame),
        then exactly ONE fetch + the 19.2 classification + Sprint 11P
        market state. Fails are classified, never raised.
        """

        now = _require_aware(
            reference_now if reference_now is not None else datetime.now(UTC),
            "reference_now",
        )
        canon = _canonical_name(instrument)
        tf = self._canonical_tf(timeframe)
        if tf is None:
            raise ValueError(f"unknown timeframe label {timeframe!r}.")

        # ---- Capability gate (19.2 vocabulary; unsupported -> no fetch) ----
        tf_supported = self._is_timeframe_supported(canon, tf)
        instrument_supported = self._is_instrument_supported(canon)
        if not tf_supported:
            return MtfTimeframeState(
                instrument=canon,
                timeframe=tf,
                availability=IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
                reason=(
                    f"provider {self._provider_name} does not support "
                    f"timeframe {tf!r} for {canon}."
                ),
            )
        if not instrument_supported:
            return MtfTimeframeState(
                instrument=canon,
                timeframe=tf,
                availability=IntradayCoverageStatus.UNSUPPORTED_INSTRUMENT,
                reason=(
                    f"provider {self._provider_name} does not support "
                    f"instrument {canon!r}."
                ),
            )

        # ---- Exactly ONE provider fetch ----
        try:
            series = self.provider.fetch(canon, tf, reference_now=now)
        except Exception as exc:  # pragma: no cover - defensive
            return MtfTimeframeState(
                instrument=canon,
                timeframe=tf,
                availability=IntradayCoverageStatus.PROVIDER_ERROR,
                reason=f"provider raised: {type(exc).__name__}: {exc}",
            )

        # ---- Reuse the canonical 19.2 classification (single source) ----
        try:
            coverage = self.coverage._classify_series(  # noqa: SLF001
                canon, tf, series, now,
            )
        except Exception:  # pragma: no cover - defensive
            coverage = None

        if coverage is None:
            return MtfTimeframeState(
                instrument=canon,
                timeframe=tf,
                availability=IntradayCoverageStatus.INVALID_RESPONSE,
                reason="the 19.2 coverage classification could not be produced.",
            )

        # ---- Completed candles (SAME series, canonical boundary) ----
        completed, _forming = _validated_completed_candles(series, tf, now)

        # ---- Descriptive market state (Sprint 11P, point-in-time) ----
        market_state = None
        market_state_available = False
        state_reason = coverage.reason or ""
        if completed and len(completed) >= self.config.min_candles_per_timeframe:
            try:
                market_state = self._market_context_engine.analyze_at(
                    completed, len(completed) - 1,
                )
                market_state_available = market_state is not None
            except Exception:  # pragma: no cover - defensive
                market_state = None
                market_state_available = False
        elif completed:
            state_reason = (
                f"insufficient history for market-state classification "
                f"({len(completed)} completed candle(s) < "
                f"min_candles_per_timeframe={self.config.min_candles_per_timeframe})."
                + (f" {coverage.reason}" if coverage.reason else "")
            )
        elif coverage.reason:
            state_reason = coverage.reason

        return MtfTimeframeState(
            instrument=canon,
            timeframe=tf,
            availability=coverage.status,
            coverage=coverage,
            completed_candle_count=len(completed),
            latest_completed_timestamp=(
                completed[-1].timestamp if completed else None
            ),
            latest_completed_candle=completed[-1] if completed else None,
            market_state=market_state,
            direction=(
                self._direction_from_state(market_state)
                if market_state is not None
                else MTFDirection.UNKNOWN
            ),
            market_state_available=market_state_available,
            reason=state_reason,
        )

    # ------------------------------------------------------------
    # PER-SYMBOL + UNIVERSE ANALYSIS
    # ------------------------------------------------------------

    def analyze_symbol(
        self,
        instrument: str,
        *,
        reference_now: datetime | None = None,
    ) -> PerSymbolMtfResult:
        """
        Produce ONE instrument's multi-timeframe market-state result.

        Exactly one :class:`MtfTimeframeState` per CONFIGURED timeframe
        is produced (a frame is never silently dropped). A failure on
        one frame is classified and never terminates the symbol.
        """

        now = _require_aware(
            reference_now if reference_now is not None else datetime.now(UTC),
            "reference_now",
        )
        canon = _canonical_name(instrument)
        states = tuple(
            self.assess_timeframe(canon, tf, reference_now=now)
            for tf in self.config.timeframes
        )
        completeness = compute_completeness(states)
        alignment, reason = classify_alignment(states)
        return PerSymbolMtfResult(
            instrument=canon,
            timeframes=self.config.timeframes,
            timeframe_states=states,
            completeness=completeness,
            alignment=alignment,
            alignment_reason=reason,
            reference_now=now,
            market_session=market_session_state(now),
        )

    def analyze_universe(
        self,
        universe: UniverseDefinition | Sequence[str] | None = None,
        *,
        reference_now: datetime | None = None,
    ) -> MtfUniverseAnalysis:
        """
        Universe-wide MTF market-state analysis (deterministic,
        failure-isolated).

        ``universe`` may be a validated :class:`UniverseDefinition`
        (e.g. ``UniverseBuilder.nifty200()``) or a sequence of canonical
        names; when ``None`` the default NIFTY Top 200 definition is
        used. Every requested constituent gets EXACTLY ONE explicit
        per-symbol result (never silently dropped), ordered canonically.
        One symbol failing on one timeframe never terminates the run.
        """

        now = _require_aware(
            reference_now if reference_now is not None else datetime.now(UTC),
            "reference_now",
        )
        names = _resolve_universe_names(universe)
        results = tuple(
            self.analyze_symbol(n, reference_now=now) for n in names
        )

        tally = {
            "complete": 0, "incomplete": 0, "unsupported": 0, "unavailable": 0,
            "aligned": 0, "mixed": 0, "conflicting": 0,
            "alignment_incomplete": 0, "alignment_unavailable": 0,
        }
        for result in results:
            completeness = result.completeness
            if completeness is MarketStateCompleteness.COMPLETE:
                tally["complete"] += 1
            elif completeness is MarketStateCompleteness.INCOMPLETE:
                tally["incomplete"] += 1
            elif completeness is MarketStateCompleteness.UNSUPPORTED:
                tally["unsupported"] += 1
            else:
                tally["unavailable"] += 1
            alignment = result.alignment
            if alignment is MtfAlignmentState.ALIGNED:
                tally["aligned"] += 1
            elif alignment is MtfAlignmentState.MIXED:
                tally["mixed"] += 1
            elif alignment is MtfAlignmentState.CONFLICTING:
                tally["conflicting"] += 1
            elif alignment is MtfAlignmentState.INCOMPLETE:
                tally["alignment_incomplete"] += 1
            else:
                tally["alignment_unavailable"] += 1

        counts = MtfUniverseCounts(**tally)
        return MtfUniverseAnalysis(
            analysis_id=mtf_analysis_id(
                self.config.timeframes, names, now,
            ),
            reference_now=now,
            timeframes=self.config.timeframes,
            universe_instrument_count=len(names),
            instruments=names,
            results=results,
            counts=counts,
            market_session=market_session_state(now),
        )

    # ------------------------------------------------------------
    # INTERNALS (deterministic helpers)
    # ------------------------------------------------------------

    def _canonical_tf(self, timeframe: str) -> str | None:
        from engine.data.historical_times import canonical_timeframe

        return canonical_timeframe(timeframe)

    def _is_timeframe_supported(self, instrument: str, tf: str) -> bool:
        try:
            return bool(self.provider.is_timeframe_supported(tf))
        except Exception:  # pragma: no cover - defensive
            return False

    def _is_instrument_supported(self, instrument: str) -> bool:
        supports = getattr(self.provider, "supports_instrument", None)
        if supports is None:
            return True  # unknown superset (e.g. Yahoo pass-through)
        try:
            return bool(supports(instrument))
        except Exception:  # pragma: no cover - defensive
            return True

    @property
    def _provider_name(self) -> str:
        return getattr(self.provider, "data_source", "unknown")

    @staticmethod
    def _direction_from_state(market_state) -> MTFDirection:
        from engine.models.mtf_analysis import direction_from_trend

        if market_state is None:
            return MTFDirection.UNKNOWN
        return direction_from_trend(getattr(market_state, "trend", None))


class MtfAnalysisViewBuilder:
    """
    Thin, deterministic projection helpers for the operator CLI / JSON
    surface. PROJECTION ONLY — no market intelligence is recomputed.
    """

    @staticmethod
    def timeframe_state_to_jsonable(state: MtfTimeframeState) -> dict:
        market_state = state.market_state
        trend = market_state.trend if market_state is not None else None
        return {
            "timeframe": state.timeframe,
            "availability": state.availability.value,
            "completed_candle_count": state.completed_candle_count,
            "latest_completed_timestamp": (
                state.latest_completed_timestamp.isoformat()
                if state.latest_completed_timestamp
                else None
            ),
            "latest_close": (
                state.latest_completed_candle.close
                if state.latest_completed_candle is not None
                else None
            ),
            "market_state_available": state.market_state_available,
            "trend_state": (
                trend.state.value if trend is not None else None
            ),
            "range_state": (
                market_state.range.state.value
                if market_state is not None and market_state.range is not None
                else None
            ),
            "direction": state.direction.value,
            "fresh": state.is_current_fresh,
            "reason": state.reason or None,
        }

    @staticmethod
    def symbol_to_jsonable(result: PerSymbolMtfResult) -> dict:
        return {
            "instrument": result.instrument,
            "completeness": result.completeness.value,
            "alignment": result.alignment.value,
            "alignment_reason": result.alignment_reason,
            "timeframes": list(result.timeframes),
            "timeframe_states": [
                MtfAnalysisViewBuilder.timeframe_state_to_jsonable(s)
                for s in result.timeframe_states
            ],
        }

    @staticmethod
    def analysis_to_jsonable(analysis: MtfUniverseAnalysis) -> dict:
        counts = analysis.counts
        return {
            "analysis_id": analysis.analysis_id,
            "reference_now": analysis.reference_now.isoformat(),
            "timeframes": list(analysis.timeframes),
            "universe_instrument_count": analysis.universe_instrument_count,
            "counts": {
                "complete": counts.complete,
                "incomplete": counts.incomplete,
                "unsupported": counts.unsupported,
                "unavailable": counts.unavailable,
                "tested": counts.tested,
                "completeness_ratio": round(counts.completeness_ratio, 4),
                "aligned": counts.aligned,
                "mixed": counts.mixed,
                "conflicting": counts.conflicting,
                "alignment_incomplete": counts.alignment_incomplete,
                "alignment_unavailable": counts.alignment_unavailable,
                "aligned_ratio": round(counts.aligned_ratio, 4),
            },
            "market_session": (
                analysis.market_session.value
                if analysis.market_session is not None
                else None
            ),
            "results": [
                MtfAnalysisViewBuilder.symbol_to_jsonable(r)
                for r in analysis.results
            ],
        }


__all__ = [
    "MtfAnalysisEngine",
    "MtfAnalysisViewBuilder",
]