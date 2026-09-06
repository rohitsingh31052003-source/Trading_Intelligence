#!/usr/bin/env python3
"""
Forward-validation / forward-testing operator CLI (Checkpoint 19.9).

A THIN command-line interface over the Checkpoint 19.9
forward-validation framework. It records what the FROZEN 19.x pipeline
observed at each instant ``T`` and measures what the market
subsequently did AFTER ``T``.

    Scan Cycle (19.3) -> MTF (19.4) -> Setup Quality (19.5)
        -> Lifecycle (19.6) -> Alerts (19.7) -> Reliability (19.8)
        -> FORWARD OBSERVATION (at T) -> WAIT -> FORWARD OUTCOME
        -> FORWARD SESSION / REPORT (19.9)

Sub-commands:

* ``start``  — open a deterministic forward-validation session
  (records the configuration + policy versions; persists the session
  manifest when ``--state-dir`` is provided).
* ``evaluate`` — run ONE forward-validation cycle at an explicit
  reference instant (records one observation per requested instrument
  from the FROZEN upstream outputs; measures forward outcomes when
  forward candles are supplied via ``--forward-data``).
* ``close`` — close an open session (deterministic snapshot).
* ``report`` — produce the deterministic forward-validation report for
  one or more sessions.
* ``demo``  — fully OFFLINE / DETERMINISTIC scripted demonstration
  through the real 19.x pipeline outputs (default sub-command).

The default test mode is deterministic/offline (fixture provider +
explicit reference instants). No live mode exists in this CLI — no
credentials, no network, no broker execution. Forward-market data is
supplied explicitly (deterministic path) or NOT at all (the outcome is
recorded honestly as UNAVAILABLE / WINDOW_INCOMPLETE).

Exit codes: 0 whenever the run executed (honest findings, never treated
as CLI failure); 2 for bad args; 1 on runtime failure. Banner:
FORWARD-VALIDATION ONLY — no trade execution, no alerts, no setup
lifecycle management, no persistence framework beyond the session
record, no broker execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.forward_validation import (  # noqa: E402
    ForwardOutcomeEngine,
    ForwardSessionManager,
    ForwardValidationEngine,
    build_forward_observation,
)
from engine.config.forward_validation_config import (  # noqa: E402
    ForwardValidationConfig,
)
from engine.persistence.forward_validation_store import (  # noqa: E402
    ForwardObservationStore,
)
from engine.reporting.forward_validation import (  # noqa: E402
    ForwardValidationFormatter,
)


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"not an ISO-8601 instant: {value!r}",
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(
            "reference instants must be timezone-aware (naive datetimes "
            "are never silently accepted).",
        )
    return parsed.astimezone(UTC)


def _parse_pairs(value: str) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for chunk in value.split(","):
        if not chunk.strip():
            continue
        if "=" not in chunk:
            raise argparse.ArgumentTypeError(
                f"metadata entries must be name=value pairs (got {chunk!r})."
            )
        name, _, val = chunk.partition("=")
        pairs.append((name.strip(), val.strip()))
    return tuple(pairs)


def _json_dump(value) -> None:
    print(json.dumps(value, sort_keys=True, indent=2))


def _banner() -> None:
    print("=" * 76)
    print("FORWARD-VALIDATION ONLY — no trade execution, no broker orders,")
    print("no alert delivery, no automatic entry/exit. The user is the")
    print("final execution boundary.")
    print("=" * 76)


def _build_config(args) -> ForwardValidationConfig:
    return ForwardValidationConfig(
        provider=args.provider,
        timeframes=tuple(args.timeframes),
        max_holding_bars=args.horizon,
        record_duplicates=args.record_duplicates,
        reject_out_of_order=not args.allow_out_of_order,
        label=args.label or "",
        metadata=_parse_pairs(args.metadata) if args.metadata else (),
    )


def _build_store(args) -> ForwardObservationStore | None:
    if not args.state_dir:
        return None
    return ForwardObservationStore(Path(args.state_dir))


def _cmd_start(args) -> int:
    _banner()
    config = _build_config(args)
    store = _build_store(args)
    session = config.policy_version()
    started = args.reference_now
    if started is not None and not isinstance(started, datetime):
        started = _parse_instant(str(started))
    manager = ForwardSessionManager(config, store=store)
    opened = manager.open_session(
        universe=tuple(args.instruments) if args.instruments else None,
        started_at=started,
        label=args.label or "",
    )
    print(f"[session] opened {opened.session_id} "
          f"(provider={opened.provider}, "
          f"primary={opened.primary_timeframe}, "
          f"universe={len(opened.universe)})")
    print(f"[session] validation policy version: {session}")
    print(f"[session] persisted: {store is not None}")
    if args.json:
        _json_dump({
            "session_id": opened.session_id,
            "provider": opened.provider,
            "timeframes": list(opened.timeframes),
            "primary_timeframe": opened.primary_timeframe,
            "universe_size": len(opened.universe),
            "started_at": opened.started_at.isoformat(),
            "status": opened.status,
            "policy_version": session,
            "persisted": store is not None,
        })
    return 0


def _cmd_demo(args) -> int:
    _banner()
    import types

    from engine.models.setup_lifecycle import (
        LifecycleObservationStatus,
        SetupLifecycleState,
    )
    from engine.models.setup_quality import (
        SetupQualityClassification,
        SetupQualityStatus,
    )

    T0 = datetime(2026, 9, 4, 5, 30, tzinfo=UTC)  # Friday 11:00 IST
    config = _build_config(args)
    store = _build_store(args)
    manager = ForwardSessionManager(config, store=store)
    engine = ForwardValidationEngine(config, session_manager=manager)
    outcome_engine = ForwardOutcomeEngine(config)
    formatter = ForwardValidationFormatter()
    policy_version = config.policy_version()

    opened = manager.open_session(
        universe=("RELIANCE", "TCS"),
        started_at=T0,
        label="checkpoint19.9-demo",
    )
    print(f"[demo] opened session {opened.session_id}")

    def _quality(instrument, direction, classification, score, status):
        return types.SimpleNamespace(
            instrument=instrument,
            primary_timeframe="15m",
            status=status,
            classification=classification,
            score=score,
            setup_direction=direction or "",
            setup_type="TREND_CONTINUATION" if direction else "",
            mtf_alignment="ALIGNED" if direction else "UNAVAILABLE",
            mtf_completeness="COMPLETE" if direction else "INCOMPLETE",
        )

    def _lifecycle(instrument, setup_id, state, obs_status, lifecycle_id):
        return types.SimpleNamespace(
            instrument=instrument,
            setup_id=setup_id,
            lifecycle_id=lifecycle_id,
            state=state,
            observation_status=obs_status,
            observation_id=f"obs-demo-{instrument}",
            scan_cycle_id=f"scan-demo-{instrument}",
            reason="",
        )

    def _record(instrument, direction, q, lc, price):
        return build_forward_observation(
            session_id=opened.session_id,
            scan_cycle_id=f"scan-{instrument}",
            reference_now=T0,
            instrument=instrument,
            quality=_quality(
                instrument, direction,
                SetupQualityClassification.EXCELLENT if direction else SetupQualityClassification.LOW,
                88 if direction else 40,
                SetupQualityStatus.QUALIFIED if direction else SetupQualityStatus.WATCH,
            ),
            lifecycle=lc,
            policy_version=policy_version,
            reference_price=price,
        )

    # -- 1/2/3: RELIANCE bullish qualified setup, TCS watch --------
    rel = manager.record_observation(
        opened.session_id,
        _record(
            "RELIANCE", "BULLISH",
            _quality("RELIANCE", "BULLISH",
                     SetupQualityClassification.EXCELLENT, 88,
                     SetupQualityStatus.QUALIFIED),
            _lifecycle("RELIANCE", "setup-demo-r", SetupLifecycleState.CONFIRMED,
                       LifecycleObservationStatus.OBSERVED, "lc-demo-r"),
            100.0,
        ),
    )
    tcs = manager.record_observation(
        opened.session_id,
        _record(
            "TCS", "",
            _quality("TCS", "",
                     SetupQualityClassification.LOW, 40,
                     SetupQualityStatus.WATCH),
            None,
            200.0,
        ),
    )
    for obs in (rel, tcs):
        print(f"[demo] recorded {obs.instrument}: "
              f"dir={obs.direction or 'none'} "
              f"q={obs.quality_classification.name} "
              f"state={obs.lifecycle_state.name} "
              f"mtf={obs.mtf_alignment}")
    # Duplicate observation idempotency.
    dup = manager.record_observation(opened.session_id, rel)
    print(f"[demo] duplicate reprocess -> {dup.status.name}")

    # -- outcome measurement after T -------------------------------
    from engine.models.ohlcv import OHLCVCandle

    forward = [
        OHLCVCandle(
            timestamp=T0 + timedelta(minutes=15 * i),
            open=100.0 + 3 * i, high=100.0 + 4 * i,
            low=100.0 + 1 * i, close=100.0 + 3 * i,
            volume=1000,
        )
        for i in range(1, 4)
    ]
    obs = manager.observations_for(opened.session_id)[0]
    outcome = outcome_engine.measure(
        obs, forward,
        measurement_timestamp=T0 + timedelta(minutes=45),
    )
    print(f"[demo] outcome for {obs.instrument}: "
          f"{outcome.availability.name} "
          f"forward_return={outcome.forward_return:.4f} "
          f"dir={outcome.outcome_direction.name} "
          f"window_complete={outcome.window_complete}")

    # -- close + report ---------------------------------------------
    closed = engine.close_session(opened.session_id, ended_at=T0 + timedelta(hours=1))
    print(f"[demo] closed session {closed.session_id} "
          f"(observations={closed.counts.observations})")
    report = engine.report(closed, outcomes=(outcome,))
    print(formatter.format_report(report))
    print("\nCheckpoint 19.9 demo completed successfully.")
    return 0


def _cmd_report(args) -> int:
    _banner()
    config = _build_config(args)
    store = _build_store(args)
    engine = ForwardValidationEngine(config, store=store)
    formatter = ForwardValidationFormatter()
    if not args.session_ids:
        print("no sessions supplied; reporting an empty report.", file=sys.stderr)
    report = engine.report(tuple(args.session_ids))
    print(formatter.format_report(report))
    if args.json:
        _json_dump({
            "report_id": report.report_id,
            "session_ids": list(report.session_ids),
            "universe_instrument_count": report.universe_instrument_count,
            "instruments_attempted": report.instruments_attempted,
            "setups_detected": report.setups_detected,
            "alerts_generated": report.alerts_generated,
            "alerts_delivered": report.alerts_delivered,
        })
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="forward_validation",
        description=(
            "Checkpoint 19.9 forward-validation / forward-testing CLI "
            "(descriptive, point-in-time-safe; no broker execution)."
        ),
    )
    parser.add_argument(
        "--provider",
        default="fixture",
        choices=("fixture",),
        help="market-data provider (fixture = deterministic offline).",
    )
    parser.add_argument(
        "--timeframes",
        nargs="+",
        default=("15m", "1h"),
        help="canonical intraday timeframes (primary = lowest duration).",
    )
    parser.add_argument(
        "--instruments",
        nargs="+",
        default=None,
        help="instruments (default: the configured universe).",
    )
    parser.add_argument(
        "--horizon",
        type=int,
        default=5,
        help="outcome measurement window in primary-timeframe bars.",
    )
    parser.add_argument(
        "--reference-now",
        default=None,
        type=_parse_instant,
        help="explicit aware-UTC reference instant (deterministic).",
    )
    parser.add_argument(
        "--state-dir",
        default=None,
        help="forward-validation persistence directory (default: cwd "
             "data/forward_validation).",
    )
    parser.add_argument(
        "--label", default=None, help="run label.",
    )
    parser.add_argument(
        "--metadata",
        default=None,
        help="comma-separated name=value metadata pairs.",
    )
    parser.add_argument(
        "--record-duplicates",
        action="store_true",
        help="record duplicate observations instead of flagging them.",
    )
    parser.add_argument(
        "--allow-out-of-order",
        action="store_true",
        help="record out-of-order observations instead of rejecting them.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit pure deterministic machine JSON where supported.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("start")
    sub.add_parser("demo")
    p_report = sub.add_parser("report")
    p_report.add_argument("session_ids", nargs="*")
    p_report.add_argument(
        "--state-dir", default=None,
    )
    p_report.add_argument("--json", action="store_true")

    # NOTE: the parent parser carries ALL configuration flags
    # (--provider/--timeframes/--horizon/...); a sub-command only
    # selects the action. Flags must be given BEFORE the sub-command
    # name (argparse convention for this layout):
    #   python scripts/forward_validation.py --timeframes 15m start ...
    args = parser.parse_args(argv)
    command = args.command or "demo"
    try:
        if command == "start":
            return _cmd_start(args)
        if command == "demo":
            return _cmd_demo(args)
        if command == "report":
            setattr(args, "session_ids", tuple(args.session_ids or ()))
            setattr(args, "reference_now", None)
            setattr(args, "instruments", None)
            setattr(args, "horizon", getattr(args, "horizon", 5))
            return _cmd_report(args)
    except (TypeError, ValueError) as exc:
        print(f"invalid arguments: {exc}", file=sys.stderr)
        return 2
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())