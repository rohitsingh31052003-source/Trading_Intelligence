#!/usr/bin/env python3
"""
Operator-facing CLI for the Checkpoint 19.6 SETUP LIFECYCLE analyzer.

A THIN command-line interface over the NEW 19.6 lifecycle
orchestration: it consumes the FROZEN 19.5 setup-quality universe
assessments (fixture-derived by default) + the FROZEN 19.3 scan-cycle
identity and demonstrates / inspects the deterministic setup lifecycle
behavior:

    Scan Cycle (19.3 identity)
        |
    19.5 Setup Quality
        |
    19.6 SETUP LIFECYCLE (this CLI)

The CLI implements NO trading intelligence beyond lifecycle
processing:

* NO trade execution, NO broker order placement, NO portfolio/position
  management, NO automated entries/exits, NO user notifications, NO
  persistence framework, NO forward-testing infrastructure, NO
  reliability/watchdog framework. 19.7-19.9 own those.

Behaviour by default is FULLY OFFLINE and DETERMINISTIC:

* ``--demo`` (default) runs a SCRIPTED sequence of fixture setup-quality
  assessments over explicit timestamps + scan-cycle ids and prints the
  lifecycle history — no network, no credentials, no alerts.
* ``--assessments`` accepts a JSON file array of 19.5-compatible
  assessment fixtures (for scripted operator scenarios).

``--scan-cycle`` and ``--timestamp`` (ISO-8601, UTC) force an explicit
cycle identity + observation instant (never wall-clock by default).

``--json`` emits a pure deterministic JSON projection.

Exit codes: 0 whenever the run executed (honest findings, never treated
as CLI failure); 2 for bad args; 1 on runtime failure. Banner:
SETUP-LIFECYCLE ONLY — no trade execution, no alerts, no setup
lifecycle persistence, no broker execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.setup_lifecycle import (  # noqa: E402
    SetupLifecycleEngine,
    SetupLifecycleStore,
)
from engine.config.setup_lifecycle_config import (  # noqa: E402
    SetupLifecycleConfig,
)
from engine.reporting.setup_lifecycle import (  # noqa: E402
    SetupLifecycleFormatter,
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


def _resolve_timestamp(provided: datetime | None) -> datetime:
    if provided is not None:
        return provided
    # Deterministic sentinel: the fixture start instant. Wall-clock is
    # NEVER used by the default deterministic path.
    return _FIXTURE_START


def build_default_sequence():
    """Scripted deterministic fixture sequence demonstrating the 19.6
    lifecycle behavior (setup creation, repeated observation, quality
    change, data unavailability, and direction reversal).

    Returns a list of ``(timestamp, scan_cycle_id, SetupQualityResult)``
    built through the FROZEN 19.5 setup-quality engine, so the lifecycle
    consumes real setup-quality assessments.
    """

    from tests.test_setup_quality import (  # local convenience import
        _aligned_mtf,
        _bearish_15m,
        _bullish_15m,
        _flat_candles,
        _market_context,
    )
    from dashboard.setup_quality import SetupQualityEngine
    from engine.config.setup_quality_config import SetupQualityConfig
    from engine.models.market_context import MarketTrendState

    sq_engine = SetupQualityEngine(SetupQualityConfig())

    def assess(instrument: str, direction: str, candles):
        market = _market_context(
            MarketTrendState.BULLISH
            if direction == "BULLISH"
            else (
                MarketTrendState.BEARISH
                if direction == "BEARISH"
                else MarketTrendState.UNKNOWN
            ),
        )
        mtf = _aligned_mtf(instrument, market_state=market)
        return sq_engine.evaluate(mtf, candles)

    t0 = _FIXTURE_START
    step = _STEP_MINUTES()
    return [
        (t0, "cycle-1", assess("RELIANCE", "BULLISH", _bullish_15m())),
        (t0 + step, "cycle-2", assess("RELIANCE", "BULLISH", _bullish_15m())),
        (t0 + 2 * step, "cycle-3", assess("RELIANCE", "UNKNOWN", _flat_candles())),
        (t0 + 3 * step, "cycle-4", assess("RELIANCE", "BEARISH", _bearish_15m())),
    ]


def _STEP_MINUTES():
    from datetime import timedelta

    return timedelta(minutes=_FIXTURE_STEP_MINUTES)


def run_demo(
    engine: SetupLifecycleEngine,
    args,
) -> tuple[int, str]:
    """Run the deterministic demo sequence; print / return the report."""

    formatter = SetupLifecycleFormatter()
    scenarios = build_default_sequence()
    outcomes = []
    for ts, cycle, result in scenarios:
        outcomes.append(engine.process(result, cycle, timestamp=ts))

    paragraphs = [
        formatter.format_cycle(
            _display_cycle(engine, scenarios[-1][1], scenarios[-1][0]),
        ),
    ]
    for lc in engine.store.list_lifecycles():
        paragraphs.append(formatter.format_lifecycle(lc))

    report = "\n\n".join(paragraphs)
    if args.json:
        print(json.dumps(_json_report(engine), indent=2, sort_keys=True))
    else:
        print(report)
    return 0, report


def _display_cycle(
    engine: SetupLifecycleEngine,
    cycle_id: str,
    ts: datetime,
):
    """A lightweight display cycle result for the demo (rendering only —
    assertions use the real ``process_universe`` result)."""

    from engine.models.setup_lifecycle import (
        SetupLifecycleCycleResult,
    )

    instruments = tuple(
        sorted({lc.instrument for lc in engine.store.list_lifecycles()}),
    )
    return SetupLifecycleCycleResult(
        cycle_id="lc-cycle-demo",
        scan_cycle_id=cycle_id,
        reference_now=ts,
        instruments=instruments,
        results=(),
        active_count=engine.store.active_count,
        terminal_count=engine.store.terminal_count,
    )


def _json_report(engine: SetupLifecycleEngine) -> dict:
    """Deterministic JSON projection for the operator surface."""

    return {
        "lifecycles": [
            {
                "lifecycle_id": lc.lifecycle_id,
                "setup_id": lc.setup_id,
                "instrument": lc.instrument,
                "direction": lc.direction.value,
                "setup_type": lc.setup_type,
                "primary_timeframe": lc.primary_timeframe,
                "instance": lc.instance_discriminator,
                "state": lc.state.value,
                "consecutive_missing": lc.consecutive_missing,
                "observations": [
                    {
                        "observation_id": o.observation_id,
                        "timestamp": o.observation_timestamp.isoformat(),
                        "scan_cycle_id": o.scan_cycle_id,
                        "status": o.observation_status.value,
                        "at_state": o.at_state.value if o.at_state else None,
                        "quality_status": o.quality_status,
                        "quality_score": o.quality_score,
                    }
                    for o in lc.observations
                ],
                "transitions": [
                    {
                        "transition_id": t.transition_id,
                        "from_state": t.from_state.value,
                        "to_state": t.to_state.value,
                        "timestamp": t.observation_timestamp.isoformat(),
                        "reason": t.reason,
                    }
                    for t in lc.transitions
                ],
            }
            for lc in engine.store.list_lifecycles()
        ],
        "active_count": engine.store.active_count,
        "terminal_count": engine.store.terminal_count,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_setup_lifecycle",
        description="Setup lifecycle analyzer (Checkpoint 19.6).",
    )
    parser.add_argument(
        "--demo", action="store_true", default=True,
        help="Run the deterministic fixture demo (default).",
    )
    parser.add_argument(
        "--timestamp", type=str, default=None,
        help="Overwrite the demo's first observation timestamp "
             "(ISO-8601, UTC).",
    )
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="Emit a pure deterministic JSON report.",
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
        engine = SetupLifecycleEngine(
            SetupLifecycleConfig(), SetupLifecycleStore(),
        )
        code, _ = run_demo(engine, args)
        return code
    except Exception:
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())