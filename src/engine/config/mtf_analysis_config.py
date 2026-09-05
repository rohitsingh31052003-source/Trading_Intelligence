"""
Configuration for the multi-timeframe market-state analysis layer
(Checkpoint 19.4).

All timeframe configuration lives here; no magic values are embedded in
the MTF analysis engine. The defaults are deliberately small and
deterministic. They are NOT calibrated to any market and do NOT imply
any setup / trading semantics.

TIMEFRAME SET (documented rationale — see the Checkpoint 19.4 audit doc):

The default set is ``("15m", "1h")``. This is the smallest sensible
intraday pair supported by the existing architecture:

* ``15m`` is the canonical intraday execution/setup timeframe the
  19.2 coverage layer already grades (the fixture provider and the
  dashboard scanner both default to it).
* ``1h`` (canonical; alias ``60m``) is the smallest NATIVE
  higher-intraday interval that the OPTIONAL live Yahoo provider serves
  (Yahoo native intervals include ``1h`` and ``60m``; ``30m`` is also
  native but ``1h`` gives a genuinely higher timeframe, and 30m vs 15m
  is only a 2x ratio with no native 30m fixture coverage).
* Fixture runs therefore observe 15m data and honestly report the 1h
  timeframe as UNSUPPORTED (per the 19.2 capability-discovery
  convention) — an explicit INCOMPLETE/unsupported MTF result, never a
  fabricated full alignment.

The configuration is intentionally easy to change later WITHOUT a
redesign: an operator may construct ``MtfAnalysisConfig(timeframes=("5m",
"15m", "1h"))``. Every timeframe label is canonicalized via the existing
:func:`engine.data.historical_times.canonical_timeframe`, duplicates are
rejected, and unknown strings are rejected (never silently accepted).
Provider support is discovered per (provider, instrument, timeframe) at
analysis time by the 19.2 capability layer — the config never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


def canonicalize_timeframes(
    timeframes: Sequence[str],
    *,
    allow_intraday_only: bool = True,
) -> tuple[str, ...]:
    """
    Canonicalize + validate a timeframe set.

    * every label is canonicalized via the existing
      :func:`engine.data.historical_times.canonical_timeframe`;
    * unknown / non-string labels are REJECTED (``ValueError``) —
      arbitrary strings never become valid timeframes;
    * duplicates are REJECTED (``ValueError``) — a set with one
      timeframe listed twice is invalid;
    * a non-empty set is required;
    * when ``allow_intraday_only`` is True (MTF market-state layer is
      intraday-only at 19.4) the daily ``"1D"`` label is rejected;
    * timeframes must be supplied in ascending-duration order
      (lowest-duration first) — an unordered/descending set is REJECTED
      rather than silently re-sorted, so the presented ordering is always
      explicit and deterministic.

    Returns the canonical labels in the ORDER supplied (deterministic,
    caller-visible).
    """

    from engine.data.historical_times import canonical_timeframe

    if isinstance(timeframes, str):
        raise TypeError("timeframes must be a sequence of strings")

    if not timeframes:
        raise ValueError("at least one timeframe is required")

    canonical: list[str] = []
    seen: set[str] = set()
    for tf in timeframes:
        if not isinstance(tf, str):
            raise TypeError(
                f"timeframe labels must be str, got {type(tf).__name__}",
            )
        resolved = canonical_timeframe(tf)
        if resolved is None:
            raise ValueError(
                f"unknown timeframe label {tf!r} — arbitrary strings are "
                "never silently accepted as valid timeframes.",
            )
        if allow_intraday_only and resolved == "1D":
            raise ValueError(
                f"timeframe {tf!r} resolves to '1D' which is not an "
                "intraday timeframe; the MTF market-state layer is "
                "intraday-only (daily context belongs to later "
                "checkpoints).",
            )
        if resolved in seen:
            raise ValueError(
                f"duplicate timeframe {resolved!r} in configuration — "
                "duplicate timeframes are rejected.",
            )
        seen.add(resolved)
        canonical.append(resolved)

    ordered = tuple(canonical)
    _validate_ordering(ordered)
    return ordered


def _validate_ordering(
    timeframes: tuple[str, ...],
) -> None:
    """Deterministically order frames lowest-duration first.

    The MTF layer always presents timeframe states as TIME
    (lowest-duration) -> HIGH (highest-duration) regardless of the order
    the caller supplied them in. Any two distinct intraday frames are
    strictly ordered by duration, so the sorted key is deterministic.
    """

    from engine.data.historical_times import timeframe_seconds

    if len(timeframes) < 2:
        return
    durations = [timeframe_seconds(tf) for tf in timeframes]
    if any(d is None for d in durations):
        raise ValueError(
            "timeframe durations must all be known (canonical labels).",
        )
    ordered = tuple(sorted(timeframes, key=lambda tf: timeframe_seconds(tf)))
    if ordered != timeframes:
        raise ValueError(
            "timeframes must be supplied in ascending duration order "
            "(lowest-duration first, e.g. ('5m', '15m', '1h')). The MTF "
            "layer presents frames low->high; duplicate or unordered "
            "sets are rejected rather than silently re-sorted.",
        )
    if len(ordered) != len(set(ordered)):
        raise ValueError("timeframe set may not contain duplicates.")


@dataclass(frozen=True, slots=True)
class MtfAnalysisConfig:
    """
    Configuration for :class:`dashboard.mtf_analysis.MtfAnalysisEngine`.

    Attributes:

    timeframes
        Canonical intraday timeframe set, ordered lowest-duration first
        (default ``("15m", "1h")``). Validated:
        every label canonical, no duplicates, no unknown strings,
        ascending-duration order, intraday-only.

    max_lookback_bars
        Maximum number of completed candles examined per timeframe when
        computing the timeframe's normalized market state (the existing
        market-context engines operate on a bounded recent window).
        Positive; default ``150``.

    min_candles_per_timeframe
        Minimum number of completed candles required before a
        timeframe's market state (structure/trend classification) is
        attempted. Below this the state is ``INSUFFICIENT_HISTORY``
        (explicit, never silently guessed). Default ``8``.
    """

    timeframes: tuple[str, ...] = ("15m", "1h")
    max_lookback_bars: int = 150
    min_candles_per_timeframe: int = 8

    def __post_init__(self) -> None:
        clean = canonicalize_timeframes(self.timeframes)
        object.__setattr__(self, "timeframes", clean)
        if isinstance(self.max_lookback_bars, bool) or not isinstance(
            self.max_lookback_bars, int,
        ):
            raise TypeError("max_lookback_bars must be an int, not bool.")
        if self.max_lookback_bars <= 0:
            raise ValueError("max_lookback_bars must be positive.")
        if isinstance(self.min_candles_per_timeframe, bool) or not isinstance(
            self.min_candles_per_timeframe, int,
        ):
            raise TypeError("min_candles_per_timeframe must be an int, not bool.")
        if self.min_candles_per_timeframe <= 0:
            raise ValueError("min_candles_per_timeframe must be positive.")

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable config snapshot (sorted)."""

        return (
            ("timeframes", ",".join(self.timeframes)),
            ("max_lookback_bars", str(self.max_lookback_bars)),
            ("min_candles_per_timeframe", str(self.min_candles_per_timeframe)),
        )


__all__ = [
    "MtfAnalysisConfig",
    "canonicalize_timeframes",
]