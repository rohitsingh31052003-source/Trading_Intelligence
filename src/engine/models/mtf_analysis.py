"""
Multi-timeframe market-state analysis models (Checkpoint 19.4).

These models describe the deterministic MULTI-TIMEFRAME MARKET-STATE
layer that answers, at one reference instant and for one universe:

    "What does the current market state of this instrument look like
     across the configured timeframes, and are those timeframe states
     aligned, mixed, or conflicting?"

They are MARKET-STATE / DATA-STRUCTURE models ONLY:

* They carry NO setup detection, NO setup scoring, NO setup quality,
  NO trade ranking, NO entry signals, NO buy/sell decisions, NO
  entry/stop/target, NO risk/reward, NO trade plans, NO setup lifecycle,
  NO alerts, and NO broker/execution semantics.
* Every requested timeframe has an EXPLICIT per-timeframe
  :class:`~engine.models.intraday_coverage.IntradayCoverageStatus`
  availability result (missing / stale / unsupported / error are
  preserved — never silently dropped).
* The per-timeframe availability vocabulary REUSES the Checkpoint 19.2
  :class:`engine.models.intraday_coverage.IntradayCoverageStatus`
  taxonomy verbatim (no parallel status set is invented).
* The normalized per-timeframe MARKET STATE reuses the Sprint 11P
  :class:`engine.models.market_context.MarketContext` (structure /
  descriptive trend / range / support-resistance) BY REFERENCE — the
  Sprint 11P engine is point-in-time safe and carries no trade
  semantics, so it is reused rather than rewritten.
* The ``ALIGNED / MIXED / CONFLICTING / INCOMPLETE / UNAVAILABLE``
  classification describes TIMEFRAME RELATIONSHIPS ONLY. It NEVER
  produces a BUY / SELL / TRADE / NO-TRADE verdict.

TIMEFRAME-ALIGNMENT RULE (documented + deterministic):

Different timeframes do not close at the same instant. The MTF layer
therefore aligns timeframe states by their OWN latest COMPLETED candle
at the same reference instant ``T`` (completed-candle boundary), and
NEVER requires identical candle timestamps. A relation is only ever
computed from states whose data was available at ``T``; future candles
(including future higher-timeframe candles) are structurally excluded
by the reused completed-candle boundary.

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses.
* Optional fields use ``None`` so "unobserved" / "unavailable" is never
  silently a real value.
* ``__post_init__`` performs structural validation only; the alignment
  classifier is a pure, exported function (:func:`classify_alignment`),
  mirroring the 19.3 ``cycle_id`` placement in ``continuous_scan.py``.
* No wall-clock dependence: timestamps are explicit, caller-supplied,
  timezone-aware values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Sequence

from engine.models.intraday_coverage import (
    IntradayCoverageStatus,
    IntradayInstrumentCoverage,
)
from engine.models.market_context import (
    MarketContext,
    MarketTrend,
    MarketTrendState,
)
from engine.models.ohlcv import OHLCVCandle


def _canonical_timeframe(timeframe: str) -> str | None:
    """Canonical timeframe lookup via the engine canonical utility.

    Imported lazily so the model layer keeps zero ``engine.data``
    module-level imports (matching the repository convention) while
    still validating/spelling timeframes through the SINGLE canonical
    vocabulary.
    """

    from engine.data.historical_times import (
        canonical_timeframe as _canonical_timeframe_impl,
    )

    return _canonical_timeframe_impl(timeframe)


def _sha256_prefix(payload: str, prefix: str) -> str:
    """Deterministic ``"<prefix>" + sha256[:16]`` over a canonical string."""

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def _canonical_name(name: str) -> str:
    """Canonicalize + validate a single instrument name."""

    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


class MTFDirection(Enum):
    """
    Descriptive per-timeframe directional market-state disposition.

    This is a MARKET-STATE relationship token — NOT a BUY / SELL signal
    and NOT a trade direction. It is derived ONLY from the reused
    Sprint 11P descriptive trend classification.

    BULLISH
        The timeframe's descriptive market structure is intact and
        bullish (Sprint 11P ``MarketTrendState.BULLISH``).

    BEARISH
        The timeframe's descriptive market structure is intact and
        bearish (Sprint 11P ``MarketTrendState.BEARISH``).

    NEUTRAL
        The timeframe is observed but non-directional: descriptive
        trend is ``RANGE`` or ``NEUTRAL`` (a sideways market is never
        silently interpreted as bullish or bearish).

    RANGE
        The timeframe is observed in a descriptive RANGE state
        (Sprint 11P ``MarketTrendState.RANGE``) — a sideways market is
        a NEUTRAL frame, never a directional verdict.

    UNKNOWN
        No directional market-state disposition could be derived
        (insufficient structure / no usable data). Missing evidence is
        never fabricated.
    """

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    RANGE = "RANGE"
    UNKNOWN = "UNKNOWN"


_TREND_TO_DIRECTION = {
    MarketTrendState.BULLISH: MTFDirection.BULLISH,
    MarketTrendState.BEARISH: MTFDirection.BEARISH,
    MarketTrendState.RANGE: MTFDirection.NEUTRAL,
    MarketTrendState.NEUTRAL: MTFDirection.NEUTRAL,
    MarketTrendState.UNKNOWN: MTFDirection.UNKNOWN,
}


def direction_from_trend(trend: MarketTrend | None) -> MTFDirection:
    """Deterministic mapping from the reused Sprint 11P trend state."""

    if trend is None:
        return MTFDirection.UNKNOWN
    return _TREND_TO_DIRECTION.get(trend.state, MTFDirection.UNKNOWN)


class MarketStateCompleteness(Enum):
    """
    Per-symbol completeness of the MTF market-state result.

    COMPLETE
        EVERY configured timeframe carries usable completed data AND a
        derived market state. A complete result is never claimed when a
        timeframe is missing/unsupported/stale-without-state.

    INCOMPLETE
        At least one timeframe is observed but the full set could not be
        established (a frame is unsupported, provider-error, empty,
        stale, or has insufficient history for market-state
        classification). Partial coverage is explicit, never converted
        into full coverage.

    UNSUPPORTED
        EVERY configured timeframe is unsupported by the provider for
        this instrument (nothing can be established). Distinct from
        UNAVAILABLE (see below).

    UNAVAILABLE
        No usable per-timeframe data exists at all (provider errors /
        not-ready / empty / invalid responses across the set) — the MTF
        result has nothing observable to relate. Missing evidence is
        never fabricated.
    """

    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"


class MtfAlignmentState(Enum):
    """
    Relationship classification across the configured timeframes'
    market states (DELIBERATELY DISTINCT from the Sprint 11U
    ``MTFAlignment`` which describes a higher-timeframe context vs a
    lower-timeframe OPPORTUNITY direction — 19.4 expresses a pure
    market-state relationship with NO setup/opportunity semantics).

    ALIGNED
        Every usable timeframe exposes an identical directional market
        state (e.g. 15m BULLISH + 1h BULLISH). A TIMEFRAME RELATIONSHIP
        only — it is NOT a BUY signal.

    CONFLICTING
        At least two usable timeframes expose OPPOSING directional
        market states (e.g. 15m BULLISH + 1h BEARISH). Reported
        honestly; a TIMEFRAME RELATIONSHIP only — it is NOT a SHORT /
        NO-TRADE verdict.

    MIXED
        Usable timeframes expose a blend of directional and
        non-directional (RANGE/NEUTRAL/UNKNOWN) states, or every frame
        is non-directional — neither clearly aligned nor clearly
        conflicting. The relationship is indeterminate by observation.

    INCOMPLETE
        At least one timeframe could not establish a usable market state
        (unsupported / provider-error / empty / stale-without-state /
        insufficient history), so a full relationship cannot be
        asserted. Explicit, never converted into an implicit result.

    UNAVAILABLE
        No timeframe carries usable market state at all — there is
        nothing observable to relate.
    """

    ALIGNED = "ALIGNED"
    MIXED = "MIXED"
    CONFLICTING = "CONFLICTING"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"


def classify_alignment(
    timeframe_states: Sequence["MtfTimeframeState"],
) -> tuple[MtfAlignmentState, str]:
    """
    Deterministic MTF alignment / conflict classification.

    Rule (documented, pure, future-safe):

    1. No states -> ``UNAVAILABLE``.
    2. No state carries usable data with a derived market state ->
       ``UNAVAILABLE`` (nothing observable to relate).
    3. Fewer usable states than configured timeframes -> ``INCOMPLETE``
       (a missing/unsupported/insufficient frame makes the full
       relationship unassertable).
    4. Among the usable directional states:
       - two or more DISTINCT directions (BULLISH vs BEARISH) ->
         ``CONFLICTING``;
       - exactly one distinct directional state and it is the ONLY
         state present in every frame -> ``ALIGNED``;
       - otherwise (directional + non-directional mix, or all
         non-directional) -> ``MIXED``.

    The classification NEVER invents evidence: it reads only the
    already-computed per-timeframe market states (each itself derived
    from completed candles <= the reference instant).
    """

    states = list(timeframe_states)
    if not states:
        return MtfAlignmentState.UNAVAILABLE, "no timeframes configured."

    usable = [
        s for s in states
        if s.has_usable_data and s.market_state is not None
    ]
    if not usable:
        return (
            MtfAlignmentState.UNAVAILABLE,
            "no timeframe carries usable completed data with a derived "
            "market state — nothing observable to relate.",
        )
    if len(usable) != len(states):
        missing = [
            s.timeframe for s in states if s not in usable
        ]
        return (
            MtfAlignmentState.INCOMPLETE,
            "at least one configured timeframe could not establish a "
            f"usable market state ({', '.join(sorted(missing))}) — the "
            "full relationship cannot be asserted.",
        )

    directional = {
        s.direction for s in usable
        if s.direction in (MTFDirection.BULLISH, MTFDirection.BEARISH)
    }
    if len(directional) >= 2:
        return (
            MtfAlignmentState.CONFLICTING,
            "opposing directional market states observed across the "
            "configured timeframes: "
            + ", ".join(sorted(d.value for d in directional)) + ".",
        )
    if len(directional) == 1:
        one = next(iter(directional))
        non_directional = [
            s.timeframe for s in usable
            if s.direction not in (MTFDirection.BULLISH, MTFDirection.BEARISH)
        ]
        if not non_directional:
            return (
                MtfAlignmentState.ALIGNED,
                "all configured timeframes expose the same directional "
                f"market state ({one.value}).",
            )
        return (
            MtfAlignmentState.MIXED,
            "the configured timeframes agree on one directional market "
            f"state ({one.value}) while others are non-directional: "
            + ", ".join(sorted(non_directional)) + ".",
        )

    return (
        MtfAlignmentState.MIXED,
        "no directional market-state signal is observable on any "
        "configured timeframe (all states are non-directional / "
        "indeterminate) — neither clearly aligned nor clearly "
        "conflicting.",
    )


def compute_completeness(
    timeframe_states: Sequence["MtfTimeframeState"],
) -> MarketStateCompleteness:
    """Deterministic per-symbol completeness from the timeframe states."""

    states = list(timeframe_states)
    if not states:
        return MarketStateCompleteness.UNAVAILABLE
    usable = [s for s in states if s.has_usable_data]
    if not usable:
        if all(
            s.availability
            in (
                IntradayCoverageStatus.UNSUPPORTED_TIMEFRAME,
                IntradayCoverageStatus.UNSUPPORTED_INSTRUMENT,
            )
            for s in states
        ):
            return MarketStateCompleteness.UNSUPPORTED
        return MarketStateCompleteness.UNAVAILABLE
    if len(usable) != len(states):
        return MarketStateCompleteness.INCOMPLETE
    if all(s.market_state is not None for s in usable):
        return MarketStateCompleteness.COMPLETE
    return MarketStateCompleteness.INCOMPLETE


@dataclass(frozen=True, slots=True)
class MtfTimeframeState:
    """
    ONE configured timeframe's normalized market-state snapshot.

    Attributes:

    instrument
        Canonical instrument name.

    timeframe
        Canonical timeframe label (e.g. ``"15m"``, ``"1h"``).

    availability
        Reused Checkpoint 19.2
        :class:`~engine.models.intraday_coverage.IntradayCoverageStatus`
        classification — the SAME vocabulary as the 19.2 coverage layer
        (no parallel status set is invented).

    coverage
        The reused 19.2 :class:`IntradayInstrumentCoverage` classification
        (BY REFERENCE) when one was produced, else ``None`` (defensive).

    completed_candle_count
        Number of VALIDATED completed candles accepted at the reference
        instant (``0`` when none).

    latest_completed_timestamp
        Timestamp of the latest COMPLETED candle (``None`` when none).

    latest_completed_candle
        The latest COMPLETED candle (canonical :class:`OHLCVCandle`)
        carrying full OHLCV, or ``None`` when none exists.

    market_state
        The reused Sprint 11P :class:`MarketContext` (structure /
        descriptive trend / range / support-resistance) computed ONLY
        from completed candles <= the reference instant, or ``None``
        when the timeframe has no usable data / insufficient history.

    direction
        Derived descriptive :class:`MTFDirection` from the market state.

    market_state_available
        True exactly when usable completed candles existed AND a market
        state was derived.

    reason
        Human-readable summary of the timeframe state (provider-safe).
    """

    instrument: str
    timeframe: str
    availability: IntradayCoverageStatus
    coverage: IntradayInstrumentCoverage | None = None
    completed_candle_count: int = 0
    latest_completed_timestamp: datetime | None = None
    latest_completed_candle: OHLCVCandle | None = None
    market_state: MarketContext | None = None
    direction: MTFDirection = MTFDirection.UNKNOWN
    market_state_available: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        canon_inst = _canonical_name(self.instrument)
        object.__setattr__(self, "instrument", canon_inst)
        canonical = _canonical_timeframe(self.timeframe)
        if canonical is None:
            raise ValueError(
                f"unknown timeframe label {self.timeframe!r}.",
            )
        if canonical != self.timeframe:
            object.__setattr__(self, "timeframe", canonical)
        if self.latest_completed_timestamp is not None:
            _require_aware(
                self.latest_completed_timestamp,
                "latest_completed_timestamp",
            )
        if self.coverage is not None:
            object.__setattr__(self, "availability", self.coverage.status)
        else:
            object.__setattr__(
                self,
                "availability",
                self.availability
                or IntradayCoverageStatus.NOT_TESTED,
            )
        if (
            self.has_usable_data
            and self.market_state is not None
            and self.market_state.trend is not None
        ):
            direction = direction_from_trend(self.market_state.trend)
            object.__setattr__(self, "direction", direction)
            object.__setattr__(self, "market_state_available", True)
        else:
            object.__setattr__(self, "direction", MTFDirection.UNKNOWN)
            object.__setattr__(self, "market_state_available", False)

    @property
    def has_usable_data(self) -> bool:
        """True when usable completed candles were accepted (19.2 semantics)."""
        return bool(self.availability.is_valid_data)

    @property
    def is_current_fresh(self) -> bool:
        """True ONLY when the 19.2 status is exactly VALID / VALID_WITH_GAPS."""
        return self.availability in (
            IntradayCoverageStatus.VALID,
            IntradayCoverageStatus.VALID_WITH_GAPS,
        )


@dataclass(frozen=True, slots=True)
class PerSymbolMtfResult:
    """
    ONE instrument's complete multi-timeframe market-state result.

    Every configured timeframe appears EXACTLY ONCE in
    ``timeframe_states`` (a symbol can never be silently dropped, and a
    missing/unsupported/error/stale timeframe is explicit). The result is
    MARKET STATE ONLY — it carries no setup, opportunity, direction-of-
    -intent, entry/stop/target or ranking semantics.

    Attributes:

    instrument
        Canonical instrument name.

    timeframes
        Configured canonical timeframes (ascending duration; the
        presentation order).

    timeframe_states
        One :class:`MtfTimeframeState` per configured timeframe (in the
        same order).

    completeness
        :class:`MarketStateCompleteness` classification.

    alignment / alignment_reason
        :class:`MtfAlignmentState` relationship classification + the
        deterministic reason it was reached.

    reference_now
        The deterministic reference instant used for every completed-
        candle boundary.

    market_session
        :class:`engine.data.market_session.MarketSessionState` at the
        reference instant, or ``None`` when not evaluated.

    error
        Defensive whole-symbol error detail (``""`` when none; never a
        credential / sensitive value).
    """

    instrument: str
    timeframes: tuple[str, ...]
    timeframe_states: tuple[MtfTimeframeState, ...] = field(
        default_factory=tuple,
    )
    completeness: MarketStateCompleteness = (
        MarketStateCompleteness.UNAVAILABLE
    )
    alignment: MtfAlignmentState = MtfAlignmentState.UNAVAILABLE
    alignment_reason: str = ""
    reference_now: datetime | None = None
    market_session: Any | None = None
    error: str = ""

    def __post_init__(self) -> None:
        canon = _canonical_name(self.instrument)
        object.__setattr__(self, "instrument", canon)
        clean_tf = tuple(_canonical_timeframe(t) for t in self.timeframes)
        if any(t is None for t in clean_tf):
            raise ValueError("timeframes must be canonical labels.")
        object.__setattr__(
            self,
            "timeframes",
            tuple(t for t in clean_tf),
        )
        if self.reference_now is not None:
            _require_aware(self.reference_now, "reference_now")
        if self.timeframe_states:
            if len(self.timeframe_states) != len(self.timeframes):
                raise ValueError(
                    "exactly one timeframe state per configured "
                    f"timeframe is required: expected {len(self.timeframes)} "
                    f"states, got {len(self.timeframe_states)}.",
                )
            seen: set[str] = set()
            for state, tf in zip(self.timeframe_states, self.timeframes):
                if not isinstance(state, MtfTimeframeState):
                    raise TypeError(
                        "timeframe_states must be MtfTimeframeState objects.",
                    )
                if state.timeframe != tf:
                    raise ValueError(
                        f"timeframe state {state.timeframe!r} does not "
                        f"match configured timeframe {tf!r}.",
                    )
                if state.instrument != self.instrument:
                    raise ValueError(
                        "timeframe state instrument does not match the "
                        "symbol result instrument.",
                    )
                if state.timeframe in seen:
                    raise ValueError(
                        f"duplicate timeframe state {state.timeframe!r}.",
                    )
                seen.add(state.timeframe)
            try:
                computed = compute_completeness(self.timeframe_states)
                object.__setattr__(self, "completeness", computed)
                relation, reason = classify_alignment(self.timeframe_states)
                object.__setattr__(self, "alignment", relation)
                object.__setattr__(self, "alignment_reason", reason)
            except Exception:
                # Invariant set already defaults; post_init must never
                # mask a caller-provided explicit classification.
                pass

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    def state_for(self, timeframe: str) -> MtfTimeframeState | None:
        """Per-timeframe state lookup (``None`` when not configured)."""
        canonical = _canonical_timeframe(timeframe)
        if canonical is None:
            return None
        for state in self.timeframe_states:
            if state.timeframe == canonical:
                return state
        return None

    @property
    def timeframe_count(self) -> int:
        return len(self.timeframes)

    @property
    def is_complete(self) -> bool:
        return self.completeness is MarketStateCompleteness.COMPLETE

    @property
    def alignment_is_aligned(self) -> bool:
        return self.alignment is MtfAlignmentState.ALIGNED

    @property
    def alignment_is_conflicting(self) -> bool:
        return self.alignment is MtfAlignmentState.CONFLICTING


@dataclass(frozen=True, slots=True)
class MtfUniverseCounts:
    """
    Deterministic aggregate counts over an MTF universe analysis.

    ``complete/incomplete/unsupported/unavailable`` are completeness
    buckets; ``aligned/mixed/conflicting/alignment_incomplete/
    alignment_unavailable`` tally the relationship classifications.
    ``tested`` == sum of the four completeness buckets.
    """

    complete: int = 0
    incomplete: int = 0
    unsupported: int = 0
    unavailable: int = 0
    aligned: int = 0
    mixed: int = 0
    conflicting: int = 0
    alignment_incomplete: int = 0
    alignment_unavailable: int = 0

    @property
    def tested(self) -> int:
        return self.complete + self.incomplete + self.unsupported + self.unavailable

    @property
    def completeness_ratio(self) -> float:
        """
        Proportion of TESTED symbols with a COMPLETE MTF result, in
        ``[0, 1]`` (0 when nothing was tested). Partial coverage can
        never become false full coverage.
        """
        tested = self.tested
        if tested == 0:
            return 0.0
        return self.complete / tested

    @property
    def aligned_ratio(self) -> float:
        tested = self.tested
        if tested == 0:
            return 0.0
        return self.aligned / tested


#: MTF analysis id prefix (documented, deterministic).
MTF_ANALYSIS_ID_PREFIX = "mtf-"


def mtf_analysis_id(
    timeframes: tuple[str, ...],
    universe: tuple[str, ...],
    reference_now: datetime,
) -> str:
    """Deterministic MTF analysis identity.

    ``"mtf-" + sha256[:16]`` over canonical (timeframes + sorted
    universe + reference instant). Repeated analysis of the same instant
    yields the same id (idempotency / reproducibility).
    """

    parts = [
        ",".join(timeframes),
        "|".join(sorted(set(_canonical_name(n) for n in universe))),
        reference_now.astimezone(UTC).isoformat(),
    ]
    return _sha256_prefix("|".join(parts), MTF_ANALYSIS_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class MtfUniverseAnalysis:
    """
    Deterministic universe-wide MTF market-state analysis.

    Attributes:

    analysis_id
        Deterministic MTF analysis identity (``mtf-<sha256[:16]>``).

    reference_now
        The deterministic reference instant.

    timeframes
        Configured canonical timeframes.

    universe_instrument_count
        Requested universe size.

    instruments
        Canonical instruments requested (sorted, de-duplicated).

    results
        Per-symbol :class:`PerSymbolMtfResult` — EXACTLY ONE per
        requested constituent (never silently dropped), ordered
        canonically.

    counts
        :class:`MtfUniverseCounts` aggregate.

    market_session
        :class:`engine.data.market_session.MarketSessionState` at the
        reference instant, or ``None``.

    error
        Defensive analysis-level error detail (``""`` when none).
    """

    analysis_id: str
    reference_now: datetime
    timeframes: tuple[str, ...]
    universe_instrument_count: int
    instruments: tuple[str, ...] = field(default_factory=tuple)
    results: tuple[PerSymbolMtfResult, ...] = field(default_factory=tuple)
    counts: MtfUniverseCounts = field(default_factory=MtfUniverseCounts)
    market_session: Any | None = None
    error: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.reference_now, "reference_now")
        if not self.analysis_id.strip():
            raise ValueError("analysis_id must not be empty")
        clean_tf = tuple(_canonical_timeframe(t) for t in self.timeframes)
        if any(t is None for t in clean_tf):
            raise ValueError("timeframes must be canonical labels.")
        object.__setattr__(self, "timeframes", tuple(t for t in clean_tf))
        canon_universe = tuple(sorted(set(_canonical_name(n) for n in self.instruments)))
        object.__setattr__(self, "instruments", canon_universe)
        if self.results and len(self.results) != len(canon_universe):
            raise ValueError(
                f"expected exactly {len(canon_universe)} per-symbol results, "
                f"got {len(self.results)}.",
            )
        if self.results:
            seen: set[str] = set()
            for result in self.results:
                if not isinstance(result, PerSymbolMtfResult):
                    raise TypeError(
                        "results must be PerSymbolMtfResult objects.",
                    )
                if result.instrument in seen:
                    raise ValueError(
                        f"duplicate per-symbol result for {result.instrument!r}.",
                    )
                seen.add(result.instrument)
            missing = set(canon_universe) - seen
            if missing:
                raise ValueError(
                    "per-symbol results missing constituents: "
                    f"{sorted(missing)!r}.",
                )
            extras = seen - set(canon_universe)
            if extras:
                raise ValueError(
                    "per-symbol results contain non-universe symbols: "
                    f"{sorted(extras)!r}.",
                )

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    @property
    def instrument_count(self) -> int:
        return len(self.results)

    def result_for(self, instrument: str) -> PerSymbolMtfResult | None:
        """Per-symbol lookup (``None`` when not present)."""
        canon = _canonical_name(instrument)
        for result in self.results:
            if result.instrument == canon:
                return result
        return None

    @property
    def is_empty(self) -> bool:
        return not self.results


__all__ = [
    "MTF_ANALYSIS_ID_PREFIX",
    "MTFDirection",
    "MarketStateCompleteness",
    "MtfAlignmentState",
    "MtfTimeframeState",
    "MtfUniverseAnalysis",
    "MtfUniverseCounts",
    "PerSymbolMtfResult",
    "classify_alignment",
    "compute_completeness",
    "direction_from_trend",
    "mtf_analysis_id",
]