#!/usr/bin/env python3
"""
Operator-facing CLI for the Checkpoint 19.4 MULTI-TIMEFRAME MARKET-STATE
analyzer.

A THIN command-line interface over the EXISTING 19.2 intraday coverage
layer + the reusable Sprint 11P market-context engine + the NEW 19.4 MTF
market-state orchestration. It answers, for one or several symbols at one
reference instant:

    "What does the current market state of this instrument look like
     across the configured timeframes, and are those timeframe states
     aligned, mixed, or conflicting?"

The CLI implements NO trading intelligence:

* NO setup detection, NO setup scoring, NO trade ranking, NO entry
  signals, NO buy/sell decisions, NO entry/stop/target, NO risk/reward,
  NO trade plans, NO setup lifecycle, NO alerts, NO broker execution.
* MTF ALIGNED / MIXED / CONFLICTING are TIMEFRAME RELATIONSHIP
  classifications — never BUY / SELL / TRADE / NO-TRADE verdicts.

Behaviour by default is FULLY OFFLINE and DETERMINISTIC:

* ``--provider fixture`` (default) uses the local deterministic
  fixtures (no network, no API key). The default timeframe set is
  ``15m,1h``: fixture data exists for 15m (the 4 fixture instruments),
  and the 1h timeframe is honestly reported UNSUPPORTED — the MTF
  result is explicitly INCOMPLETE, never a fabricated full alignment.

LIVE analysis is OPT-IN and separated:

* ``--provider yahoo`` uses the OPTIONAL live/near-live Yahoo provider
  (requires ``yfinance``; makes real network requests), whose native
  intraday intervals include both ``15m`` and ``1h``.

``--reference-now`` (ISO-8601, UTC) forces a deterministic reference
time; the default is a fixed deterministic sentinel for fixture runs
(fully reproducible) and wall-clock for live runs.

Exit codes: 0 whenever the analysis ran (per-symbol / per-timeframe
findings are reported honestly, never treated as CLI failure); 2 for bad
args; 1 on runtime failure. Banner: MULTI-TIMEFRAME MARKET-STATE
ANALYSIS ONLY — no setups, no ranking, no broker execution.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Deterministic reference time used for default FIXTURE runs (a Friday
#: 11:00 IST trading-weekday instant) so the fixture path never depends
#: on wall-clock time and its freshness grading is reproducible.
_FIXTURE_DETERMINISTIC_NOW = datetime(2026, 9, 4, 5, 30, tzinfo=UTC)  # 11:00 IST Fri

_DEFAULT_TIMEFRAMES = ("15m", "1h")


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


def _parse_timeframes(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return _DEFAULT_TIMEFRAMES
    labels = [t.strip() for t in raw.split(",")]
    labels = [t for t in labels if t]
    if not labels:
        raise argparse.ArgumentTypeError(
            "--timeframes produced an empty list.",
        )
    from engine.config.mtf_analysis_config import canonicalize_timeframes

    try:
        return canonicalize_timeframes(tuple(labels))
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --timeframes {raw!r}: {exc}",
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_mtf.py",
        description=(
            "NIFTY Top 200 multi-timeframe market-state analyzer "
            "(Checkpoint 19.4). Descriptive tool across configured "
            "intraday timeframes."
        ),
    )
    parser.add_argument(
        "--provider",
        default="fixture",
        choices=("fixture", "yahoo"),
        help=(
            "Market-data provider: 'fixture' (default, deterministic, "
            "offline; 15m data, 1h explicitly unsupported) or 'yahoo' "
            "(OPT-IN live/near-live; native 15m + 1h; requires yfinance)."
        ),
    )
    parser.add_argument(
        "--timeframes",
        default=None,
        help=(
            "Comma-separated canonical intraday timeframes, ascending "
            "duration (default '15m,1h'). Validated: canonical labels, "
            "no duplicates, no unknown strings."
        ),
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
            "ISO-8601 timezone-aware reference instant (UTC recommended). "
            "Deterministic fixture default when omitted."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable MTF analysis projection as JSON.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        reference_now = _parse_reference_now(args.reference_now, args.provider)
    except argparse.ArgumentTypeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    try:
        timeframes = _parse_timeframes(args.timeframes)
    except argparse.ArgumentTypeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    from engine.config.universe_boundary import UniverseBuilder

    if args.instruments:
        names = [n.strip().upper() for n in args.instruments.split(",")]
        names = [n for n in names if n]
        if not names:
            print("ERROR: --instruments produced an empty list.", file=sys.stderr)
            return 2
        universe = UniverseBuilder.custom(names, label="cli explicit")
        universe_label = f"custom ({len(names)})"
    else:
        universe = None  # engine default: validated NIFTY Top 200
        from engine.config.universe_boundary import DEFAULT_NIFTY200_UNIVERSE

        universe = DEFAULT_NIFTY200_UNIVERSE
        universe_label = "NIFTY Top 200"

    from dashboard.mtf_analysis import MtfAnalysisEngine, MtfAnalysisViewBuilder
    from engine.reporting.mtf_analysis import MtfAnalysisFormatter

    engine = MtfAnalysisEngine.build(
        args.provider, timeframes=timeframes,
    )
    analysis = engine.analyze_universe(
        universe, reference_now=reference_now,
    )

    if args.json:
        print(
            json.dumps(
                MtfAnalysisViewBuilder.analysis_to_jsonable(analysis),
                indent=2,
                sort_keys=True,
            ),
        )
    else:
        formatter = MtfAnalysisFormatter()
        print(
            f"[i] universe  : {universe_label} "
            f"({analysis.universe_instrument_count} instruments)"
            f"  provider={args.provider}  timeframes={','.join(timeframes)}"
        )
        print(formatter.format_summary(analysis))
        for result in analysis.results:
            print(f"\n=== {result.instrument} ===")
            print(formatter.format_symbol(result))
        last = analysis.results[-1] if analysis.results else None
        if last is not None:
            print("[done] summary :", formatter.format_summary(analysis))
        print(
            "\nMULTI-TIMEFRAME MARKET-STATE ANALYSIS ONLY — no setups, "
            "no ranking, no entry/stop/target, no broker execution.",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())