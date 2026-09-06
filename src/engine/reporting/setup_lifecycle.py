"""
Setup lifecycle report formatter (Checkpoint 19.6).

Deterministic, stateless text renderers for the 19.6 lifecycle models:

    * :class:`engine.models.setup_lifecycle.SetupLifecycle`
      (one lifecycle snapshot —
      :meth:`SetupLifecycleFormatter.format_lifecycle`)
    * :class:`engine.models.setup_lifecycle.SetupLifecycleCycleResult`
      (one universe-wide lifecycle-processing cycle —
      :meth:`SetupLifecycleFormatter.format_cycle`)
    * :class:`engine.models.setup_lifecycle.SetupLifecycleObservationResult`
      (one per-symbol processing outcome —
      :meth:`SetupLifecycleFormatter.format_observation`)

Returns ``str`` (no ``print()`` inside). DESCRIPTIVE ONLY — it reports
lifecycle history / state, NOT a trade signal, NOT a prediction, NOT a
guarantee of profitability, and NOT a trading recommendation. Every
report ends with the explicit warning that lifecycle state is a
temporal-continuity classification, not a trade/execution verdict and
not a prediction.

Following the 11O-19.5 reporting convention, this formatter is imported
via its full path (NOT re-exported from ``reporting/__init__.py``).
"""

from __future__ import annotations

from engine.models.setup_lifecycle import (
    LifecycleObservationStatus,
    SetupLifecycle,
    SetupLifecycleCycleResult,
    SetupLifecycleObservationResult,
    SetupLifecycleState,
)

_WARNING = (
    "WARNING: Setup lifecycle state is a temporal-continuity "
    "classification of a setup candidate across scan cycles. It is NOT "
    "a trade signal, does NOT predict profitability, and does NOT "
    "constitute a trading recommendation. No trade execution, order "
    "placement, alert, or setup lifecycle persistence is performed by "
    "this analysis."
)


def _state_label(state: SetupLifecycleState) -> str:
    if state.is_terminal:
        return f"{state.value} (terminal)"
    return f"{state.value} (active)"


def _obs_status_label(status: LifecycleObservationStatus) -> str:
    if status is LifecycleObservationStatus.OBSERVED:
        return "OBSERVED (setup identity present)"
    if status is LifecycleObservationStatus.ABSENT:
        return "ABSENT (clean non-observation)"
    if status is LifecycleObservationStatus.DATA_UNAVAILABLE:
        return "DATA_UNAVAILABLE (data failure — never invalidation)"
    if status is LifecycleObservationStatus.SUPERSEDED:
        return "SUPERSEDED (opposing-direction setup — invalidated)"
    return "LATE_REJECTED (out-of-order observation)"


