"""
Setup-quality intelligence engine (Checkpoint 19.5).

This module transforms the FROZEN 19.4 multi-timeframe market-state
result (:class:`engine.models.mtf_analysis.PerSymbolMtfResult` /
:class:`PerSymbolMtfResult`) into EXPLICIT, EXPLAINABLE, DETERMINISTIC
SETUP-QUALITY INTELLIGENCE:

    19.4 MTF MARKET STATE
            |
    SETUP CANDIDATE DETECTION   (reused Sprint 11O + 11Q, point-in-time)
            |
    SETUP QUALITY ASSESSMENT    (this engine)
            |
    QUALITY / CONFIDENCE CLASSIFICATION
            |
    CANDIDATE RANKING
            |
    19.6 SETUP LIFECYCLE (future)

The engine is STATELESS and PURE:

* It consumes the already-computed 19.4 ``PerSymbolMtfResult`` (which
  is itself derived ONLY from completed candles <= the reference
  instant) plus the caller-supplied COMPLETED primary-timeframe candles
  at that instant (the SAME candles the 19.4 state was computed from).
  It inspects no provider, no store and no future data.
* Setup DETECTION reuses the proven, point-in-time-safe Sprint 11O
  :class:`CandlePatternEngine` and Sprint 11Q
  :class:`SetupConfluenceEngine` VERBATIM — no new setup detection
  heuristic is invented.
* Setup QUALITY is a NEW, transparent, bounded integer score with
  documented components (see :class:`SetupQualityConfig` and the
  Checkpoint 19.5 audit document).
* Setup TYPE reuses the Sprint 11R :class:`SetupType` taxonomy with the
  SAME documented derivation rules (BREAKOUT / TREND_CONTINUATION /
  STRUCTURE_CONTINUATION / SETUP_CANDIDATE) WITHOUT computing any
  entry / stop / target / risk-reward geometry (those are downstream
  trade-plan concepts, deliberately out of scope here).

The engine implements NO trading logic:

* NO trade execution, NO broker order placement, NO portfolio/position
  management, NO automated entries/exits, NO user notifications, NO
  setup lifecycle/state persistence, NO forward-testing infrastructure,
  NO reliability/watchdog framework.
* A QUALIFIED setup is a coherent candidate worth further evaluation —
  never a BUY/SELL verdict, never a prediction, never a guarantee of
  profitability.

POINT-IN-TIME / NO-LOOK-AHEAD (STRUCTURAL):

* The 19.4 result is a function of completed candles <= ``T`` only.
* The caller supplies the completed primary-timeframe candles at ``T``
  (completed-candle boundary applied upstream); the engine never reads
  a forming/future candle itself.
* Candle patterns are attributed to the LAST index of the supplied
  completed candles only; the reused 11O/11Q engines are themselves
  historical-safe (patterns at index ``T`` depend only on
  ``candles[T-1]`` / ``candles[T]``; the 11Q assessment reads only the
  already-computed ``MarketContext`` + patterns).
* Tests prove: appending arbitrary future candles (including a future
  breakout / BOS / higher-timeframe confirmation / support-resistance
  event) does NOT change an already-computed setup-quality result at an
  earlier ``T``.
"""
from __future__ import annotations

from datetime import datetime
from typing import Mapping, Sequence

from engine.config.setup_quality_config import SetupQualityConfig
from engine.intelligence.candle_patterns import CandlePatternEngine
from engine.intelligence.setup_confluence import SetupConfluenceEngine
from engine.models.intraday_coverage import IntradayCoverageStatus
from engine.models.market_context import (
    MarketContext,
    PriceLocation,
)
from engine.models.mtf_analysis import (
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfTimeframeState,
    PerSymbolMtfResult,
)
from engine.models.ohlcv import OHLCVCandle
from engine.models.setup_confluence import (
    EvidenceAlignment,
    SetupClassification,
    SetupDirection,
)
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityCounts,
    SetupQualityResult,
    SetupQualityScoreComponent,
    SetupQualityStatus,
    SetupQualityUniverseResult,
    setup_quality_analysis_id,
)
from engine.models.trade_candidate import SetupType


def _canonical_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )
    return value


