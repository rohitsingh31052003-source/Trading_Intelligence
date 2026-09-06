"""
Configuration for the setup-quality intelligence layer (Checkpoint 19.5).

All scoring weights, thresholds, caps and data-quality gates live here;
no magic numbers are embedded in the engine. The defaults are
deliberately conservative and deterministic. They are NOT calibrated to
any market; they express interpretable, rule-based setup-quality
criteria over the frozen 19.4 multi-timeframe market-state layer.

The 19.5 quality model is DESCRIPTIVE:

* A ``Setup Quality Score`` expresses how strongly the OBSERVED
  technical evidence supports the detected setup according to the
  defined model. It is NOT a probability of success, NOT a
  profitability prediction, and NOT a trading recommendation.
* Every score component has a documented meaning and contribution
  (see the Checkpoint 19.5 audit document for the full methodology).
* Data quality is a PREREQUISITE: stale / incomplete / unsupported /
  missing data can never become a high-quality setup.

Weights (documented; sum = 100):

``mtf_alignment``
    Multi-timeframe relationship quality (reused 19.4
    ``MtfAlignmentState``). ALIGNED earns the full weight; MIXED earns
    a partial share; CONFLICTING / INCOMPLETE / UNAVAILABLE earn zero
    (conflict and missing evidence are never positive evidence).

``setup_confirmation``
    Single-frame setup confirmation (reused 11Q
    ``SetupClassification``). POTENTIAL_SETUP earns the full weight;
    WATCH earns a partial share; NO_SETUP earns zero.

``setup_confluence``
    Number of aligned independent evidence sources (reused 11Q
    ``SetupAssessment.confluence_score`` in ``[0, 5]``), projected onto
    the weight. More independent agreement -> stronger.

``structure_quality``
    Structural-foundation robustness of the primary timeframe (reused
    Sprint 11P ``MarketContext``: the descriptive ``structure_intact``
    flag + the number of confirmed swings). A robust, intact structural
    base earns the full weight.

Thresholds (bounded score [0, max_score] = [0, 100]):

``excellent_threshold``    80  (inclusive)
``high_threshold``         65  (inclusive)
``medium_threshold``       50  (inclusive)
``low_threshold``          35  (inclusive)
    A score below ``low_threshold`` is REJECTED.

Caps (each can only REDUCE the classification; applied in order):

``watch_max_classification``
    Strongest classification a WATCH-level setup (reused 11Q WATCH)
    may reach. Default LOW (a WATCH-level setup is never a qualified
    high-quality setup by definition).

``no_setup_classification``
    Classification assigned when no setup candidate was detected
    (reused 11Q NO_SETUP). Default REJECTED — no setup is never a
    qualified setup.

``conflict_max_classification``
    Strongest classification when conflicting evidence is present
    (reused 11Q ``has_conflict``). Default MEDIUM — a conflicting
    setup is never EXCELLENT/HIGH.

``stale_data_max_classification``
    Strongest classification when the primary timeframe data is STALE
    (reused 19.2 ``IntradayCoverageStatus.STALE``). Default LOW —
    stale data is a hard data-quality caveat and never a high-quality
    setup.

``incomplete_mtf_max_classification``
    Strongest classification when the 19.4 MTF completeness is
    INCOMPLETE (a configured frame is missing / unsupported /
    without usable state). Default LOW — an incomplete multi-frame
    relation is never a high-quality setup.

Partial-credit fraction:

``partial_fraction``
    Fraction of a component weight awarded for partial evidence
    (e.g. an 11Q WATCH, an MTF MIXED relation, a partial structural
    base). Default ``0.5``; must lie within ``[0.0, 1.0]``.

Primary-timeframe selection:

``primary_timeframe``
    The canonical intraday timeframe on which single-frame setup
    detection runs (the "setup / execution" frame). When ``None``
    (default) the LOWEST-DURATION configured 19.4 timeframe is used
    (the engine convention: the lowest-duration intraday frame is the
    setup/execution frame). When set, it must be one of the
    configured 19.4 timeframes (validated at evaluation time).

Ranking:

``max_qualified``
    Optional cap on the number of QUALIFIED candidates surfaced in the
    ``top_n`` view (``None`` = all qualified candidates are surfaced).
    The full ranked candidate set remains available internally.

Data-quality gates:

``min_usable_for_setup``
    Not a score component: the 19.4 primary-timeframe state must carry
    usable completed data + a derived market state
    (``market_state_available``) before ANY setup detection / scoring
    is attempted. Below the gate the result is INCOMPLETE/UNAVAILABLE
    (never a setup).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SetupQualityConfig:
    """
    Configuration for :class:`dashboard.setup_quality.SetupQualityEngine`.

    Every attribute is validated at construction (no magic values).
    """

    mtf_alignment_weight: int = 30
    setup_confirmation_weight: int = 30
    setup_confluence_weight: int = 25
    structure_quality_weight: int = 15

    excellent_threshold: int = 80
    high_threshold: int = 65
    medium_threshold: int = 50
    low_threshold: int = 35

    watch_max_classification: str = "LOW"
    no_setup_classification: str = "REJECTED"
    conflict_max_classification: str = "MEDIUM"
    stale_data_max_classification: str = "LOW"
    incomplete_mtf_max_classification: str = "LOW"

    partial_fraction: float = 0.5

    primary_timeframe: str | None = None

    max_qualified: int | None = None

    def __post_init__(self) -> None:
        weights = (
            self.mtf_alignment_weight,
            self.setup_confirmation_weight,
            self.setup_confluence_weight,
            self.structure_quality_weight,
        )
        for name, value in (
            ("mtf_alignment_weight", self.mtf_alignment_weight),
            ("setup_confirmation_weight", self.setup_confirmation_weight),
            ("setup_confluence_weight", self.setup_confluence_weight),
            ("structure_quality_weight", self.structure_quality_weight),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if value < 0:
                raise ValueError(f"{name} must be non-negative.")
        if sum(weights) != 100:
            raise ValueError(
                "setup-quality weights must sum to 100 "
                f"(got {sum(weights)}).",
            )
        # Classification thresholds: ascending within [0, max].
        thresholds = (
            ("low_threshold", self.low_threshold),
            ("medium_threshold", self.medium_threshold),
            ("high_threshold", self.high_threshold),
            ("excellent_threshold", self.excellent_threshold),
        )
        for name, value in thresholds:
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an int.")
            if not 0 <= value <= 100:
                raise ValueError(
                    f"{name} must lie within [0, 100].",
                )
        if not (
            self.low_threshold
            <= self.medium_threshold
            <= self.high_threshold
            <= self.excellent_threshold
        ):
            raise ValueError(
                "Thresholds must satisfy low <= medium <= high <= excellent.",
            )
        self._validate_classification_member(
            self.watch_max_classification, "watch_max_classification",
        )
        self._validate_classification_member(
            self.no_setup_classification, "no_setup_classification",
        )
        self._validate_classification_member(
            self.conflict_max_classification, "conflict_max_classification",
        )
        self._validate_classification_member(
            self.stale_data_max_classification, "stale_data_max_classification",
        )
        self._validate_classification_member(
            self.incomplete_mtf_max_classification,
            "incomplete_mtf_max_classification",
        )
        if isinstance(self.partial_fraction, bool) or not isinstance(
            self.partial_fraction, (int, float),
        ):
            raise TypeError("partial_fraction must be a number.")
        if not 0.0 <= self.partial_fraction <= 1.0:
            raise ValueError(
                "partial_fraction must lie within [0.0, 1.0].",
            )
        if self.primary_timeframe is not None:
            from engine.data.historical_times import canonical_timeframe

            if not isinstance(self.primary_timeframe, str):
                raise TypeError("primary_timeframe must be a str or None.")
            resolved = canonical_timeframe(self.primary_timeframe)
            if resolved is None:
                raise ValueError(
                    f"unknown primary_timeframe {self.primary_timeframe!r} — "
                    "arbitrary strings are never silently accepted.",
                )
            if resolved == "1D":
                raise ValueError(
                    "primary_timeframe must be an intraday timeframe "
                    "(1D daily context belongs to later checkpoints).",
                )
            object.__setattr__(self, "primary_timeframe", resolved)
        if self.max_qualified is not None:
            if isinstance(self.max_qualified, bool) or not isinstance(
                self.max_qualified, int,
            ):
                raise TypeError("max_qualified must be an int or None.")
            if self.max_qualified < 1:
                raise ValueError("max_qualified must be >= 1 when set.")

    @staticmethod
    def _validate_classification_member(value: str, name: str) -> None:
        from engine.models.setup_quality import SetupQualityClassification

        if not isinstance(value, str):
            raise TypeError(f"{name} must be a str.")
        try:
            SetupQualityClassification(value)
        except ValueError as exc:
            raise ValueError(
                f"{name} must be a SetupQualityClassification member "
                f"(got {value!r}).",
            ) from exc
        if value in ("INCOMPLETE", "UNAVAILABLE"):
            raise ValueError(
                f"{name} must not be INCOMPLETE/UNAVAILABLE (those are "
                "data-gate classifications, not quality caps).",
            )

    @property
    def max_score(self) -> int:
        """Maximum achievable score = sum of weights (= 100)."""
        return (
            self.mtf_alignment_weight
            + self.setup_confirmation_weight
            + self.setup_confluence_weight
            + self.structure_quality_weight
        )

    def classification_for_score(self, score: int) -> str:
        """
        Deterministic quality band from a bounded score in
        ``[0, max_score]``.

        Returns the LOWEST classification whose threshold is met
        (negating the thresholds): ``score < low`` -> REJECTED;
        ``low <= score < medium`` -> LOW; etc.
        """

        if score < self.low_threshold:
            return "REJECTED"
        if score < self.medium_threshold:
            return "LOW"
        if score < self.high_threshold:
            return "MEDIUM"
        if score < self.excellent_threshold:
            return "HIGH"
        return "EXCELLENT"

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable config snapshot (sorted)."""

        return tuple(sorted(
            (
                ("mtf_alignment_weight", str(self.mtf_alignment_weight)),
                ("setup_confirmation_weight", str(self.setup_confirmation_weight)),
                ("setup_confluence_weight", str(self.setup_confluence_weight)),
                ("structure_quality_weight", str(self.structure_quality_weight)),
                ("max_score", str(self.max_score)),
                ("excellent_threshold", str(self.excellent_threshold)),
                ("high_threshold", str(self.high_threshold)),
                ("medium_threshold", str(self.medium_threshold)),
                ("low_threshold", str(self.low_threshold)),
                ("watch_max_classification", self.watch_max_classification),
                ("no_setup_classification", self.no_setup_classification),
                ("conflict_max_classification", self.conflict_max_classification),
                ("stale_data_max_classification", self.stale_data_max_classification),
                ("incomplete_mtf_max_classification", self.incomplete_mtf_max_classification),
                ("partial_fraction", f"{self.partial_fraction:.4f}"),
                ("primary_timeframe", self.primary_timeframe or "(lowest-duration)"),
                ("max_qualified", str(self.max_qualified)),
            ),
        ))


__all__ = ["SetupQualityConfig"]