class SetupLifecycleFormatter:
    """Stateless, deterministic renderer for setup lifecycle results."""

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

    # ------------------------------------------------------------
    # SINGLE LIFECYCLE SNAPSHOT
    # ------------------------------------------------------------

    def format_lifecycle(self, lifecycle: SetupLifecycle) -> str:
        """Render ONE lifecycle snapshot (full history)."""

        lines = [
            "SETUP LIFECYCLE — SNAPSHOT",
            "",
            f"Lifecycle id       : {lifecycle.lifecycle_id}",
            f"Setup id           : {lifecycle.setup_id}",
            f"Instrument         : {lifecycle.instrument}",
            f"Direction          : {lifecycle.direction.value}",
            f"Setup type         : {lifecycle.setup_type}",
            f"Primary timeframe  : {lifecycle.primary_timeframe}",
            f"Instance           : #{lifecycle.instance_discriminator}",
            f"State              : {_state_label(lifecycle.state)}",
            f"Created at         : {lifecycle.created_at.isoformat()}",
            f"Last observation   : "
            f"{(lifecycle.last_observation_timestamp.isoformat() if lifecycle.last_observation_timestamp else 'unavailable')}",
            f"Consecutive misses : {lifecycle.consecutive_missing}",
        ]
        if lifecycle.latest_status is not None:
            lines.append(
                f"Latest quality     : "
                f"{lifecycle.latest_status} / "
                f"{lifecycle.latest_classification or 'unavailable'} / "
                f"{(lifecycle.latest_score if lifecycle.latest_score is not None else 'unavailable')}",
            )
        lines.append(
            f"Latest observation : "
            f"{_obs_status_label(lifecycle.latest_observation_status)}",
        )
        lines.append(f"Observations       : {lifecycle.observation_count}")
        lines.append(f"Transitions        : {lifecycle.transition_count}")

        if lifecycle.transitions:
            lines.append("")
            lines.append("TRANSITIONS (explicit, explainable)")
            for t in lifecycle.transitions:
                lines.append(
                    f"  {t.observation_timestamp.isoformat()}  "
                    f"{t.from_state.value} -> {t.to_state.value}",
                )
                for wrapped in self._wrap(
                    f"    reason: {t.reason}", indent="    ",
                ).splitlines():
                    lines.append(wrapped)

        if lifecycle.observations:
            lines.append("")
            lines.append("OBSERVATION HISTORY (immutable, point-in-time)")
            for obs in lifecycle.observations:
                lines.append(
                    f"  {obs.observation_timestamp.isoformat()}  "
                    f"{_obs_status_label(obs.observation_status)}  "
                    f"-> {obs.at_state.value if obs.at_state else 'none'}",
                )
                latest = (
                    f"{obs.quality_status} / "
                    f"{obs.quality_classification or 'unavailable'} / "
                    f"{obs.quality_score if obs.quality_score is not None else 'unavailable'}"
                )
                lines.append(f"    quality: {latest}")
                for wrapped in self._wrap(
                    f"    reason: {obs.reason}", indent="    ",
                ).splitlines():
                    lines.append(wrapped)

        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # PER-SYMBOL OBSERVATION RESULT
    # ------------------------------------------------------------

    def format_observation(
        self, result: SetupLifecycleObservationResult,
    ) -> str:
        """Render ONE per-symbol lifecycle-processing outcome."""

        lines = [
            "SETUP LIFECYCLE — PER SYMBOL PROCESSING",
            "",
            f"Instrument          : {result.instrument}",
            f"Observation time    : {result.observation_timestamp.isoformat()}",
            f"Scan cycle          : {result.scan_cycle_id}",
            f"Setup id            : {result.setup_id or 'unavailable'}",
            f"Lifecycle id        : {result.lifecycle_id or 'unavailable'}",
            f"State               : "
            f"{(result.state.value if result.state is not None else 'none')}",
            f"Created             : {'yes' if result.created else 'no'}",
            f"Advanced            : {'yes' if result.advanced else 'no'}",
            f"Duplicate           : {'yes' if result.duplicate else 'no'}",
            f"Late rejected       : {'yes' if result.late_rejected else 'no'}",
        ]
        if result.observation_status is not None:
            lines.append(
                f"Observation status  : "
                f"{_obs_status_label(result.observation_status)}",
            )
        if result.transition is not None:
            lines.append(
                f"Transition          : "
                f"{result.transition.from_state.value} -> "
                f"{result.transition.to_state.value}",
            )
        lines.append("")
        for wrapped in self._wrap(
            f"Reason: {result.reason}", indent="",
        ).splitlines():
            lines.append(wrapped)
        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # UNIVERSE-WIDE CYCLE RESULT
    # ------------------------------------------------------------

    def format_cycle(self, result: SetupLifecycleCycleResult) -> str:
        """Render ONE universe-wide lifecycle-processing cycle."""

        counts = result.counts
        lines = [
            "SETUP LIFECYCLE — UNIVERSE-WIDE PROCESSING CYCLE",
            "",
            f"Cycle id            : {result.cycle_id}",
            f"Scan cycle          : {result.scan_cycle_id}",
            f"Reference now       : {result.reference_now.isoformat()}",
            f"Instruments         : {len(result.instruments)}",
            f"Processed           : {counts.processed}",
            f"  created           : {counts.created}",
            f"  advanced          : {counts.advanced}",
            f"  duplicate         : {counts.duplicate}",
            f"  late rejected     : {counts.late_rejected}",
            f"LIVE cycles         : {counts.active}",
            f"  detected          : {counts.detected}",
            f"  confirmed         : {counts.confirmed}",
            f"TERMINAL cycles     : {counts.terminal}",
            f"  invalidated       : {counts.invalidated}",
            f"  expired           : {counts.expired}",
            f"Observation status  : "
            f"observed={counts.observed} absent={counts.absent} "
            f"data_unavailable={counts.data_unavailable} "
            f"superseded={counts.superseded}",
        ]

        if result.results:
            lines.append("")
            lines.append("PER-SYMBOL OUTCOMES")
            seen_symbols = _ordered_symbols(result)
            for instrument in seen_symbols:
                outcomes = tuple(
                    r for r in result.results
                    if r.instrument == instrument
                )
                primary = outcomes[0]
                state = primary.state.value if primary.state else "none"
                lines.append(
                    f"  {instrument:<12} state={state:<11} "
                    f"setup={primary.setup_id or 'none'}",
                )
                for extra in outcomes[1:]:
                    lines.append(
                        f"  {'':<12} ({extra.setup_id or 'none'} -> "
                        f"{(extra.state.value if extra.state else 'none')})",
                    )

        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines)


def _ordered_symbols(result: SetupLifecycleCycleResult) -> tuple[str, ...]:
    """Deterministic instrument order (canonical ascending)."""

    return tuple(sorted({r.instrument for r in result.results}))


#: Re-export for the operator CLI / demo.
LIFECYCLE_FORMATTER_TYPES = (
    SetupLifecycleFormatter,
)


__all__ = [
    "LIFECYCLE_FORMATTER_TYPES",
    "SetupLifecycleFormatter",
]