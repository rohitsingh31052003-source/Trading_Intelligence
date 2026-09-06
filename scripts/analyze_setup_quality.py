#!/usr/bin/env python3
"""
Operator-facing CLI for the Checkpoint 19.5 SETUP-QUALITY INTELLIGENCE
analyzer.

A THIN command-line interface over the FROZEN 19.4 MTF market-state
layer + the NEW 19.5 setup-quality orchestration. It answers, for one
or several symbols at one reference instant:

    "Given the market state currently available for this instrument,
     does it contain a potentially meaningful trading setup, how strong
     is that setup according to the defined quality model, and why?"

The CLI implements NO trading intelligence beyond setup-quality
assessment:

* NO trade execution, NO broker order placement, NO portfolio/position
  management, NO automated entries/exits, NO user notifications, NO
  setup lifecycle/state persistence, NO forward-testing infrastructure,
  NO reliability/watchdog framework.
* A QUALIFIED setup is a coherent candidate worth further evaluation —
  never a BUY/SELL verdict, never a prediction, never a guarantee of
  profitability.

Behaviour by default is FULLY OFFLINE and DETERMINISTIC:

* ``--provider fixture`` (default) uses the local deterministic
  fixtures (no network, no API key). The default timeframe set is
  ``15m,1h``: fixture data exists for 15m (the 4 fixture instruments);
  the 1h frame is honestly reported UNSUPPORTED, so the MTF relation is
  INCOMPLETE and the setup quality is explicitly capped (never a
  fabricated full alignment).

LIVE analysis is OPT-IN and separated:

* ``--provider yahoo`` uses the OPTIONAL live/near-live Yahoo provider
  (requires ``yfinance``; makes real network requests), whose native
  intraday intervals include both ``15m`` and ``1h``.

``--reference-now`` (ISO-8601, UTC) forces a deterministic reference
time; the default is a fixed deterministic sentinel for fixture runs
(fully reproducible) and wall-clock for live runs.

Exit codes: 0 whenever the analysis ran (per-symbol / per-timeframe
findings are reported honestly, never treated as CLI failure); 2 for
bad args; 1 on runtime failure. Banner: SETUP-QUALITY INTELLIGENCE ONLY
— no trade execution, no alerts, no setup lifecycle, no broker
execution.
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
        prog="analyze_setup_quality.py",
        description=(
            "NIFTY Top 200 setup-quality intelligence analyzer "
            "(Checkpoint 19.5). Descriptive setup-quality assessment "
            "over the frozen 19.4 multi-timeframe market-state layer."
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
        "--top-n",
        type=int,
        default=None,
        help=(
            "Cap on the number of QUALIFIED candidates surfaced in the "
            "top-N view (default: all qualified candidates). The full "
            "ranked candidate set remains available internally."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the machine-readable setup-quality projection as JSON.",
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

    from engine.config.universe_boundary import (
        DEFAULT_NIFTY200_UNIVERSE,
        UniverseBuilder,
    )

    if args.instruments is not None:
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

    if args.top_n is not None and args.top_n < 1:
        print("ERROR: --top-n must be >= 1.", file=sys.stderr)
        return 2

    from dashboard.mtf_analysis import MtfAnalysisEngine
    from dashboard.setup_quality import (
        SetupQualityEngine,
        SetupQualityViewBuilder,
    )
    from engine.config.setup_quality_config import SetupQualityConfig
    from engine.reporting.setup_quality import SetupQualityFormatter

    mtf_engine = MtfAnalysisEngine.build(args.provider, timeframes=timeframes)
    analysis = mtf_engine.analyze_universe(universe, reference_now=reference_now)

    # Primary (setup) timeframe = lowest-duration configured frame.
    primary = timeframes[0]

    # Completed primary-timeframe candles per symbol (canonical 19.2
    # path). Missing / unsupported symbols yield empty candles and the
    # data gate reports UNAVAILABLE/INCOMPLETE honestly.
    from dashboard.intraday_coverage import IntradayCoverageEngine

    coverage_engine = IntradayCoverageEngine.build(
        args.provider, timeframe=primary,
    )
    primary_candles: dict[str, list] = {}
    for instrument in analysis.instruments:
        try:
            series = coverage_engine.provider.fetch(
                instrument, primary, reference_now=reference_now,
            )
            from dashboard.data_provider import split_completed_candles

            boundary = split_completed_candles(
                tuple(series.setup_candles or ()), primary, reference_now,
            )
            valid = [
                c for c in boundary.completed
                if _valid_candle(c)
            ]
            primary_candles[instrument] = valid
        except Exception:  # per-symbol failure isolation
            primary_candles[instrument] = []

    sq_engine = SetupQualityEngine(
        SetupQualityConfig(max_qualified=args.top_n),
    )
    result = sq_engine.evaluate_universe(
        analysis,
        primary_candles_by_instrument=primary_candles,
    )

    if args.json:
        print(
            json.dumps(
                SetupQualityViewBuilder.universe_to_jsonable(result),
                indent=2,
                sort_keys=True,
            ),
        )
    else:
        formatter = SetupQualityFormatter()
        print(
            f"[i] universe  : {universe_label} "
            f"({result.universe_instrument_count} instruments)"
            f"  provider={args.provider}  timeframes={','.join(timeframes)}"
            f"  primary={result.primary_timeframe}"
        )
        print(formatter.format_universe(result))
        for symbol_result in result.results:
            print(f"\n=== {symbol_result.instrument} ===")
            print(formatter.format_result(symbol_result))
        print(
            "\nSETUP-QUALITY INTELLIGENCE ONLY — no trade execution, "
            "no alerts, no setup lifecycle, no broker execution.",
        )
    return 0


def _valid_candle(candle) -> bool:
    """Defensive candle validation (never raises; drops impossible rows)."""
    try:
        from engine.data.validator import DataValidator

        DataValidator.validate_candle(candle)
        return True
    except (ValueError, TypeError):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
