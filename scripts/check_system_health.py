#!/usr/bin/env python3
"""
Operator diagnostic CLI for Checkpoint 19.8 operational health.

A THIN command-line interface over the EXISTING 19.8 reliability
tracker. It answers the OPERATIONAL question "is the system
operational?" — NEVER "is there a good trade?". It exposes:

* system health (HEALTHY / DEGRADED / RECOVERING / FAILED);
* scanner state + scanner/cycle health;
* provider health + failure counts;
* delivery health (+ outbox pending);
* heartbeat state;
* recovery state;
* persisted operational-state location.

Behaviour by default is FULLY OFFLINE and DETERMINISTIC:

* Without ``--load-state`` the tracker runs a single deterministic
  fixture scan cycle over the frozen NIFTY Top 200 universe (fixture
  provider) and reports the resulting operational health (the default
  fixture path never depends on wall-clock time — ``--reference-now``
  is a fixed deterministic sentinel).
* ``--load-state --state-dir DIR`` reads the PERSISTED operational
  state (produced by a previous run / the running scanner) and reports
  its health WITHOUT starting a new cycle.
* ``--provider yahoo`` (OPT-IN, requires ``yfinance`` + real network)
  runs one live cycle; NOT the deterministic default.

Exit codes: 0 whenever the diagnostic ran (a DEGRADED/FAILED health is
reported honestly, never a CLI failure); 2 for bad args; 1 on a
runtime failure. No credentials are required for the normal path.
No broker execution is involved anywhere.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Deterministic reference time for the default FIXTURE run (same
#: sentinel convention as 19.3/19.4/19.5 CLIs: a Monday 11:00 IST
#: trading-weekday instant).
_FIXTURE_DETERMINISTIC_NOW = datetime(2026, 9, 7, 5, 30, tzinfo=UTC)


def _parse_reference_now(raw: str | None, provider: str) -> datetime:
    if not raw:
        if provider == "fixture":
            return _FIXTURE_DETERMINISTIC_NOW
        return datetime.now(UTC)
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --reference-now {raw!r}: {exc}",
        ) from exc
    if value.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "--reference-now must be timezone-aware (add an offset).",
        )
    return value.astimezone(UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_system_health.py",
        description=(
            "NIFTY Top 200 operational health diagnostic (Checkpoint "
            "19.8). Descriptive operational-reliability reporting only."
        ),
    )
    parser.add_argument(
        "--provider",
        default="fixture",
        choices=("fixture", "yahoo"),
        help=(
            "Market-data provider: 'fixture' (default, deterministic, "
            "offline) or 'yahoo' (OPT-IN live/near-live; requires "
            "yfinance)."
        ),
    )
    parser.add_argument(
        "--timeframe",
        default="15m",
        help="Canonical intraday timeframe (default 15m).",
    )
    parser.add_argument(
        "--instruments",
        default=None,
        help=(
            "Comma-separated instrument list (default: the full NIFTY "
            "Top 200 universe from the 19.1 manifest)."
        ),
    )
    parser.add_argument(
        "--reference-now",
        default=None,
        help=(
            "ISO-8601 timezone-aware reference instant. Deterministic "
            "fixture default when omitted."
        ),
    )
    parser.add_argument(
        "--load-state",
        action="store_true",
        help=(
            "Load the PERSISTED operational state from --state-dir and "
            "report its health WITHOUT starting a new scan cycle."
        ),
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help=(
            "Operational-state directory (default: ./data/operational_state)."
        ),
    )
    parser.add_argument(
        "--no-heartbeat",
        action="store_true",
        help="Do not refresh the heartbeat during the diagnostic run.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable health projection as JSON.",
    )
    return parser


def _build_tracker(reference_now: datetime, load_state: bool, state_dir: str | None):
    from engine.config.reliability_config import ReliabilityConfig
    from engine.persistence.reliability_store import (
        OperationalStateStore,
        default_operational_state_directory,
    )

    store = OperationalStateStore(
        state_dir or default_operational_state_directory(),
    )
    tracker = __import__(
        "dashboard.reliability",
        fromlist=["ReliabilityTracker"],
    ).ReliabilityTracker(
        ReliabilityConfig(),
        clock=lambda: reference_now,
        store=store,
    )
    if load_state:
        tracker.startup_recovery(scanner_state="LOADED", at=reference_now)
        return tracker, store
    tracker.startup_recovery(scanner_state="RUNNING", at=reference_now)
    return tracker, store


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        reference_now = _parse_reference_now(args.reference_now, args.provider)
    except argparse.ArgumentTypeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    from engine.config.universe_boundary import (
        DEFAULT_NIFTY200_UNIVERSE,
        UniverseBuilder,
    )

    if args.instruments:
        names = [n.strip().upper() for n in args.instruments.split(",")]
        names = [n for n in names if n]
        if not names:
            print("ERROR: --instruments produced an empty list.", file=sys.stderr)
            return 2
        universe = UniverseBuilder.custom(names, label="cli explicit")
        universe_label = f"custom ({len(names)})"
    else:
        universe = DEFAULT_NIFTY200_UNIVERSE
        universe_label = "NIFTY Top 200"

    try:
        tracker, store = _build_tracker(
            reference_now, args.load_state, args.state_dir,
        )

        if not args.load_state:
            if not args.no_heartbeat:
                tracker.refresh_heartbeat(at=reference_now)
            from dashboard.continuous_scanner import ContinuousScannerEngine

            engine = ContinuousScannerEngine.build(
                args.provider, timeframe=args.timeframe,
            )
            result = engine.run_cycle(
                universe=universe,
                timeframe=args.timeframe,
                reference_now=reference_now,
            )
            tracker.record_cycle(
                result,
                started_at=reference_now,
                ended_at=reference_now,
                reference_now=reference_now,
            )
            # Record provider outcomes from the per-symbol results
            # (successes + isolated failures — recorded, never a crash).
            from engine.models.operational_health import (
                ReliabilityFailureCategory,
            )

            for symbol in result.results:
                status_name = symbol.status.value
                if status_name in (
                    "PROVIDER_ERROR",
                    "INVALID_RESPONSE",
                    "TEMPORARILY_UNAVAILABLE",
                ):
                    category = (
                        ReliabilityFailureCategory.PROVIDER_TIMEOUT
                        if status_name == "PROVIDER_ERROR"
                        else ReliabilityFailureCategory.PROVIDER_UNAVAILABLE
                    )
                    tracker.record_provider_result(
                        success=False,
                        category=category,
                        provider_name=args.provider,
                        symbol=symbol.instrument,
                        cycle_id=result.cycle_id,
                        duration_seconds=None,
                        detail=f"per-symbol status {status_name}.",
                        at=reference_now,
                    )
                    tracker.record_per_symbol_failure(
                        symbol.instrument,
                        category,
                        cycle_id=result.cycle_id,
                        timestamp=reference_now,
                        detail=f"per-symbol status {status_name}.",
                    )
                elif symbol.available:
                    tracker.record_provider_result(
                        success=True,
                        provider_name=args.provider,
                        symbol=symbol.instrument,
                        cycle_id=result.cycle_id,
                        duration_seconds=None,
                        detail=f"per-symbol status {status_name}.",
                        at=reference_now,
                    )
            tracker.attach_cycle_failures(
                tracker.last_cycle_summary,
                per_symbol_failures=(),
                exceptions_escaped=0,
            )

        health = tracker.health(reference_now=reference_now)
    except Exception as exc:  # runtime failure, never silent
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        from engine.reporting.reliability import OperationalHealthFormatter

        formatter = OperationalHealthFormatter()
        payload = formatter.health_report_to_jsonable(health)
        payload.update({
            "universe": universe_label,
            "provider": args.provider,
            "state_dir": str(store.directory),
            "state_persisted": store.exists(),
        })
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        from engine.reporting.reliability import OperationalHealthFormatter

        formatter = OperationalHealthFormatter()
        print(
            f"[i] operational health: {health.health_state.value}  "
            f"provider={args.provider}  universe={universe_label}"
        )
        print(f"    state dir : {store.directory} (persisted={store.exists()})")
        print(formatter.format(health))
        print(
            "\nOPERATIONAL HEALTH REPORTING ONLY — no prediction, no "
            "setups, no alerts, no broker execution.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())