"""
Setup-quality intelligence models (Checkpoint 19.5).

These models describe the deterministic SETUP-QUALITY layer that
answers, at one reference instant and for one instrument:

    "Given the market state currently available for this instrument,
     does it contain a potentially meaningful trading setup, how
     strong is that setup according to the defined quality model,
     and why?"

The layer transforms the FROZEN 19.4 multi-timeframe market-state
result (:class:`engine.models.mtf_analysis.PerSymbolMtfResult`) into
EXPLICIT, EXPLAINABLE, DETERMINISTIC setup-quality intelligence:

    19.4 MTF MARKET STATE
            |
    SETUP CANDIDATE DETECTION   (reused Sprint 11O + 11Q)
            |
    SETUP QUALITY ASSESSMENT    (this layer)
            |
    QUALITY / CONFIDENCE CLASSIFICATION
            |
    CANDIDATE RANKING
            |
    19.6 SETUP LIFECYCLE (future)

These models are SETUP-QUALITY / DATA-STRUCTURE models ONLY:

* They carry NO trade execution, NO broker order placement, NO
  portfolio/position management, NO automated entries/exits, NO
  user notifications, NO setup lifecycle/state persistence, NO
  forward-testing infrastructure, NO reliability/watchdog framework.
* A ``Setup Quality Score`` describes how strongly the OBSERVED
  technical evidence supports the detected setup according to the
  defined model. It is NOT a probability of success, NOT a
  profitability prediction, and NOT a trading recommendation.
* A ``SETUP`` is a structured market condition that may represent a
  potential trading opportunity. A ``SETUP QUALITY SCORE`` describes
  the strength of the evidence behind it. A ``TRADE PLAN`` (entry /
  stop / target / risk-reward) is a DOWNSTREAM concept that this layer
  deliberately does NOT compute.
* Incomplete / stale / unsupported / missing data is NEVER silently
  converted into positive evidence.

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses.
* Optional fields use ``None`` (or explicit ``INCOMPLETE`` /
  ``UNAVAILABLE`` members) so "unobserved" is never silently a real
  value.
* ``__post_init__`` performs structural validation only; the scoring
  / classification / ranking logic lives in the engine and in pure,
  exported helpers.
* No wall-clock dependence: timestamps are explicit, caller-supplied,
  timezone-aware values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Sequence

from engine.models.mtf_analysis import PerSymbolMtfResult
from engine.models.setup_confluence import (
    SetupClassification,
    SetupDirection,
)


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


class SetupQualityStatus(Enum):
    """
    Per-symbol setup-quality evaluation state (Checkpoint 19.5).

    This enum is DELIBERATELY DISTINCT from the Sprint 11Q
    ``SetupClassification`` (NO_SETUP / WATCH / POTENTIAL_SETUP), the
    Sprint 11R ``CandidateStatus`` (NO_CANDIDATE / WATCH / CANDIDATE)
    and the Sprint 11S ``DecisionClassification`` (REJECTED / WATCH /
    QUALIFIED / PREFERRED). 19.5 describes SETUP-QUALITY evaluation
    state; the reused 11Q classification is carried separately on the
    result.

    QUALIFIED
        A setup candidate was DETECTED (reused 11Q POTENTIAL_SETUP) AND
        passed the minimum quality bar (classification is EXCELLENT /
        HIGH / MEDIUM / LOW). A QUALIFIED setup is a coherent candidate
        worth further evaluation — NOT a trade signal, NOT a
        prediction.

    WATCH
        Directional evidence exists but the setup is below the minimum
        quality bar (reused 11Q WATCH — "not strong enough to form a
        coherent candidate setup"). Worth monitoring, never qualified.

    NO_SETUP
        No setup candidate was detected (reused 11Q NO_SETUP). The
        market state does not form a coherent setup at this instant.

    INCOMPLETE
        Data is insufficient to evaluate the setup quality (the
        primary timeframe carries no usable market state, or the MTF
        completeness is UNSUPPORTED). Explicit — never a setup, never
        fabricated.

    UNAVAILABLE
        No usable data exists at all (nothing observable to evaluate).
        Missing evidence is never fabricated.
    """

    QUALIFIED = "QUALIFIED"
    WATCH = "WATCH"
    NO_SETUP = "NO_SETUP"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def rank_value(self) -> int:
        """Higher is stronger; used for deterministic ordering."""
        return _STATUS_RANK[self]


_STATUS_RANK = {
    SetupQualityStatus.QUALIFIED: 4,
    SetupQualityStatus.WATCH: 3,
    SetupQualityStatus.NO_SETUP: 2,
    SetupQualityStatus.INCOMPLETE: 1,
    SetupQualityStatus.UNAVAILABLE: 0,
}


class SetupQualityClassification(Enum):
    """
    Deterministic quality band for a setup-quality result.

    The band is derived SOLELY from the bounded score + the documented
    negative-evidence / data-quality caps (see the engine). It is
    DESCRIPTIVE — it never predicts profitability.

    EXCELLENT
        Score >= excellent_threshold and no disqualifying cap.

    HIGH
        Score >= high_threshold (and below excellent) with no
        disqualifying cap.

    MEDIUM
        Score >= medium_threshold (and below high), or capped at MEDIUM
        by conflicting evidence.

    LOW
        Score >= low_threshold (and below medium), or capped at LOW by
        a WATCH-level setup / stale data / incomplete MTF.

    REJECTED
        Score below low_threshold, OR no setup candidate was detected
        (reused 11Q NO_SETUP). Not worth monitoring as a setup.

    INCOMPLETE
        Data insufficient to compute a score (data-gate state; never a
        quality band).

    UNAVAILABLE
        No usable data (data-gate state; never a quality band).
    """

    EXCELLENT = "EXCELLENT"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"
    UNAVAILABLE = "UNAVAILABLE"

    @property
    def rank_value(self) -> int:
        """Higher is stronger; used for deterministic ordering."""
        return _CLASSIFICATION_RANK[self]

    @property
    def is_qualified_band(self) -> bool:
        """True for the quality bands a QUALIFIED setup may carry."""
        return self in (
            SetupQualityClassification.EXCELLENT,
            SetupQualityClassification.HIGH,
            SetupQualityClassification.MEDIUM,
            SetupQualityClassification.LOW,
        )


_CLASSIFICATION_RANK = {
    SetupQualityClassification.EXCELLENT: 6,
    SetupQualityClassification.HIGH: 5,
    SetupQualityClassification.MEDIUM: 4,
    SetupQualityClassification.LOW: 3,
    SetupQualityClassification.REJECTED: 2,
    SetupQualityClassification.INCOMPLETE: 1,
    SetupQualityClassification.UNAVAILABLE: 0,
}


@dataclass(frozen=True, slots=True)
class SetupQualityScoreComponent:
    """
    One interpretable component of a ``Setup Quality Score``.

    Every point the engine awards (or withholds) is attributable to a
    named, human-readable component so a reviewer can understand WHY a
    score was reached without rerunning the pipeline.

    Attributes:

    name
        Short canonical name of the component (``"mtf_alignment"``,
        ``"setup_confirmation"``, ``"setup_confluence"``,
        ``"structure_quality"``).

    points
        Points awarded for this component (integer >= 0).

    max_points
        Maximum points available for this component (integer >= 0,
        >= ``points``).

    reason
        Human-readable explanation of the award (derived from the
        actual inputs, never a generic template).
    """

    name: str
    points: int
    max_points: int
    reason: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("component name must not be empty.")
        if isinstance(self.points, bool) or not isinstance(self.points, int):
            raise TypeError("points must be an int.")
        if isinstance(self.max_points, bool) or not isinstance(self.max_points, int):
            raise TypeError("max_points must be an int.")
        if self.points < 0 or self.max_points < 0:
            raise ValueError("points and max_points must be non-negative.")
        if self.points > self.max_points:
            raise ValueError("points must not exceed max_points.")


@dataclass(frozen=True, slots=True)
class SetupQualityResult:
    """
    ONE instrument's complete setup-quality assessment at a reference
    instant (Checkpoint 19.5).

    The result is DESCRIPTIVE. It is NOT a trade signal, NOT a
    prediction, NOT a guarantee of profitability, and NOT a trading
    recommendation. It deliberately does NOT compute entry / stop /
    target / risk-reward (those belong to the downstream trade-plan
    layer).

    Attributes:

    instrument
        Canonical instrument name.

    reference_now
        The deterministic reference instant (explicit, timezone-aware).

    mtf
        The reused 19.4 :class:`PerSymbolMtfResult` (BY REFERENCE,
        never modified).

    primary_timeframe
        The canonical primary (setup / execution) timeframe the
        single-frame setup detection ran on.

    status
        :class:`SetupQualityStatus` evaluation state.

    classification
        :class:`SetupQualityClassification` quality band.

    score
        Bounded integer ``[0, max_score]`` (``None`` for INCOMPLETE /
        UNAVAILABLE data-gate states — never fabricated).

    score_components
        Tuple of :class:`SetupQualityScoreComponent` (the full,
        auditable scoring breakdown). Empty for data-gate states.

    setup_type
        Reused Sprint 11R :class:`engine.models.trade_candidate.SetupType`
        name (``"TREND_CONTINUATION"`` / ``"BREAKOUT"`` /
        ``"STRUCTURE_CONTINUATION"`` / ``"SETUP_CANDIDATE"``), or
        ``None`` when no directional setup was detected. Carried as a
        string so this model layer has no trade-candidate dependency.

    setup_direction
        Reused 11Q :class:`SetupDirection` (BULLISH / BEARISH /
        NEUTRAL / UNKNOWN), or ``None`` when not evaluable.

    setup_classification
        Reused 11Q :class:`SetupClassification` (NO_SETUP / WATCH /
        POTENTIAL_SETUP), or ``None`` when the data gate prevented
        detection.

    confluence_score
        Reused 11Q confluence score (``[0, 5]``), or ``None`` when not
        evaluable.

    has_conflict
        Whether the reused 11Q assessment recorded conflicting
        evidence.

    positive_factors
        Deterministic, ordered tuple of short positive-evidence labels
        (derived from the actual inputs).

    negative_factors
        Deterministic, ordered tuple of short negative-evidence labels
        (derived from the actual inputs).

    data_complete
        ``True`` when the 19.4 MTF completeness is COMPLETE (every
        configured frame usable). An incomplete relation is never
        treated as complete.

    stale_data
        ``True`` when the primary timeframe data is STALE (19.2
        semantics). Stale data is a hard data-quality caveat.

    explanation
        Human-readable summary of why this quality was reached
        (generated from the actual scoring inputs).

    reason
        Short human-readable summary of the evaluation state.
    """

    instrument: str
    reference_now: datetime
    mtf: PerSymbolMtfResult
    primary_timeframe: str
    status: SetupQualityStatus
    classification: SetupQualityClassification
    score: int | None = None
    score_components: tuple[SetupQualityScoreComponent, ...] = field(
        default_factory=tuple,
    )
    setup_type: str | None = None
    setup_direction: SetupDirection | None = None
    setup_classification: SetupClassification | None = None
    confluence_score: int | None = None
    has_conflict: bool = False
    positive_factors: tuple[str, ...] = field(default_factory=tuple)
    negative_factors: tuple[str, ...] = field(default_factory=tuple)
    data_complete: bool = False
    stale_data: bool = False
    explanation: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "instrument", _canonical_name(self.instrument),
        )
        _require_aware(self.reference_now, "reference_now")
        if not isinstance(self.mtf, PerSymbolMtfResult):
            raise TypeError("mtf must be a PerSymbolMtfResult.")
        if self.mtf.instrument != self.instrument:
            raise ValueError(
                "mtf result instrument does not match the setup-quality "
                "result instrument.",
            )
        if self.mtf.reference_now is not None and (
            self.mtf.reference_now != self.reference_now
        ):
            raise ValueError(
                "reference_now must match the mtf result reference instant.",
            )
        if self.primary_timeframe not in self.mtf.timeframes:
            raise ValueError(
                f"primary_timeframe {self.primary_timeframe!r} must be one "
                f"of the configured mtf timeframes "
                f"{list(self.mtf.timeframes)!r}.",
            )
        # Data-gate states carry no score / components / setup fields.
        if self.status in (
            SetupQualityStatus.INCOMPLETE,
            SetupQualityStatus.UNAVAILABLE,
        ):
            if self.score is not None:
                raise ValueError(
                    "data-gate states must not carry a score.",
                )
            if self.score_components:
                raise ValueError(
                    "data-gate states must not carry score components.",
                )
            if self.classification not in (
                SetupQualityClassification.INCOMPLETE,
                SetupQualityClassification.UNAVAILABLE,
            ):
                raise ValueError(
                    "data-gate states must carry the matching data-gate "
                    "classification.",
                )
        else:
            # Non-gate states must carry a bounded score + classification.
            if self.score is None:
                raise ValueError(
                    "a non-gate setup-quality result must carry a score.",
                )
            if not self.score_components:
                raise ValueError(
                    "a non-gate setup-quality result must carry score "
                    "components.",
                )
            if isinstance(self.score, bool) or not isinstance(self.score, int):
                raise TypeError("score must be an int.")
            if not 0 <= self.score <= 100:
                raise ValueError("score must lie within [0, 100].")
            if self.classification in (
                SetupQualityClassification.INCOMPLETE,
                SetupQualityClassification.UNAVAILABLE,
            ):
                raise ValueError(
                    "a scored result must not carry a data-gate "
                    "classification.",
                )

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    @property
    def is_qualified(self) -> bool:
        """True when the setup is a QUALIFIED candidate."""
        return self.status is SetupQualityStatus.QUALIFIED

    @property
    def score_component_map(self) -> dict[str, int]:
        """Component name -> points (deterministic)."""
        return {c.name: c.points for c in self.score_components}

    @property
    def setup_type_enum_name(self) -> str | None:
        """Reused SetupType member name (string form)."""
        return self.setup_type


@dataclass(frozen=True, slots=True)
class SetupQualityCounts:
    """
    Deterministic aggregate counts over a setup-quality universe
    result.

    ``qualified / watch / no_setup / incomplete / unavailable`` are
    status buckets (``tested`` == sum of the five). ``excellent /
    high / medium / low / rejected`` tally the classification
    distribution. ``qualified_ratio`` is the proportion of TESTED
    symbols with a QUALIFIED setup.
    """

    qualified: int = 0
    watch: int = 0
    no_setup: int = 0
    incomplete: int = 0
    unavailable: int = 0
    excellent: int = 0
    high: int = 0
    medium: int = 0
    low: int = 0
    rejected: int = 0

    @property
    def tested(self) -> int:
        return (
            self.qualified
            + self.watch
            + self.no_setup
            + self.incomplete
            + self.unavailable
        )

    @property
    def qualified_ratio(self) -> float:
        """Proportion of TESTED symbols with a QUALIFIED setup, in
        ``[0, 1]`` (0 when nothing was tested)."""
        tested = self.tested
        if tested == 0:
            return 0.0
        return self.qualified / tested


#: Setup-quality analysis id prefix (documented, deterministic).
SETUP_QUALITY_ID_PREFIX = "sq-"


def setup_quality_analysis_id(
    timeframes: tuple[str, ...],
    universe: tuple[str, ...],
    reference_now: datetime,
    config_snapshot: Sequence[tuple[str, str]],
) -> str:
    """Deterministic setup-quality analysis identity.

    ``"sq-" + sha256[:16]`` over canonical (timeframes + sorted
    universe + reference instant + config snapshot). Repeated analysis
    of the same instant + config yields the same id.
    """

    parts = [
        ",".join(timeframes),
        "|".join(sorted(set(_canonical_name(n) for n in universe))),
        reference_now.astimezone(UTC).isoformat(),
        ";".join(f"{k}={v}" for k, v in sorted(config_snapshot)),
    ]
    return _sha256_prefix("|".join(parts), SETUP_QUALITY_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class SetupQualityUniverseResult:
    """
    Deterministic universe-wide setup-quality analysis (Checkpoint
    19.5).

    Every requested constituent appears EXACTLY ONCE in ``results``
    (never silently dropped), ordered canonically. QUALIFIED candidates
    are additionally surfaced in ``ranked_qualified`` (strongest-first)
    and ``top_n`` (a capped view over ``ranked_qualified`` when
    ``max_qualified`` is set). Ranking is deterministic; rejected /
    incomplete / unavailable symbols are never confused with qualified
    candidates.

    Attributes:

    analysis_id
        Deterministic setup-quality analysis identity
        (``sq-<sha256[:16]>``).

    reference_now
        The deterministic reference instant.

    timeframes
        Configured canonical timeframes (from the 19.4 results).

    primary_timeframe
        The canonical primary (setup) timeframe used.

    universe_instrument_count
        Requested universe size.

    instruments
        Canonical instruments requested (sorted, de-duplicated).

    results
        Per-symbol :class:`SetupQualityResult` — EXACTLY ONE per
        requested constituent.

    counts
        :class:`SetupQualityCounts` aggregate.

    ranked_qualified
        QUALIFIED results ranked strongest-first (deterministic).

    top_n
        The top-N view over ``ranked_qualified`` (== ``ranked_qualified``
        when ``max_qualified`` is None).

    max_qualified
        The configured cap (``None`` = no cap).

    error
        Defensive analysis-level error detail (``""`` when none).
    """

    analysis_id: str
    reference_now: datetime
    timeframes: tuple[str, ...]
    primary_timeframe: str
    universe_instrument_count: int
    instruments: tuple[str, ...] = field(default_factory=tuple)
    results: tuple[SetupQualityResult, ...] = field(default_factory=tuple)
    counts: SetupQualityCounts = field(default_factory=SetupQualityCounts)
    ranked_qualified: tuple[SetupQualityResult, ...] = field(
        default_factory=tuple,
    )
    top_n: tuple[SetupQualityResult, ...] = field(default_factory=tuple)
    max_qualified: int | None = None
    error: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.reference_now, "reference_now")
        if not self.analysis_id.strip():
            raise ValueError("analysis_id must not be empty")
        canon_universe = tuple(
            sorted(set(_canonical_name(n) for n in self.instruments)),
        )
        object.__setattr__(self, "instruments", canon_universe)
        if self.results and len(self.results) != len(canon_universe):
            raise ValueError(
                f"expected exactly {len(canon_universe)} per-symbol "
                f"results, got {len(self.results)}.",
            )
        if self.results:
            seen: set[str] = set()
            for result in self.results:
                if not isinstance(result, SetupQualityResult):
                    raise TypeError(
                        "results must be SetupQualityResult objects.",
                    )
                if result.instrument in seen:
                    raise ValueError(
                        f"duplicate per-symbol result for "
                        f"{result.instrument!r}.",
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
        # ranked_qualified must be a deterministic subset of results.
        if self.ranked_qualified:
            ranked_ids = {r.instrument for r in self.ranked_qualified}
            if not all(r.is_qualified for r in self.ranked_qualified):
                raise ValueError(
                    "ranked_qualified must contain only QUALIFIED results.",
                )
            if not ranked_ids <= {r.instrument for r in self.results}:
                raise ValueError(
                    "ranked_qualified must be a subset of results.",
                )
            if len(ranked_ids) != len(self.ranked_qualified):
                raise ValueError(
                    "ranked_qualified must not contain duplicates.",
                )
        if self.top_n:
            top_ids = [r.instrument for r in self.top_n]
            ranked_ids = [r.instrument for r in self.ranked_qualified]
            if top_ids != ranked_ids[: len(top_ids)]:
                raise ValueError(
                    "top_n must be a prefix of ranked_qualified.",
                )

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    @property
    def instrument_count(self) -> int:
        return len(self.results)

    def result_for(self, instrument: str) -> SetupQualityResult | None:
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
    "SETUP_QUALITY_ID_PREFIX",
    "SetupQualityClassification",
    "SetupQualityCounts",
    "SetupQualityResult",
    "SetupQualityScoreComponent",
    "SetupQualityStatus",
    "SetupQualityUniverseResult",
    "setup_quality_analysis_id",
]