def _primary_timeframe_for(
    mtf: "PerSymbolMtfResult",
    config: SetupQualityConfig,
) -> str:
    """Resolve the primary (setup) timeframe deterministically.

    When ``config.primary_timeframe`` is set it must be an element of
    the 19.4 configured timeframes. Otherwise the LOWEST-DURATION
    timeframe is used (the 19.4 presentation order is ascending
    duration, so this is ``mtf.timeframes[0]``).
    """

    if config.primary_timeframe is not None:
        if config.primary_timeframe not in mtf.timeframes:
            raise ValueError(
                f"configured primary_timeframe "
                f"{config.primary_timeframe!r} is not one of the mtf "
                f"configured timeframes {list(mtf.timeframes)!r}.",
            )
        return config.primary_timeframe
    if not mtf.timeframes:
        raise ValueError("mtf result carries no configured timeframes.")
    return mtf.timeframes[0]


def _direction_from_assessment(direction) -> SetupDirection | None:
    """Deterministic 11Q direction normalization (str or enum)."""
    if direction is None:
        return None
    if isinstance(direction, SetupDirection):
        return direction
    try:
        return SetupDirection(str(direction))
    except ValueError:
        return None


#: Status -> tally key (deterministic).
_STATUS_KEY = {
    SetupQualityStatus.QUALIFIED: "qualified",
    SetupQualityStatus.WATCH: "watch",
    SetupQualityStatus.NO_SETUP: "no_setup",
    SetupQualityStatus.INCOMPLETE: "incomplete",
    SetupQualityStatus.UNAVAILABLE: "unavailable",
}

#: Classification -> tally key (deterministic).
_CLASSIFICATION_KEY = {
    SetupQualityClassification.EXCELLENT: "excellent",
    SetupQualityClassification.HIGH: "high",
    SetupQualityClassification.MEDIUM: "medium",
    SetupQualityClassification.LOW: "low",
    SetupQualityClassification.REJECTED: "rejected",
    SetupQualityClassification.INCOMPLETE: "incomplete",
    SetupQualityClassification.UNAVAILABLE: "unavailable",
}


