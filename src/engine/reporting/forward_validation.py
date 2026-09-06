"""
Validation / forward-testing reporting (Checkpoint 19.9).

A stateless, deterministic, human-readable REPORT formatter for the
forward-validation framework. It returns ``str`` (never ``print``) and
report every dimension SEPARATELY (data reliability / scanner / MTF /
setup quality / ranking / lifecycle / alerts / outcomes / sample size) —
it never collapses the assessment into one "accuracy" number and never
claims predictive validity or profitability.

The formatter is imported via full path (``reporting/__init__.py`` is
NOT extended, matching the 11O-19.8 reporting convention).
"""

from __future__ import annotations

from typing import Sequence

from engine.models.forward_validation import (
    ForwardObservation,
    ForwardOutcome,
    ForwardSession,
    ForwardValidationReport,
)

#: Fixed, non-configurable disclaimer (descriptive-only).
FORWARD_VALIDATION_DISCLAIMER = (
    "Forward validation is a descriptive engineering measurement: it "
    "records what the system observed at each instant and what the "
    "market subsequently did. It does NOT predict future performance, "
    "does NOT guarantee profitability, and does NOT constitute a "
    "trading recommendation. The user is the final execution boundary."
)


class ForwardValidationFormatter:
    """Deterministic forward-validation report formatting."""

    def __init__(
        self,
        precision: int = 4,
        width: int = 78,
    ) -> None:
        if isinstance(precision, bool) or not isinstance(precision, int):
            raise TypeError("precision must be an int, not bool.")
        if precision < 0:
            raise ValueError("precision must be non-negative.")
        if isinstance(width, bool) or not isinstance(width, int):
            raise TypeError("width must be an int, not bool.")
        if width < 20:
            raise ValueError("width must be >= 20.")
        self.precision = precision
        self.width = width

    # ------------------------------------------------------------
    # SESSION
    # ------------------------------------------------------------

    def format_session(self, session: ForwardSession) -> str:
        lines: list[str] = []
        lines.append("=" * self.width)
        lines.append("FORWARD-VALIDATION SESSION")
        lines.append("=" * self.width)
        lines.append(f"session_id            : {session.session_id}")
        lines.append(f"provider              : {session.provider}")
        lines.append(f"timeframes            : {', '.join(session.timeframes)}")
        lines.append(f"primary timeframe     : {session.primary_timeframe}")
        lines.append(f"universe size         : {len(session.universe)}")
        lines.append(f"universe version      : {session.universe_version or 'unavailable'}")
        lines.append(f"started_at            : {session.started_at.isoformat()}")
        lines.append(
            f"ended_at              : "
            f"{session.ended_at.isoformat() if session.ended_at else 'open'}"
        )
        lines.append(f"status                : {session.status}")
        lines.append(f"observations          : {session.counts.observations}")
        lines.append(f"outcomes              : {session.counts.outcomes}")
        lines.append(f"alerts                : {session.counts.alerts}")
        lines.append(f"alerts delivered      : {session.counts.alerts_delivered}")
        lines.append(f"alerts failed         : {session.counts.alerts_failed}")
        lines.append(f"alerts suppressed     : {session.counts.alerts_suppressed}")
        if session.policy_versions:
            lines.append("policy versions       :")
            for name, value in session.policy_versions:
                lines.append(f"  {name} = {value or 'unavailable'}")
        lines.append("")
        lines.append(FORWARD_VALIDATION_DISCLAIMER)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # OBSERVATION
    # ------------------------------------------------------------

    def format_observation(self, observation: ForwardObservation) -> str:
        lines: list[str] = []
        lines.append("=" * self.width)
        lines.append("FORWARD OBSERVATION (point-in-time)")
        lines.append("=" * self.width)
        lines.append(f"observation_id        : {observation.observation_id}")
        lines.append(f"setup_id              : {observation.setup_id or 'unavailable'}")
        lines.append(f"lifecycle_id          : {observation.lifecycle_id or 'unavailable'}")
        lines.append(f"instrument            : {observation.instrument}")
        lines.append(f"direction             : {observation.direction or 'unavailable'}")
        lines.append(f"setup_type            : {observation.setup_type or 'unavailable'}")
        lines.append(f"primary timeframe     : {observation.primary_timeframe or 'unavailable'}")
        lines.append(f"observed at           : {observation.observation_timestamp.isoformat()}")
        lines.append(f"scan_cycle_id         : {observation.scan_cycle_id or 'unavailable'}")
        lines.append(f"lifecycle state       : {observation.lifecycle_state.name}")
        lines.append(
            "lifecycle obs status  : "
            f"{observation.lifecycle_observation_status.name}"
        )
        lines.append(f"quality status        : {observation.quality_status.name}")
        lines.append(
            "quality classification: "
            f"{observation.quality_classification.name}"
        )
        lines.append(f"quality score         : {_fmt(observation.quality_score)}")
        lines.append(f"MTF alignment         : {observation.mtf_alignment}")
        lines.append(f"MTF completeness      : {observation.mtf_completeness}")
        lines.append(f"alert state           : {observation.alert_state or 'unavailable'}")
        lines.append(f"alert id              : {observation.alert_id or 'unavailable'}")
        lines.append(f"reference price       : {_fmt(observation.reference_price)}")
        lines.append(f"record status         : {observation.status.name}")
        if observation.reason:
            lines.append(f"reason                : {observation.reason}")
        lines.append("")
        lines.append(
            "This observation records ONLY information available at the "
            "observation timestamp."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------
    # OUTCOME
    # ------------------------------------------------------------

    def format_outcome(self, outcome: ForwardOutcome) -> str:
        lines: list[str] = []
        lines.append("=" * self.width)
        lines.append("FORWARD OUTCOME (measured after T)")
        lines.append("=" * self.width)
        lines.append(f"outcome_id            : {outcome.outcome_id}")
        lines.append(f"observation_id        : {outcome.observation_id}")
        lines.append(f"instrument            : {outcome.instrument}")
        lines.append(f"direction             : {outcome.direction or 'unavailable'}")
        lines.append(f"availability          : {outcome.availability.name}")
        lines.append(f"forward return        : {_fmt(outcome.forward_return)}")
        lines.append(
            "max favorable         : "
            f"{_fmt(outcome.max_favorable_movement)}"
        )
        lines.append(
            "max adverse           : "
            f"{_fmt(outcome.max_adverse_movement)}"
        )
        lines.append(
            "direction consistent  : "
            f"{'yes' if outcome.direction_consistent is True else 'no' if outcome.direction_consistent is False else 'unavailable'}"
        )
        lines.append(f"outcome direction     : {outcome.outcome_direction.name}")
        lines.append(f"bars available        : {outcome.bars_available}")
        lines.append(f"bars used             : {outcome.bars_used}")
        lines.append(f"horizon (bars)        : {outcome.horizon_bars}")
        lines.append(
            f"window complete       : {'yes' if outcome.window_complete else 'no'}"
        )
        lines.append(f"measured at           : {outcome.measurement_timestamp.isoformat()}")
        if outcome.reason:
            lines.append(f"reason                : {outcome.reason}")
        lines.append("")
        lines.append(
            "This outcome was measured ONLY from completed primary "
            "candles strictly after the observation timestamp."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------
    # REPORT
    # ------------------------------------------------------------

    def format_report(
        self,
        report: ForwardValidationReport,
        *,
        observations: Sequence[ForwardObservation] | None = None,
        outcomes: Sequence[ForwardOutcome] | None = None,
    ) -> str:
        del observations, outcomes  # report carries the authoritative views
        lines: list[str] = []
        lines.append("=" * self.width)
        lines.append("FORWARD-VALIDATION REPORT (Checkpoint 19.9)")
        lines.append("=" * self.width)
        lines.append(f"report_id             : {report.report_id}")
        lines.append(
            "sessions              : "
            f"{', '.join(report.session_ids) if report.session_ids else 'none'}"
        )
        lines.append(f"universe size         : {report.universe_instrument_count}")
        lines.append(f"instruments attempted: {report.instruments_attempted}")
        lines.append(
            "instruments usable    : "
            f"{report.instruments_with_usable_data}"
        )
        lines.append(f"unsupported           : {report.unsupported_instruments}")
        lines.append(f"provider failures     : {report.provider_failures}")
        lines.append(f"successful scans      : {report.successful_scans}")
        lines.append(
            "incomplete MTF        : "
            f"{report.incomplete_mtf_analyses}"
        )
        lines.append("")
        lines.append("SETUP-QUALITY DISTRIBUTION")
        lines.append("-" * self.width)
        for name, count in report.classification_counts:
            lines.append(f"  {name:<12} {count}")
        if not report.classification_counts:
            lines.append("  (no observations recorded)")
        lines.append("")
        lines.append("SETUP TYPE / DIRECTION / MTF / LIFECYCLE")
        lines.append("-" * self.width)
        _render_counts(lines, "setup_type", report.setup_type_counts)
        _render_counts(lines, "direction", report.direction_counts)
        _render_counts(lines, "mtf_alignment", report.mtf_alignment_counts)
        _render_counts(lines, "lifecycle", report.lifecycle_counts)
        lines.append("")
        lines.append("ALERTS")
        lines.append("-" * self.width)
        lines.append(f"  generated   {report.alerts_generated}")
        lines.append(f"  delivered   {report.alerts_delivered}")
        lines.append(f"  suppressed  {report.alerts_suppressed}")
        lines.append("")
        lines.append("FORWARD OUTCOMES (measured after T)")
        lines.append("-" * self.width)
        _render_counts(lines, "availability", report.outcome_counts)
        _render_counts(lines, "outcome_direction", report.outcome_direction_counts)
        _render_counts(lines, "consistency", report.outcome_consistency_counts)
        if report.outcomes:
            returns = [
                o.forward_return
                for o in report.outcomes
                if o.forward_return is not None
            ]
            if returns:
                avg = sum(returns) / len(returns)
                lines.append(
                    f"  average forward return : {avg:.{self.precision}f} "
                    f"({len(returns)} measured)"
                )
            favorable = sum(
                1 for o in report.outcomes
                if o.outcome_direction is not None
                and o.outcome_direction.name == "FAVORABLE"
            )
            unfavorable = sum(
                1 for o in report.outcomes
                if o.outcome_direction is not None
                and o.outcome_direction.name == "UNFAVORABLE"
            )
            neutral = sum(
                1 for o in report.outcomes
                if o.outcome_direction is not None
                and o.outcome_direction.name == "NEUTRAL"
            )
            lines.append(
                f"  favorable {favorable} / unfavorable {unfavorable} / "
                f"neutral {neutral}"
            )
        lines.append("")
        lines.append("OPERATIONAL FAILURES (reused 19.8 vocabulary)")
        lines.append("-" * self.width)
        _render_counts(lines, "failure", report.failure_counts)
        lines.append("")
        lines.append("SAMPLE-SIZE NOTE")
        lines.append("-" * self.width)
        lines.append(f"  {report.sample_size_note}")
        lines.append("")
        lines.append("RATIONALE")
        lines.append("-" * self.width)
        lines.append(f"  {report.rationale}")
        lines.append("")
        lines.append(FORWARD_VALIDATION_DISCLAIMER)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # RAW DISTRIBUTIONS (for per-dimension reporting)
    # ------------------------------------------------------------

    def format_distribution(
        self,
        title: str,
        counts: Sequence[tuple[str, int]],
    ) -> str:
        lines: list[str] = [title, "-" * self.width]
        _render_counts(lines, title, counts)
        return "\n".join(lines)


def _render_counts(
    lines: list[str],
    label: str,
    counts: Sequence[tuple[str, int]],
) -> None:
    if not counts:
        lines.append(f"  {label:<12} (none recorded)")
        return
    for name, count in counts:
        lines.append(f"  {name:<12} {count}")


def _fmt(value: float | int | None) -> str:
    if value is None:
        return "unavailable"
    return f"{float(value):.6f}"


__all__ = [
    "FORWARD_VALIDATION_DISCLAIMER",
    "ForwardValidationFormatter",
]