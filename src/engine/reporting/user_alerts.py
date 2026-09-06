"""
User alerts report formatters (Checkpoint 19.7).

Deterministic, stateless renderers for the 19.7 alert models:

    * :meth:`AlertFormatter.format_alert` — ONE user-facing alert
      (human-readable text);
    * :meth:`AlertFormatter.format_cycle` — ONE alert-processing cycle
      (universe-wide view incl. suppression audit);
    * :meth:`AlertFormatter.alert_to_json` — the machine-readable
      deterministic JSON projection of one alert;
    * :meth:`AlertFormatter.cycle_to_json` — the machine-readable
      deterministic JSON projection of one alert cycle.

Returns ``str`` (no ``print()`` inside). DESCRIPTIVE ONLY — an alert
informs the user that a meaningful lifecycle EVENT happened; it is NOT
a trade signal, NOT a prediction, NOT a guarantee of profitability and
NOT a trading recommendation. It NEVER contains entry/stop/target,
quantity, position size, risk/reward, order type or any execution
directive.

Every report ends with the explicit warning that alert events are
descriptive lifecycle notifications, not trade signals and not
predictions.

Following the 11O-19.6 reporting convention, this formatter is imported
via its full path (NOT re-exported from ``reporting/__init__.py``).
"""

from __future__ import annotations

import json

from engine.models.user_alerts import (
    AlertCycleResult,
    AlertDeliveryStatus,
    AlertEvent,
    AlertEventKind,
)

_WARNING = (
    "WARNING: Alert events are descriptive lifecycle notifications. "
    "They inform the user that a lifecycle event happened for a setup "
    "candidate — they are NOT trade signals, do NOT predict "
    "profitability, and do NOT constitute a trading recommendation. "
    "No trade execution, order placement, or external notification is "
    "performed by this analysis."
)

_KIND_LABEL = {
    AlertEventKind.SETUP_DETECTED: "SETUP DETECTED",
    AlertEventKind.SETUP_CONFIRMED: "SETUP CONFIRMED",
    AlertEventKind.SETUP_INVALIDATED: "SETUP INVALIDATED",
    AlertEventKind.SETUP_EXPIRED: "SETUP EXPIRED",
}


def _optional(value):
    return "unavailable" if value is None else value


