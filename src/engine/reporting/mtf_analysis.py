"""
Multi-timeframe market-state report formatter (Checkpoint 19.4).

Deterministic, stateless text renderer for the 19.4
:class:`engine.models.mtf_analysis.PerSymbolMtfResult` / universe
analysis. Returns ``str`` (no ``print()`` inside). DESCRIPTIVE ONLY —
it reports TIMEFRAME MARKET STATE and TIMEFRAME RELATIONSHIPS; it is
NOT a trading signal, NOT a setup, NOT a prediction and NOT a
recommendation.

Following the 11O-19.3 reporting convention, this formatter is imported
via its full path (NOT re-exported from ``reporting/__init__.py``).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from engine.models.mtf_analysis import (
    MarketStateCompleteness,
    MtfAlignmentState,
    MtfUniverseAnalysis,
    PerSymbolMtfResult,
)


def _alignment_label(alignment: MtfAlignmentState) -> str:
    if alignment is MtfAlignmentState.ALIGNED:
        return "ALIGNED (timeline relationship — NOT a buy signal)"
    if alignment is MtfAlignmentState.CONFLICTING:
        return "CONFLICTING (timeline relationship — NOT a sell / no-trade signal)"
    if alignment is MtfAlignmentState.MIXED:
        return "MIXED (indeterminate relationship)"
    if alignment is MtfAlignmentState.INCOMPLETE:
        return "INCOMPLETE (full relationship unassertable)"
    return "UNAVAILABLE (nothing observable to relate)"


class MtfAnalysisFormatter:
    """Stateless, deterministic renderer for MTF market-state results."""

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

    @staticmethod
    def _session_label(session: Any) -> str:
        if session is None:
            return "unknown"
        return str(getattr(session, "value", session))

    @staticmethod
    def _trend_label(state: Any) -> str:
        return _enum_name(state)

    def format_timeframe_state(self, state) -> str:
        """One timeframe's market-state snapshot (deterministic)."""

        market_state = state.market_state
        trend = market_state.trend if market_state is not None else None
        rng = market_state.range if market_state is not None else None
        lines = [
            f"Timeframe          : {state.timeframe}",
            f"Availability       : {state.availability.value}",
            f"Fresh              : {'yes' if state.is_current_fresh else 'no'}",
            f"Completed candles  : {state.completed_candle_count}",
            f"Latest completed   : "
            f"{state.latest_completed_timestamp.isoformat() if state.latest_completed_timestamp else 'none'}",
            f"Latest close       : "
            f"{state.latest_completed_candle.close if state.latest_completed_candle is not None else 'none'}",
            f"Market state       : "
            f"{'available' if state.market_state_available else 'UNAVAILABLE'}",
            f"Trend              : "
            f"{trend.state.value if trend is not None else 'n/a'}",
            f"Range              : "
            f"{rng.state.value if rng is not None else 'n/a'}",
            f"Direction (desc.)  : {state.direction.value}",
        ]
        if state.reason:
            lines.append("Reason")
            lines.append(self._wrap("  " + state.reason, indent="  "))
        return "\n".join(lines)

    def format_symbol(self, result: PerSymbolMtfResult) -> str:
        """One instrument's complete MTF market-state report."""

        lines = [
            "MULTI-TIMEFRAME MARKET STATE — PER SYMBOL",
            "",
            f"Instrument         : {result.instrument}",
            f"Completeness       : {result.completeness.value}",
            f"Alignment          : {_alignment_label(result.alignment)}",
            f"Timeframes         : {', '.join(result.timeframes)}",
        ]
        if result.reference_now is not None:
            lines.append(
                f"Reference now      : {result.reference_now.isoformat()}",
            )
        if result.market_session is not None:
            lines.append(
                f"Market session     : {self._session_label(result.market_session)}",
            )
        lines.append("")
        if result.timeframe_states:
            lines.append("TIMEFRAME STATES")
            lines.append("")
            for idx, state in enumerate(result.timeframe_states, start=1):
                lines.append(f"--- Timeframe {idx}/{len(result.timeframe_states)} ---")
                lines.append(self.format_timeframe_state(state))
                lines.append("")
        lines.append("ALIGNMENT REASON")
        lines.append(self._wrap("  " + (result.alignment_reason or result.error or "n/a")))
        lines.append("")
        lines.append(
            "DISCLAIMER: this MTF result reports timeline market state and "
            "timeline relationships only. ALIGNED/CONFLICTING/MIXED are "
            "TIME FRAME RELATIONSHIP classifications — they are NOT trade "
            "signals, NOT setups, NOT predictions, and NOT a "
            "recommendation.",
        )
        return "\n".join(lines)

    def format_universe(self, analysis: MtfUniverseAnalysis) -> str:
        """The complete universe-wide MTF analysis report."""

        counts = analysis.counts
        lines = [
            "MULTI-TIMEFRAME MARKET-STATE ANALYSIS — UNIVERSE",
            "",
            f"Analysis id        : {analysis.analysis_id}",
            f"Reference now      : {analysis.reference_now.isoformat()}",
            f"Timeframes         : {', '.join(analysis.timeframes)}",
            f"Universe size      : {analysis.universe_instrument_count}",
            f"Market session     : {self._session_label(analysis.market_session)}",
            "",
            "COMPLETENESS COUNTS",
            f"  complete                 : {counts.complete}",
            f"  incomplete               : {counts.incomplete}",
            f"  unsupported              : {counts.unsupported}",
            f"  unavailable              : {counts.unavailable}",
            f"  completeness ratio       : {counts.completeness_ratio:.2%}",
            "",
            "ALIGNMENT / RELATIONSHIP COUNTS",
            f"  aligned                  : {counts.aligned}",
            f"  mixed                    : {counts.mixed}",
            f"  conflicting              : {counts.conflicting}",
            f"  alignment_incomplete     : {counts.alignment_incomplete}",
            f"  alignment_unavailable    : {counts.alignment_unavailable}",
            "",
        ]
        if analysis.results:
            lines.append("PER-SYMBOL MTF RESULT")
            header = (
                f"  {'INSTRUMENT':<22} {'COMPLETE':<12} {'ALIGNMENT':<14} "
                f"15m-dir 1h-dir"
            )
            lines.append(header)
            for result in analysis.results:
                line = (
                    f"  {result.instrument:<22} "
                    f"{result.completeness.value:<12} "
                    f"{result.alignment.value:<14} "
                )
                for state in result.timeframe_states:
                    dir_label = (
                        f"{state.timeframe}={state.direction.value}"
                    )
                    line += f"{dir_label:<8} "
                lines.append(line.rstrip())
            lines.append("")
        lines.append(
            "DISCLAIMER: this MTF universe analysis reports timeline "
            "market state and timeline relationships only. It is NOT a "
            "trade scan, NOT a setup/opportunity list, NOT a prediction, "
            "and does NOT authorize any broker execution.",
        )
        return "\n".join(lines)

    def format_summary(self, analysis: MtfUniverseAnalysis) -> str:
        """Compact one-line summary (for CLIs / logs)."""

        counts = analysis.counts
        parts = [
            f"id={analysis.analysis_id}",
            f"timeframes={','.join(analysis.timeframes)}",
            f"tested={counts.tested}/{analysis.universe_instrument_count}",
            f"complete={counts.complete}",
            f"incomplete={counts.incomplete}",
            f"unsupported={counts.unsupported}",
            f"unavailable={counts.unavailable}",
            f"aligned={counts.aligned}",
            f"mixed={counts.mixed}",
            f"conflicting={counts.conflicting}",
            f"completeness_ratio={counts.completeness_ratio:.2f}",
        ]
        return "MTF SUMMARY " + " ".join(parts)


def _enum_name(value: Any) -> str:
    """
    Late-bound enum-value helper kept OUT of the hot path (a dataclass
    may carry a plain str ``reason`` field; only states are enums).
    """

    from enum import Enum

    return value.value if isinstance(value, Enum) else str(value)


__all__ = [
    "MtfAnalysisFormatter",
]