class SetupQualityEngine:
    """
    Deterministic setup-quality intelligence engine (Checkpoint 19.5).

    Public API:

        evaluate(mtf_result, primary_candles, ...) -> SetupQualityResult
            ONE symbol's setup-quality assessment at the result's
            reference instant.

        evaluate_universe(analysis, primary_candles_by_instrument, ...)
            -> SetupQualityUniverseResult
            Universe-wide setup-quality analysis (deterministic,
            failure-isolated, ranked).

    The engine is stateless across calls: identical inputs always
    produce identical outputs.
    """

    def __init__(
        self,
        config: SetupQualityConfig | None = None,
        *,
        pattern_engine: CandlePatternEngine | None = None,
        confluence_engine: SetupConfluenceEngine | None = None,
    ) -> None:
        self.config = config or SetupQualityConfig()
        self._pattern_engine = pattern_engine or CandlePatternEngine()
        self._confluence_engine = confluence_engine or SetupConfluenceEngine()

    # ============================================================
    # PUBLIC API
    # ============================================================

    def evaluate(
        self,
        mtf_result,
        primary_candles: Sequence[OHLCVCandle] | None = None,
        *,
        label: str = "",
        metadata: Sequence[tuple[str, str]] | None = None,
    ) -> SetupQualityResult:
        """
        Produce ONE instrument's setup-quality assessment.

        ``mtf_result`` is the already-computed 19.4
        :class:`PerSymbolMtfResult`; ``primary_candles`` are the
        COMPLETED primary-timeframe candles at the reference instant
        (the pipeline fetches them through the same canonical
        19.2/19.4 boundary). The result is a pure deterministic
        function of these inputs; the mtf result is never modified.

        ``label`` / ``metadata`` are descriptive run identity
        (presentation/provenance only; NOT part of the scoring).
        """

        del label, metadata  # presentation-only; scoring is input-driven
        reference_now = _require_aware(
            mtf_result.reference_now, "mtf_result.reference_now",
        )
        primary = _primary_timeframe_for(mtf_result, self.config)
        primary_state = mtf_result.state_for(primary)
        if primary_state is None:
            raise ValueError(
                f"mtf result has no state for primary timeframe {primary!r}.",
            )

        # ---- Data gate (19.2/19.4 vocabulary) ---------------------
        data_state = self._data_gate(mtf_result, primary_state)
        if data_state is not None:
            return data_state

        # ---- Setup detection (reused 11O + 11Q, point-in-time) ----
        candles = list(primary_candles or ())
        market_state = primary_state.market_state
        assessment, patterns_label = self._detect_setup(
            candles, market_state,
            reference_now,
        )

        # ---- Quality score ---------------------------------------
        components = self._score_components(
            mtf_result, primary_state, assessment,
        )
        total = sum(c.points for c in components)
        total = max(0, min(self.config.max_score, int(total)))

        # ---- Classification (band + negative-evidence caps) -------
        classification = self._classify(
            mtf_result, primary_state, assessment, total,
        )

        # ---- Status -----------------------------------------------
        status = self._status(assessment, classification)

        # ---- Setup type (reused 11R taxonomy, WITHOUT geometry) ----
        setup_type = self._setup_type(assessment, market_state)

        positive, negative = self._factors(
            mtf_result, primary_state, assessment, classification,
        )
        setup_direction = _direction_from_assessment(assessment.direction)

        explanation = self._explanation(
            mtf_result, assessment, total, classification,
            components, negative,
        )

        reason = self._reason(status, assessment, classification)

        return SetupQualityResult(
            instrument=mtf_result.instrument,
            reference_now=reference_now,
            mtf=mtf_result,
            primary_timeframe=primary,
            status=status,
            classification=SetupQualityClassification(classification),
            score=total,
            score_components=tuple(components),
            setup_type=(
                setup_type.name if setup_type is not None else None
            ),
            setup_direction=setup_direction,
            setup_classification=assessment.classification,
            confluence_score=assessment.confluence_score,
            has_conflict=assessment.has_conflict,
            positive_factors=tuple(positive),
            negative_factors=tuple(negative),
            data_complete=(
                mtf_result.completeness is MarketStateCompleteness.COMPLETE
            ),
            stale_data=bool(
                primary_state.availability is IntradayCoverageStatus.STALE
            ),
            explanation=explanation,
            reason=reason,
        )

    def evaluate_universe(
        self,
        analysis,
        primary_candles_by_instrument: Mapping[str, Sequence[OHLCVCandle]]
        | None = None,
        *,
        label: str = "",
        metadata: Sequence[tuple[str, str]] | None = None,
    ) -> SetupQualityUniverseResult:
        """
        Universe-wide setup-quality analysis (deterministic,
        failure-isolated).

        ``analysis`` is the already-computed 19.4
        :class:`MtfUniverseAnalysis`. ``primary_candles_by_instrument``
        maps canonical instrument -> completed primary-timeframe candles
        (missing keys are treated as no candles: the data gate decides
        UNAVAILABLE/INCOMPLETE from the 19.4 result — never a setup).

        Every requested constituent gets EXACTLY ONE explicit result;
        one symbol's failure never terminates the run; QUALIFIED
        candidates are ranked deterministically (strongest-first).
        """

        del label, metadata
        reference_now = _require_aware(
            analysis.reference_now, "analysis.reference_now",
        )
        candles_map: dict[str, Sequence[OHLCVCandle]] = dict(
            primary_candles_by_instrument or {},
        )
        results = tuple(
            self.evaluate(
                analysis.result_for(instrument),
                candles_map.get(instrument) or (),
            )
            for instrument in analysis.instruments
        )

        tally = {
            "qualified": 0, "watch": 0, "no_setup": 0,
            "incomplete": 0, "unavailable": 0,
            "excellent": 0, "high": 0, "medium": 0,
            "low": 0, "rejected": 0,
        }
        for r in results:
            tally[_STATUS_KEY[r.status]] += 1
            tally[_CLASSIFICATION_KEY[r.classification]] += 1
        counts = SetupQualityCounts(
            qualified=tally["qualified"],
            watch=tally["watch"],
            no_setup=tally["no_setup"],
            incomplete=tally["incomplete"],
            unavailable=tally["unavailable"],
            excellent=tally["excellent"],
            high=tally["high"],
            medium=tally["medium"],
            low=tally["low"],
            rejected=tally["rejected"],
        )

        ranked = tuple(
            sorted(
                (r for r in results if r.is_qualified),
                key=self._ranking_key,
            ),
        )
        max_q = self.config.max_qualified
        top_n = ranked if max_q is None else ranked[:max_q]

        return SetupQualityUniverseResult(
            analysis_id=setup_quality_analysis_id(
                analysis.timeframes,
                analysis.instruments,
                reference_now,
                self.config.snapshot(),
            ),
            reference_now=reference_now,
            timeframes=analysis.timeframes,
            primary_timeframe=(
                self.config.primary_timeframe
                or (
                    analysis.timeframes[0]
                    if analysis.timeframes else ""
                )
            ),
            universe_instrument_count=analysis.universe_instrument_count,
            instruments=analysis.instruments,
            results=results,
            counts=counts,
            ranked_qualified=ranked,
            top_n=top_n,
            max_qualified=max_q,
        )

    # ============================================================
    # DATA GATE (19.2 / 19.4 vocabulary)
    # ============================================================

    def _data_gate(
        self,
        mtf_result,
        primary_state: MtfTimeframeState,
    ) -> SetupQualityResult | None:
        """
        Deterministic data-quality gate.

        Returns a data-gate ``SetupQualityResult`` (INCOMPLETE /
        UNAVAILABLE) when the data cannot support a setup evaluation;
        ``None`` when the setup may be evaluated.

        Rules (never convert missing evidence into positive evidence):

        * 19.4 completeness UNAVAILABLE or no usable primary state
          -> UNAVAILABLE (nothing observable).
        * 19.4 completeness UNSUPPORTED -> INCOMPLETE (the full
          relation cannot be established; primary frame unsupported).
        * primary state missing / not usable / no derived market state
          -> INCOMPLETE (cannot detect a setup on the primary frame).
        * otherwise -> ``None`` (proceed; staleness is a cap, the MTF
          INCOMPLETE completeness is a cap, both applied in
          :meth:`_classify`).
        """

        if mtf_result.completeness is MarketStateCompleteness.UNAVAILABLE:
            return SetupQualityResult(
                instrument=mtf_result.instrument,
                reference_now=mtf_result.reference_now,
                mtf=mtf_result,
                primary_timeframe=primary_state.timeframe,
                status=SetupQualityStatus.UNAVAILABLE,
                classification=SetupQualityClassification.UNAVAILABLE,
                data_complete=False,
                reason=(
                    "no usable per-timeframe data exists — setup quality "
                    "is UNAVAILABLE (missing evidence is never "
                    "fabricated)."
                ),
            )
        if mtf_result.completeness is MarketStateCompleteness.UNSUPPORTED:
            return SetupQualityResult(
                instrument=mtf_result.instrument,
                reference_now=mtf_result.reference_now,
                mtf=mtf_result,
                primary_timeframe=primary_state.timeframe,
                status=SetupQualityStatus.INCOMPLETE,
                classification=SetupQualityClassification.INCOMPLETE,
                data_complete=False,
                reason=(
                    "every configured timeframe is unsupported by the "
                    "provider — setup quality is INCOMPLETE (nothing "
                    "can be established)."
                ),
            )
        if (
            primary_state.market_state is None
            or not primary_state.market_state_available
            or not primary_state.has_usable_data
            or primary_state.availability
            in (
                IntradayCoverageStatus.NO_DATA,
                IntradayCoverageStatus.EMPTY,
                IntradayCoverageStatus.PROVIDER_ERROR,
                IntradayCoverageStatus.INVALID_RESPONSE,
                IntradayCoverageStatus.TEMPORARILY_UNAVAILABLE,
                IntradayCoverageStatus.UNSUPPORTED_INSTRUMENT,
                IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
            )
        ):
            return SetupQualityResult(
                instrument=mtf_result.instrument,
                reference_now=mtf_result.reference_now,
                mtf=mtf_result,
                primary_timeframe=primary_state.timeframe,
                status=SetupQualityStatus.INCOMPLETE,
                classification=SetupQualityClassification.INCOMPLETE,
                data_complete=(
                    mtf_result.completeness is MarketStateCompleteness.COMPLETE
                ),
                reason=(
                    f"primary timeframe {primary_state.timeframe!r} "
                    "carries no usable market state "
                    f"({primary_state.availability.value}) — setup "
                    "quality is INCOMPLETE, never a setup."
                ),
            )
        return None

    # ============================================================
    # SETUP DETECTION (reused 11O + 11Q)
    # ============================================================

    def _detect_setup(
        self,
        candles: Sequence[OHLCVCandle],
        market_context: MarketContext | None,
        reference_now: datetime,
    ):
        """
        Detect a setup candidate with the REUSED Sprint 11O + 11Q
        engines (point-in-time safe by construction).

        Returns ``(assessment, patterns_label)``. ``patterns_label``
        is the short human-readable candle-pattern label for the
        triggering index (e.g. ``"HAMMER"``), or ``""`` when none.
        """

        patterns_label = ""
        assessment = None
        if market_context is not None and candles:
            try:
                patterns = self._pattern_engine.detect(candles)
                at_t = tuple(
                    p for p in patterns if p.index == len(candles) - 1
                )
                if at_t:
                    patterns_label = ", ".join(
                        sorted({p.pattern_type.value for p in at_t}),
                    )
                assessment = self._confluence_engine.assess(
                    at_t,
                    market_context,
                    index=len(candles) - 1,
                    timestamp=candles[-1].timestamp,
                )
            except Exception:  # pragma: no cover - defensive
                assessment = None
        if assessment is None:
            assessment = self._confluence_engine.assess(
                (),
                market_context,
                index=-1,
                timestamp=reference_now,
            )
            if market_context is None:
                assessment = self._confluence_engine.assess(
                    (), None, index=-1, timestamp=reference_now,
                )
        return assessment, patterns_label

    # ============================================================
    # QUALITY SCORING (documented components; sum = 100)
    # ============================================================

    def _score_components(
        self,
        mtf_result,
        primary_state: MtfTimeframeState,
        assessment,
    ) -> list[SetupQualityScoreComponent]:
        """Compute the four documented, non-overlapping components."""

        cfg = self.config
        components: list[SetupQualityScoreComponent] = []

        # ---- MTF alignment (30) -----------------------------------
        aligns, reason = self._mtf_alignment_points(mtf_result)
        components.append(SetupQualityScoreComponent(
            name="mtf_alignment",
            points=aligns,
            max_points=cfg.mtf_alignment_weight,
            reason=reason,
        ))

        # ---- Setup confirmation (30) ------------------------------
        confirms, reason2 = self._setup_confirmation_points(assessment)
        components.append(SetupQualityScoreComponent(
            name="setup_confirmation",
            points=confirms,
            max_points=cfg.setup_confirmation_weight,
            reason=reason2,
        ))

        # ---- Setup confluence (25) --------------------------------
        conf, reason3 = self._setup_confluence_points(assessment)
        components.append(SetupQualityScoreComponent(
            name="setup_confluence",
            points=conf,
            max_points=cfg.setup_confluence_weight,
            reason=reason3,
        ))

        # ---- Structure quality (15) -------------------------------
        struct, reason4 = self._structure_quality_points(primary_state)
        components.append(SetupQualityScoreComponent(
            name="structure_quality",
            points=struct,
            max_points=cfg.structure_quality_weight,
            reason=reason4,
        ))

        return components

    def _mtf_alignment_points(
        self,
        mtf_result,
    ) -> tuple[int, str]:
        """MTF relationship quality (reused 19.4 ``MtfAlignmentState``)."""

        w = self.config.mtf_alignment_weight
        alignment = mtf_result.alignment
        if alignment is MtfAlignmentState.ALIGNED:
            return w, (
                "MTF ALIGNED: all usable timeframes expose the same "
                "directional market state."
            )
        if alignment is MtfAlignmentState.MIXED:
            return int(round(w * self.config.partial_fraction)), (
                "MTF MIXED: timeframes expose a blend of directional and "
                "non-directional states; partial credit only."
            )
        if alignment is MtfAlignmentState.CONFLICTING:
            return 0, (
                "MTF CONFLICTING: opposing directional market states "
                "across the configured timeframes; zero credit (conflict "
                "is never positive evidence)."
            )
        if alignment is MtfAlignmentState.INCOMPLETE:
            return 0, (
                "MTF INCOMPLETE: a configured timeframe could not "
                "establish a usable market state; zero credit (missing "
                "evidence is never positive evidence)."
            )
        return 0, (
            "MTF UNAVAILABLE: nothing observable to relate; zero credit."
        )

    def _setup_confirmation_points(
        self,
        assessment,
    ) -> tuple[int, str]:
        """Single-frame setup confirmation (reused 11Q classification)."""

        w = self.config.setup_confirmation_weight
        classification = assessment.classification
        if classification is SetupClassification.POTENTIAL_SETUP:
            return w, (
                "Setup confirmation POTENTIAL_SETUP: multiple independent "
                "evidence sources agree with a clear directional bias and "
                "no disqualifying conflict."
            )
        if classification is SetupClassification.WATCH:
            return int(round(w * self.config.partial_fraction)), (
                "Setup confirmation WATCH: directional evidence exists "
                "but the confluence is not strong enough to form a "
                "coherent candidate setup; partial credit only."
            )
        return 0, (
            "Setup confirmation NO_SETUP: insufficient / conflicting / "
            "non-aligned evidence; zero credit."
        )

    def _setup_confluence_points(
        self,
        assessment,
    ) -> tuple[int, str]:
        """Granular aligned-evidence count (reused 11Q confluence)."""

        w = self.config.setup_confluence_weight
        confluence = assessment.confluence_score
        if confluence <= 0:
            return 0, "Confluence score is 0; zero credit."
        points = int(round(confluence / 5.0 * w))
        return max(0, min(w, points)), (
            f"Confluence of {confluence}/5 aligned independent evidence "
            "sources; projected onto the weight."
        )

    def _structure_quality_points(
        self,
        primary_state: MtfTimeframeState,
    ) -> tuple[int, str]:
        """Structural-foundation robustness (reused Sprint 11P context)."""

        w = self.config.structure_quality_weight
        market_state = primary_state.market_state
        if market_state is None:
            return 0, "No market state; zero credit."
        trend = market_state.trend
        intact = getattr(trend, "structure_intact", False) if trend is not None else False
        confirmed = getattr(market_state, "confirmed_swings", 0) or 0
        if intact and confirmed >= 2:
            return w, (
                f"Structure intact with {confirmed} confirmed swings; "
                "full foundation credit."
            )
        if confirmed >= 1:
            return int(round(w * self.config.partial_fraction)), (
                f"Structure present ({confirmed} confirmed swing(s)) but "
                "not fully intact; partial credit only."
            )
        return 0, "No confirmed structure; zero credit."

    # ============================================================
    # CLASSIFICATION (band + negative-evidence / data-quality caps)
    # ============================================================

    def _classify(
        self,
        mtf_result,
        primary_state: MtfTimeframeState,
        assessment,
        score: int,
    ) -> str:
        """
        Deterministic classification from the score + caps.

        Base band from :meth:`SetupQualityConfig.classification_for_score`,
        then apply caps IN ORDER (each can only REDUCE):

        1. NO_SETUP detection (11Q) -> ``no_setup_classification``.
        2. WATCH detection (11Q) -> at most ``watch_max_classification``.
        3. Conflicting evidence (11Q ``has_conflict``) -> at most
           ``conflict_max_classification``.
        4. Stale primary data (19.2 STALE) -> at most
           ``stale_data_max_classification``.
        5. Incomplete MTF (19.4 INCOMPLETE) -> at most
           ``incomplete_mtf_max_classification``.
        """

        if assessment.classification is SetupClassification.NO_SETUP:
            return self.config.no_setup_classification
        if assessment.classification is SetupClassification.WATCH:
            return self._cap(
                self.config.classification_for_score(score),
                self.config.watch_max_classification,
            )
        if assessment.classification is not SetupClassification.POTENTIAL_SETUP:
            return self.config.no_setup_classification

        band = self.config.classification_for_score(score)
        if assessment.has_conflict:
            band = self._cap(band, self.config.conflict_max_classification)
        if primary_state.availability is IntradayCoverageStatus.STALE:
            band = self._cap(band, self.config.stale_data_max_classification)
        if mtf_result.completeness is MarketStateCompleteness.INCOMPLETE:
            band = self._cap(
                band, self.config.incomplete_mtf_max_classification,
            )
        return band

    @staticmethod
    def _cap(current: str, cap: str) -> str:
        """Cap a classification at another (each can only reduce)."""

        order = {
            "REJECTED": 0, "LOW": 1, "MEDIUM": 2,
            "HIGH": 3, "EXCELLENT": 4,
        }
        if order[current] > order[cap]:
            return cap
        return current

    # ============================================================
    # STATUS
    # ============================================================

    def _status(
        self,
        assessment,
        classification: str,
    ) -> SetupQualityStatus:
        """Deterministic evaluation state from the detection + band."""

        if assessment.classification is SetupClassification.POTENTIAL_SETUP:
            return SetupQualityStatus.QUALIFIED
        if assessment.classification is SetupClassification.WATCH:
            return SetupQualityStatus.WATCH
        return SetupQualityStatus.NO_SETUP

    # ============================================================
    # SETUP TYPE (reused 11R taxonomy + rules, WITHOUT geometry)
    # ============================================================

    def _setup_type(
        self,
        assessment,
        market_context: MarketContext | None,
    ) -> SetupType | None:
        """
        Conservative setup-type classification mirroring the Sprint 11R
        derivation rules (BREAKOUT / TREND_CONTINUATION /
        STRUCTURE_CONTINUATION / SETUP_CANDIDATE), WITHOUT computing any
        entry / stop / target geometry.

        Only types justifiable from the existing 11P + 11Q evidence are
        returned; ``SETUP_CANDIDATE`` is the conservative generic
        fallback.
        """

        direction = assessment.direction
        if direction not in (SetupDirection.BULLISH, SetupDirection.BEARISH):
            return None
        if market_context is None:
            return SetupType.SETUP_CANDIDATE

        location = market_context.support_resistance.location
        trend_aligned = (
            assessment.evidence.trend.alignment is EvidenceAlignment.ALIGNED
        )

        if direction is SetupDirection.BULLISH:
            if location is PriceLocation.ABOVE_RESISTANCE:
                return SetupType.BREAKOUT
            if location is PriceLocation.NEAR_SUPPORT:
                if trend_aligned:
                    return SetupType.TREND_CONTINUATION
                return SetupType.STRUCTURE_CONTINUATION
            return SetupType.SETUP_CANDIDATE

        if direction is SetupDirection.BEARISH:
            if location is PriceLocation.BELOW_SUPPORT:
                return SetupType.BREAKOUT
            if location is PriceLocation.NEAR_RESISTANCE:
                if trend_aligned:
                    return SetupType.TREND_CONTINUATION
                return SetupType.STRUCTURE_CONTINUATION
            return SetupType.SETUP_CANDIDATE

        return SetupType.SETUP_CANDIDATE

    # ============================================================
    # EXPLAINABILITY / FACTORS
    # ============================================================

    def _factors(
        self,
        mtf_result,
        primary_state: MtfTimeframeState,
        assessment,
        classification: str,
    ) -> tuple[list[str], list[str]]:
        """Deterministic positive / negative evidence labels."""

        positive: list[str] = []
        negative: list[str] = []

        alignment = mtf_result.alignment
        if alignment is MtfAlignmentState.ALIGNED:
            positive.append("MTF_ALIGNED")
        elif alignment is MtfAlignmentState.MIXED:
            positive.append("MTF_MIXED_PARTIAL")
            negative.append("MTF_MIXED")
        elif alignment is MtfAlignmentState.CONFLICTING:
            negative.append("MTF_CONFLICTING")

        if mtf_result.completeness is MarketStateCompleteness.INCOMPLETE:
            negative.append("MTF_INCOMPLETE")

        if assessment.classification is SetupClassification.POTENTIAL_SETUP:
            positive.append("SETUP_POTENTIAL")
        elif assessment.classification is SetupClassification.WATCH:
            positive.append("SETUP_WATCH")
            negative.append("SETUP_BELOW_QUALIFICATION_BAR")
        elif assessment.classification is SetupClassification.NO_SETUP:
            negative.append("NO_SETUP_DETECTED")

        if assessment.confluence_score and assessment.confluence_score > 0:
            positive.append(f"CONFLUENCE_{assessment.confluence_score}/5")

        if assessment.has_conflict:
            negative.append("CONFLICTING_EVIDENCE")

        if primary_state.availability is IntradayCoverageStatus.STALE:
            negative.append("STALE_PRIMARY_DATA")

        market_state = primary_state.market_state
        if market_state is not None:
            trend = market_state.trend
            intact = getattr(trend, "structure_intact", False) if trend is not None else False
            confirmed = getattr(market_state, "confirmed_swings", 0) or 0
            if intact and confirmed >= 2:
                positive.append(f"STRUCTURE_INTACT_{confirmed}_SWINGS")
            elif confirmed >= 1:
                positive.append(f"STRUCTURE_PARTIAL_{confirmed}_SWINGS")

        return positive, negative

    def _explanation(
        self,
        mtf_result,
        assessment,
        score: int,
        classification: str,
        components,
        negative: list[str],
    ) -> str:
        """Human-readable explanation generated from the ACTUAL inputs."""

        parts = [
            f"MTF {mtf_result.alignment.value} "
            f"({mtf_result.completeness.value});",
            f"setup {assessment.classification.value} with confluence "
            f"{assessment.confluence_score}/5;",
            f"score {score}/100 -> {classification}",
        ]
        if negative:
            parts.append("negative evidence: " + "; ".join(sorted(negative)) + ".")
        else:
            parts.append("no negative evidence.")
        return " ".join(parts)

    def _reason(
        self,
        status: SetupQualityStatus,
        assessment,
        classification: str,
    ) -> str:
        if status is SetupQualityStatus.QUALIFIED:
            return (
                "QUALIFIED: a coherent setup candidate was detected "
                f"({assessment.classification.value}) and passed the "
                f"quality bar ({classification}). Descriptive only — "
                "not a trade signal or prediction."
            )
        if status is SetupQualityStatus.WATCH:
            return (
                "WATCH: directional evidence exists but the setup is "
                "below the qualification bar (11Q WATCH)."
            )
        return (
            "NO_SETUP: the current market state does not form a coherent "
            "setup candidate (11Q NO_SETUP)."
        )

    # ============================================================
    # RANKING (deterministic, strongest-first)
    # ============================================================

    def _ranking_key(self, result: SetupQualityResult):
        """
        Deterministic ranking key (strongest-first).

        Ties are broken by the next key:

        1. classification rank desc (EXCELLENT > HIGH > MEDIUM > LOW);
        2. score desc;
        3. confluence desc (higher confluence first; absent last);
        4. data completeness (COMPLETE first);
        5. fewer negative factors;
        6. instrument name ascending (final deterministic tie-break —
           direction is deliberately NOT a ranking key, matching the
           19.4 convention).

        Only QUALIFIED results enter the ranking; the key is a tuple so
        Python's stable sort is deterministic.
        """

        return (
            -result.classification.rank_value,
            -(result.score if result.score is not None else 0),
            -(result.confluence_score if result.confluence_score is not None else -1),
            -int(result.data_complete),
            len(result.negative_factors),
            result.instrument,
        )


