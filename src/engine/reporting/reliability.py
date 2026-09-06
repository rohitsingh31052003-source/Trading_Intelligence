"""
Operational health report formatters (Checkpoint 19.8).

Deterministic, stateless renderers for the 19.8 operational-health
models:

    * :meth:`OperationalHealthFormatter.format` — ONE operational health
      report (human-readable text);
    * :meth:`OperationalHealthFormatter.health_report_to_json` — the
      machine-readable deterministic JSON projection.

Returns ``str`` (no ``print()`` inside). DESCRIPTIVE ONLY — the report
answers "is the system operational?" — it NEVER answers "is there a
good trade?" and NEVER contains setup-quality / trade-plan / execution
directives. Operational and analytical vocabularies remain strictly
separate (HEALTHY / DEGRADED / RECOVERING / FAILED describe the system;
CONFIRMED / HIGH / ALIGNED describe setups).

Every report ends with the explicit warning that operational health is
descriptive reliability state, not a trade signal and not a prediction.

Following the 11O-19.7 reporting convention, this formatter is imported
via its full path (NOT re-exported from ``reporting/__init__.py``).
"""

from __future__ import annotations

import json

from engine.models.operational_health import OperationalHealthReport


class OperationalHealthFormatter:
    """
    Deterministic operational-health report formatter.

    ``precision`` controls the decimal places for durations; ``width``
    controls text wrapping. Both are validated (no magic values).
    """

    def __init__(self, precision: int = 2, width: int = 80) -> None:
        if isinstance(precision, bool) or not isinstance(precision, int):
            raise TypeError("precision must be an int, not bool.")
        if precision < 0:
            raise ValueError("precision must be non-negative.")
        if isinstance(width, bool) or not isinstance(width, int):
            raise TypeError("width must be an int, not bool.")
        if width < 1:
            raise ValueError("width must be >= 1.")
        self.precision = precision
        self.width = width

    # ----------------------------------------------------------
    # TEXT
    # ----------------------------------------------------------

    def format(self, report: OperationalHealthReport) -> str:
        """A complete operational health report (deterministic)."""

        lines = [
            "OPERATIONAL HEALTH REPORT",
            "",
            f"Health state        : {report.health_state.value}",
            f"Scanner state       : {report.scanner_state or 'unknown'}",
            f"Scanner health      : {report.scanner_health.value}",
            f"Provider health     : {report.provider_health.value}",
            f"Delivery health     : {report.delivery_health.value}",
            f"Heartbeat           : {report.heartbeat_state.value}"
            + (
                f" (at {report.heartbeat_at.isoformat()})"
                if report.heartbeat_at is not None
                else ""
            ),
            "Last cycle          : "
            + (
                f"{report.last_cycle_id} at "
                f"{report.last_cycle_at.isoformat()}"
                if report.last_cycle_at is not None
                else "none"
            ),
            "Last cycle duration : "
            + (
                f"{report.last_cycle_duration_seconds:.{self.precision}f}s"
                if report.last_cycle_duration_seconds is not None
                else "unavailable"
            ),
            "",
            "CYCLE COUNTS",
        ]
        for name, count in report.cycle_counts:
            lines.append(f"  {name:<12} {count}")
        lines.append("")
        lines.append("FAILURE COUNTS (operational)")
        if report.failure_counts:
            for name, count in report.failure_counts:
                lines.append(f"  {name:<24} {count}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("DOMAIN HEALTH")
        for domain, status in report.domain_health:
            lines.append(f"  {domain:<12} {status}")
        lines.append("")
        lines.append("RETRY COUNTS")
        if report.retry_counts:
            for name, count in report.retry_counts:
                lines.append(f"  {name:<24} {count}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("RECOVERY COUNTS")
        if report.recovery_counts:
            for name, count in report.recovery_counts:
                lines.append(f"  {name:<12} {count}")
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append("PER-SYMBOL FAILURES (isolated)")
        if report.per_symbol_failures:
            for failure in report.per_symbol_failures:
                lines.append(
                    f"  {failure.symbol:<22} {failure.category.value:<24} "
                    f"attempts={failure.attempts} retried={failure.retried}",
                )
        else:
            lines.append("  (none)")
        lines.append("")
        lines.append(f"Outbox pending      : {report.outbox_pending}")
        lines.append(f"Policy version      : {report.policy_version}")
        lines.append(f"Rationale           : {report.rationale or 'n/a'}")
        lines.append("")
        lines.append(
            "DISCLAIMER: operational health is descriptive reliability "
            "state. It does not predict market behavior, does not "
            "constitute a trading recommendation, and does not authorize "
            "any broker execution.",
        )
        return "\n".join(lines)

    # ----------------------------------------------------------
    # JSON
    # ----------------------------------------------------------

    def health_report_to_json(self, report: OperationalHealthReport) -> str:
        """Deterministic sorted-key JSON projection of a health report."""

        return json.dumps(
            self.health_report_to_jsonable(report),
            sort_keys=True,
            ensure_ascii=False,
        )

    def health_report_to_jsonable(self, report: OperationalHealthReport) -> dict:
        """Deterministic JSON-safe projection (no internal objects)."""

        return {
            "health_state": report.health_state.value,
            "scanner_state": report.scanner_state or None,
            "scanner_health": report.scanner_health.value,
            "provider_health": report.provider_health.value,
            "delivery_health": report.delivery_health.value,
            "heartbeat_state": report.heartbeat_state.value,
            "heartbeat_at": (
                report.heartbeat_at.isoformat()
                if report.heartbeat_at is not None
                else None
            ),
            "last_cycle_at": (
                report.last_cycle_at.isoformat()
                if report.last_cycle_at is not None
                else None
            ),
            "last_cycle_id": report.last_cycle_id or None,
            "last_cycle_duration_seconds": report.last_cycle_duration_seconds,
            "cycle_counts": list(report.cycle_counts),
            "failure_counts": list(report.failure_counts),
            "domain_health": list(report.domain_health),
            "retry_counts": list(report.retry_counts),
            "recovery_counts": list(report.recovery_counts),
            "per_symbol_failures": [
                {
                    "symbol": f.symbol,
                    "category": f.category.value,
                    "timestamp": f.timestamp.isoformat(),
                    "retried": f.retried,
                    "attempts": f.attempts,
                    "detail": f.detail,
                }
                for f in report.per_symbol_failures
            ],
            "outbox_pending": report.outbox_pending,
            "policy_version": report.policy_version,
            "rationale": report.rationale,
        }


__all__ = [
    "OperationalHealthFormatter",
]