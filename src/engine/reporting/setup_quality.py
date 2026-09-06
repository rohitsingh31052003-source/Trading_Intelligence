"""
Setup-quality intelligence report formatter (Checkpoint 19.5).

Deterministic, stateless text renderer for the 19.5
:class:`engine.models.setup_quality.SetupQualityResult` /
:class:`SetupQualityUniverseResult`. Returns ``str`` (no ``print()``
inside). DESCRIPTIVE ONLY — it reports SETUP QUALITY, NOT a trade
signal, NOT a prediction, NOT a guarantee of profitability, and NOT a
trading recommendation.

Every report ends with the explicit warning that setup-quality
classifications are descriptive technical evidence, not predictions or
guarantees of profitability.

Following the 11O-19.4 reporting convention, this formatter is imported
via its full path (NOT re-exported from ``reporting/__init__.py``).
"""

from __future__ import annotations

from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityResult,
    SetupQualityUniverseResult,
)

_WARNING = (
    "WARNING: Setup-quality classifications are descriptive technical "
    "evidence. A QUALIFIED setup is a coherent candidate for further "
    "evaluation — it is NOT a trade signal, does NOT predict "
    "profitability, and does NOT constitute a trading recommendation. "
    "No trade execution, order placement, alert, or setup lifecycle is "
    "performed by this analysis."
)


def _classification_label(classification: SetupQualityClassification) -> str:
    if classification.is_qualified_band:
        return (
            f"{classification.value} (quality band — descriptive, not "
            "predictive)"
        )
    if classification is SetupQualityClassification.REJECTED:
        return "REJECTED (below the quality bar / no setup detected)"
    if classification is SetupQualityClassification.INCOMPLETE:
        return "INCOMPLETE (data insufficient — never a setup)"
    return "UNAVAILABLE (no usable data — never a setup)"


class SetupQualityFormatter:
    """Stateless, deterministic renderer for setup-quality results."""

    def __init__(self, width: int = 80) -> None:
        if width < 1:
            raise ValueError("width must be >= 1.")
        self.width = width

    def _wrap(self, text: str, indent: str = "") -> str:
        words = text.split()
        if not words:
            return ""
        lines: list[str] = []
        current = indent
        for word in words:
            if current == indent:
                current = indent + word
            elif len(current) + 1 + len(word) <= self.width:
                current += " " + word
            else:
                lines.append(current)
                current = indent + word
        if current:
            lines.append(current)
        return "\n".join(lines)

    def format_result(self, result: SetupQualityResult) -> str:
        """Render ONE symbol's setup-quality assessment."""

        lines = [
            "SETUP-QUALITY INTELLIGENCE — PER SYMBOL",
            "",
            f"Instrument          : {result.instrument}",
            f"Reference now       : {result.reference_now.isoformat()}",
            f"Primary timeframe   : {result.primary_timeframe}",
            f"MTF alignment       : {result.mtf.alignment.value}",
            f"MTF completeness    : {result.mtf.completeness.value}",
            f"Status              : {result.status.value}",
            f"Classification      : "
            f"{_classification_label(result.classification)}",
        ]
        if result.score is not None:
            lines.append(f"Score               : {result.score}/100")
        if result.setup_classification is not None:
            lines.append(
                f"Setup (11Q)         : {result.setup_classification.value}",
            )
        if result.setup_direction is not None:
            lines.append(f"Setup direction     : {result.setup_direction.value}")
        if result.setup_type is not None:
            lines.append(f"Setup type          : {result.setup_type}")
        if result.confluence_score is not None:
            lines.append(f"Confluence (11Q)    : {result.confluence_score}/5")
        lines.append(f"Conflict present    : {'yes' if result.has_conflict else 'no'}")
        lines.append(f"Data complete       : {'yes' if result.data_complete else 'no'}")
        lines.append(f"Stale primary data  : {'yes' if result.stale_data else 'no'}")

        if result.score_components:
            lines.append("")
            lines.append("SCORE COMPONENTS")
            lines.append("")
            for component in result.score_components:
                lines.append(
                    f"  {component.name:<20} {component.points:>3}/"
                    f"{component.max_points:<3} {component.reason}",
                )
        if result.positive_factors:
            lines.append("")
            lines.append("POSITIVE EVIDENCE")
            lines.append("  " + ", ".join(result.positive_factors))
        if result.negative_factors:
            lines.append("")
            lines.append("NEGATIVE EVIDENCE")
            lines.append("  " + ", ".join(result.negative_factors))
        if result.explanation:
            lines.append("")
            lines.append("EXPLANATION")
            lines.append(self._wrap("  " + result.explanation, indent="  "))
        if result.reason:
            lines.append("")
            lines.append("REASON")
            lines.append(self._wrap("  " + result.reason, indent="  "))
        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines).rstrip() + "\n"

    def format_universe(self, result: SetupQualityUniverseResult) -> str:
        """Render the universe-wide setup-quality analysis."""

        counts = result.counts
        lines = [
            "SETUP-QUALITY INTELLIGENCE — UNIVERSE",
            "",
            f"Analysis id          : {result.analysis_id}",
            f"Reference now        : {result.reference_now.isoformat()}",
            f"Timeframes           : {', '.join(result.timeframes)}",
            f"Primary timeframe    : {result.primary_timeframe}",
            f"Universe instruments : {result.universe_instrument_count}",
            f"Max qualified (top-N): {result.max_qualified if result.max_qualified is not None else 'all'}",
            "",
            "STATUS DISTRIBUTION",
            f"  QUALIFIED   {counts.qualified}",
            f"  WATCH       {counts.watch}",
            f"  NO_SETUP    {counts.no_setup}",
            f"  INCOMPLETE  {counts.incomplete}",
            f"  UNAVAILABLE {counts.unavailable}",
            f"  TESTED      {counts.tested}",
            f"  QUALIFIED RATIO {counts.qualified_ratio:.4f} (partial coverage "
            "is never reported as full)",
            "",
            "CLASSIFICATION DISTRIBUTION",
            f"  EXCELLENT {counts.excellent}   HIGH {counts.high}   "
            f"MEDIUM {counts.medium}   LOW {counts.low}   "
            f"REJECTED {counts.rejected}",
        ]
        if result.ranked_qualified:
            lines.append("")
            lines.append("RANKED QUALIFIED CANDIDATES (strongest-first)")
            for rank, candidate in enumerate(
                result.top_n or result.ranked_qualified, start=1,
            ):
                lines.append(
                    f"  #{rank:<3} {candidate.instrument:<12} "
                    f"{candidate.classification.value:<9} "
                    f"score {candidate.score:>3}  "
                    f"type {candidate.setup_type or 'n/a':<22} "
                    f"dir {candidate.setup_direction.value if candidate.setup_direction else 'n/a'}",
                )
        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines).rstrip() + "\n"


__all__ = ["SetupQualityFormatter"]