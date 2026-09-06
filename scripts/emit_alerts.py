#!/usr/bin/env python3
"""
Operator-facing CLI for the Checkpoint 19.7 USER ALERTS emitter.

A THIN command-line interface over the NEW 19.7 alert orchestration: it
consumes the FROZEN 19.6 setup-lifecycle events (lifecycle transitions)
and demonstrates / inspects the deterministic user-alert behavior:

    Scan Cycle (19.3 identity)
        |
    19.5 Setup Quality
        |
    19.6 Setup Lifecycle (events)
        |
    19.7 USER ALERTS (this CLI) -> Alert Events -> Delivery

The CLI implements NO trading intelligence beyond alert processing:

* NO trade execution, NO broker order placement, NO portfolio/position
  management, NO automated entries/exits, NO historical research, NO
  persistence framework, NO forward-testing infrastructure, NO
  reliability/watchdog framework. 19.8-19.9 own those.

Behaviour by default is FULLY OFFLINE and DETERMINISTIC:

* ``--demo`` (default) runs a SCRIPTED sequence of fixture
  setup-quality assessments -> lifecycle processing -> alert emission
  over explicit timestamps + scan-cycle ids and prints the alert report
  — no network, no credentials, no external notification service.
* ``--allow-detected`` enables the ``SETUP_DETECTED`` alert kind
  (informational) to demonstrate the full vocabulary.

``--json`` emits a pure deterministic JSON projection.

Delivery: the default channel is the local console sink (offline).
``--no-channel`` processes alerts with NO delivery channel (every
eligible alert is recorded as SKIPPED — explicit, auditable).

Exit codes: 0 whenever the run executed (honest findings, never treated
as CLI failure); 2 for bad args; 1 on runtime failure. Banner:
USER-ALERTS ONLY — no trade execution, no broker orders, no external
notification service, no persistence framework, no broker execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.setup_lifecycle import (  # noqa: E402
    SetupLifecycleEngine,
    SetupLifecycleStore,
)
from dashboard.user_alerts import (  # noqa: E402
    AlertEngine,
    ConsoleAlertSink,
)
from engine.config.setup_lifecycle_config import (  # noqa: E402
    SetupLifecycleConfig,
)
from engine.config.user_alerts_config import (  # noqa: E402
    UserAlertConfig,
)
from engine.reporting.user_alerts import (  # noqa: E402
    AlertFormatter,
)

#: Deterministic reference instants used for the default FIXTURE demo
#: (a Friday 11:00 IST trading-weekday start + 15-minute steps), so the
#: default CLI path never depends on wall-clock time.
_FIXTURE_START = datetime(2026, 9, 4, 5, 30, tzinfo=UTC)
_FIXTURE_STEP_MINUTES = 15


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --timestamp {raw!r}: {exc}",
        ) from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "observation timestamps must be timezone-aware "
            "(include an offset, e.g. ...Z or +00:00).",
        )
    return value.astimezone(UTC)


def _step():
    return timedelta(minutes=_FIXTURE_STEP_MINUTES)


def build_default_sequence():
    """Scripted deterministic fixture sequence demonstrating the 19.7
    alert behavior (confirmation, repeated-confirm no-op, data
    unavailability, direction-reversal invalidation, and a detection
    when enabled).

    Returns a list of ``(timestamp, scan_cycle_id, SetupQualityResult)``
    built through the FROZEN 19.5 setup-quality engine, so the alerts
    consume REAL 19.6 lifecycle events derived from real 19.5
    assessments.
    """

    from tests.test_setup_lifecycle import (  # local convenience import
        _bearish,
        _bullish,
        _no_setup,
    )

    t0 = _FIXTURE_START
    step = _step()
    return [
        (t0, "cycle-1", _bullish("RELIANCE")),
        (t0 + step, "cycle-2", _bullish("RELIANCE")),
        (t0 + 2 * step, "cycle-3", _no_setup("RELIANCE")),
        (t0 + 3 * step, "cycle-4", _bearish("RELIANCE")),
        (t0 + 4 * step, "cycle-5", _bearish("RELIANCE")),
        (t0 + 5 * step, "cycle-6", _bullish("TCS")),
    ]


def run_demo(
    engine: AlertEngine,
    lc_engine: SetupLifecycleEngine,
    args,
) -> int:
    """Run the deterministic demo sequence; print the report."""

    formatter = AlertFormatter()
    scenarios = build_default_sequence()
    cycle_results = []
    for ts, cycle_id, result in scenarios:
        lc_result = lc_engine.process(result, cycle_id, timestamp=ts)
        # A single-symbol demo: build a one-cycle lifecycle result per
        # scenario so each alert cycle is observable.
        from engine.models.setup_lifecycle import (
            SetupLifecycleCycleResult,
        )

        lc_cycle = SetupLifecycleCycleResult(
            cycle_id=f"lc-demo-{cycle_id}",
            scan_cycle_id=cycle_id,
            reference_now=ts,
            instruments=("RELIANCE", "TCS"),
            results=(lc_result,),
        )
        alert_cycle = engine.process(lc_cycle)
        cycle_results.append((cycle_id, alert_cycle))

    if args.json:
        payload = {
            "cycles": [
                {
                    "scan_cycle_id": cid,
                    **formatter.cycle_to_dict(cycle),
                }
                for cid, cycle in cycle_results
            ],
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for cid, cycle in cycle_results:
            print(formatter.format_cycle(cycle))
            print()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emit_alerts",
        description="User alerts emitter (Checkpoint 19.7).",
    )
    parser.add_argument(
        "--demo", action="store_true", default=True,
        help="Run the deterministic fixture demo (default).",
    )
    parser.add_argument(
        "--allow-detected", action="store_true", default=False,
        help="Enable the informational SETUP_DETECTED alert kind in "
             "the demo (off by default — detection is not in the "
             "default eligible set).",
    )
    parser.add_argument(
        "--no-channel", action="store_true", default=False,
        help="Process alerts with NO delivery channel (every eligible "
             "alert is recorded as SKIPPED — explicit, auditable).",
    )
    parser.add_argument(
        "--timestamp", type=str, default=None,
        help="Overwrite the demo's first observation timestamp "
             "(ISO-8601, UTC).",
    )
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Emit a pure deterministic JSON projection.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # An explicit --timestamp must be timezone-aware (ISO-8601 UTC);
    # naive values are rejected at the CLI boundary (exit 2).
    if args.timestamp:
        try:
            _parse_timestamp(args.timestamp)
        except Exception as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        eligible = ("SETUP_CONFIRMED", "SETUP_INVALIDATED", "SETUP_EXPIRED")
        if args.allow_detected:
            eligible = ("SETUP_DETECTED",) + eligible
        config = UserAlertConfig(eligible_kinds=eligible)
        channels = [] if args.no_channel else [ConsoleAlertSink()]
        engine = AlertEngine.build(channels=channels, config=config)
        lc_engine = SetupLifecycleEngine(
            SetupLifecycleConfig(), SetupLifecycleStore(),
        )
        return run_demo(engine, lc_engine, args)
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())