class AlertFormatter:
    """Stateless, deterministic renderer for user alerts."""

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
    # HUMAN-READABLE ALERT
    # ------------------------------------------------------------

    def format_alert(self, alert: AlertEvent) -> str:
        """Render ONE user-facing alert (text form)."""

        lines = [
            _KIND_LABEL[alert.kind],
            "",
            f"Symbol                : {alert.instrument}",
            f"Direction             : {alert.direction}",
            f"Setup type            : {alert.setup_type}",
            f"Primary timeframe     : {alert.primary_timeframe}",
            f"Lifecycle state       : {alert.lifecycle_state.value}",
        ]
        if alert.previous_lifecycle_state is not None:
            lines.append(
                f"Previous state        : "
                f"{alert.previous_lifecycle_state.value}",
            )
        if alert.quality_score is not None:
            lines.append(
                f"Quality               : "
                f"{_optional(alert.quality_status)} / "
                f"{_optional(alert.quality_classification)} / "
                f"{alert.quality_score}",
            )
        if alert.mtf_alignment is not None:
            lines.append(
                f"MTF                   : "
                f"{alert.mtf_alignment} / {_optional(alert.mtf_completeness)}",
            )
        if alert.setup_classification is not None:
            lines.append(
                f"Setup (11Q)           : {alert.setup_classification}",
            )
        if alert.confluence_score is not None:
            lines.append(f"Confluence (11Q)      : {alert.confluence_score}/5")
        lines.append(f"Conflict present      : {'yes' if alert.has_conflict else 'no'}")
        lines.append(
            f"Observed              : {alert.observation_timestamp.isoformat()}",
        )
        lines.append(f"Reason                : {alert.transition_reason}")
        lines.append("")
        lines.append(f"Alert id              : {alert.alert_id}")
        lines.append(f"Setup id              : {alert.setup_id}")
        lines.append(
            f"Lifecycle id          : {alert.lifecycle_id}",
        )
        lines.append(f"Observation id        : {alert.observation_id}")
        lines.append(f"Scan cycle            : {alert.scan_cycle_id}")
        lines.append(f"Severity              : {alert.severity.value}")
        lines.append(f"Policy version        : {alert.policy_version}")
        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # HUMAN-READABLE CYCLE
    # ------------------------------------------------------------

    def format_cycle(self, result: AlertCycleResult) -> str:
        """Render ONE universe-wide alert-processing cycle."""

        counts = result.counts
        lines = [
            "USER ALERTS — UNIVERSE-WIDE PROCESSING CYCLE",
            "",
            f"Cycle id              : {result.cycle_id}",
            f"Lifecycle cycle       : {result.lifecycle_cycle_id}",
            f"Scan cycle            : {result.scan_cycle_id}",
            f"Reference now         : {result.reference_now.isoformat()}",
            f"Policy version        : {result.policy_version}",
            f"Instruments           : {len(result.instruments)}",
            f"Events examined       : {counts.eligible}",
            f"  emitted             : {counts.emitted}",
            f"  delivered           : {counts.delivered}",
            f"  failed              : {counts.failed}",
            f"  skipped (no channel): {counts.skipped}",
            f"  suppressed          : {counts.suppressed}",
        ]
        if counts.by_kind:
            lines.append(
                "Delivered by kind    : "
                + ", ".join(f"{k}={n}" for k, n in counts.by_kind),
            )
        if result.alerts:
            lines.append("")
            lines.append("ALERTS")
            for alert in result.alerts:
                lines.append(
                    f"  [{alert.severity.value:<9}] {_KIND_LABEL[alert.kind]} "
                    f"{alert.instrument} {alert.direction} "
                    f"{alert.setup_type} @ {alert.observation_timestamp.isoformat()}",
                )
                lines.append(f"      alert {alert.alert_id}")
        if result.suppressed:
            lines.append("")
            lines.append("SUPPRESSED (explicit, auditable)")
            for entry in result.suppressed:
                lines.append(
                    f"  {entry.kind.value} {entry.instrument} "
                    f"@ {entry.observation_timestamp.isoformat()}",
                )
                for wrapped in self._wrap(
                    f"    {entry.reason}", indent="    ",
                ).splitlines():
                    lines.append(wrapped)
        lines.append("")
        lines.append(_WARNING)
        return "\n".join(lines)

    # ------------------------------------------------------------
    # JSON PROJECTIONS
    # ------------------------------------------------------------

    def alert_to_json(self, alert: AlertEvent) -> str:
        """Deterministic machine-readable JSON for one alert."""

        return json.dumps(
            self.alert_to_dict(alert), indent=2, sort_keys=True,
        )

    def alert_to_dict(self, alert: AlertEvent) -> dict:
        """Deterministic JSON-able projection of one alert."""

        data = {
            "alert_id": alert.alert_id,
            "kind": alert.kind.value,
            "severity": alert.severity.value,
            "lifecycle_id": alert.lifecycle_id,
            "setup_id": alert.setup_id,
            "instrument": alert.instrument,
            "lifecycle_state": alert.lifecycle_state.value,
            "previous_lifecycle_state": (
                alert.previous_lifecycle_state.value
                if alert.previous_lifecycle_state is not None
                else None
            ),
            "transition_reason": alert.transition_reason,
            "setup_type": alert.setup_type,
            "direction": alert.direction,
            "primary_timeframe": alert.primary_timeframe,
            "observation_id": alert.observation_id,
            "observation_timestamp": alert.observation_timestamp.isoformat(),
            "scan_cycle_id": alert.scan_cycle_id,
            "observation_status": alert.observation_status.value,
            "policy_version": alert.policy_version,
            "quality_status": alert.quality_status,
            "quality_classification": alert.quality_classification,
            "quality_score": alert.quality_score,
            "mtf_alignment": alert.mtf_alignment,
            "mtf_completeness": alert.mtf_completeness,
            "setup_classification": alert.setup_classification,
            "confluence_score": alert.confluence_score,
            "has_conflict": alert.has_conflict,
            "reason": alert.reason,
        }
        return data

    def cycle_to_json(self, result: AlertCycleResult) -> str:
        """Deterministic machine-readable JSON for one alert cycle."""

        return json.dumps(
            self.cycle_to_dict(result), indent=2, sort_keys=True,
        )

    def cycle_to_dict(self, result: AlertCycleResult) -> dict:
        """Deterministic JSON-able projection of one alert cycle."""

        return {
            "cycle_id": result.cycle_id,
            "lifecycle_cycle_id": result.lifecycle_cycle_id,
            "scan_cycle_id": result.scan_cycle_id,
            "reference_now": result.reference_now.isoformat(),
            "instruments": list(result.instruments),
            "policy_version": result.policy_version,
            "counts": {
                "eligible": result.counts.eligible,
                "emitted": result.counts.emitted,
                "delivered": result.counts.delivered,
                "failed": result.counts.failed,
                "skipped": result.counts.skipped,
                "suppressed": result.counts.suppressed,
                "by_kind": list(result.counts.by_kind),
            },
            "alerts": [self.alert_to_dict(a) for a in result.alerts],
            "deliveries": [
                {
                    "alert_id": d.alert_id,
                    "status": d.status.value,
                    "delivery_timestamp": (
                        d.delivery_timestamp.isoformat()
                        if d.delivery_timestamp is not None
                        else None
                    ),
                    "channel": d.channel,
                    "reason": d.reason,
                }
                for d in result.deliveries
            ],
            "suppressed": [
                {
                    "alert_id": s.alert_id,
                    "kind": s.kind.value,
                    "setup_id": s.setup_id,
                    "lifecycle_id": s.lifecycle_id,
                    "instrument": s.instrument,
                    "observation_id": s.observation_id,
                    "observation_timestamp": s.observation_timestamp.isoformat(),
                    "policy_version": s.policy_version,
                    "reason": s.reason,
                }
                for s in result.suppressed
            ],
        }


#: Re-export for the operator CLI / demo.
ALERT_FORMATTER_TYPES = (AlertFormatter,)


__all__ = [
    "ALERT_FORMATTER_TYPES",
    "AlertFormatter",
]