class SetupQualityViewBuilder:
    """
    Thin, deterministic projection helpers for the operator CLI / JSON
    surface. PROJECTION ONLY — no intelligence is recomputed.
    """

    @staticmethod
    def result_to_jsonable(result: SetupQualityResult) -> dict:
        mtf = result.mtf
        return {
            "instrument": result.instrument,
            "status": result.status.value,
            "classification": result.classification.value,
            "score": result.score,
            "score_components": [
                {
                    "name": c.name,
                    "points": c.points,
                    "max_points": c.max_points,
                    "reason": c.reason,
                }
                for c in result.score_components
            ],
            "setup_type": result.setup_type,
            "setup_direction": (
                result.setup_direction.value
                if result.setup_direction is not None
                else None
            ),
            "setup_classification": (
                result.setup_classification.value
                if result.setup_classification is not None
                else None
            ),
            "confluence_score": result.confluence_score,
            "has_conflict": result.has_conflict,
            "positive_factors": list(result.positive_factors),
            "negative_factors": list(result.negative_factors),
            "data_complete": result.data_complete,
            "stale_data": result.stale_data,
            "primary_timeframe": result.primary_timeframe,
            "mtf_alignment": mtf.alignment.value,
            "mtf_completeness": mtf.completeness.value,
            "explanation": result.explanation,
            "reason": result.reason,
        }

    @staticmethod
    def universe_to_jsonable(result: SetupQualityUniverseResult) -> dict:
        counts = result.counts
        return {
            "analysis_id": result.analysis_id,
            "reference_now": result.reference_now.isoformat(),
            "timeframes": list(result.timeframes),
            "primary_timeframe": result.primary_timeframe,
            "universe_instrument_count": result.universe_instrument_count,
            "max_qualified": result.max_qualified,
            "counts": {
                "qualified": counts.qualified,
                "watch": counts.watch,
                "no_setup": counts.no_setup,
                "incomplete": counts.incomplete,
                "unavailable": counts.unavailable,
                "tested": counts.tested,
                "qualified_ratio": round(counts.qualified_ratio, 4),
                "excellent": counts.excellent,
                "high": counts.high,
                "medium": counts.medium,
                "low": counts.low,
                "rejected": counts.rejected,
            },
            "results": [
                SetupQualityViewBuilder.result_to_jsonable(r)
                for r in result.results
            ],
            "ranked_qualified": [
                SetupQualityViewBuilder.result_to_jsonable(r)
                for r in result.ranked_qualified
            ],
            "top_n": [
                SetupQualityViewBuilder.result_to_jsonable(r)
                for r in result.top_n
            ],
        }


__all__ = [
    "SetupQualityEngine",
    "SetupQualityViewBuilder",
]