"""
Configuration for the validation / forward-testing layer (Checkpoint 19.9).

All forward-validation thresholds, measurement windows and policy knobs
live here; no magic numbers are embedded in the forward-observation /
outcome engines or models. The defaults are deliberately conservative,
deterministic and DOCUMENTED (each threshold carries its reasoning in
its field docstring).

FORWARD-VALIDATION POLICY (documented):

``ForwardValidationConfig`` bundles:

* the technical-policy versions the FORWARD SESSION must record so a
  later configuration change can NEVER silently reinterpret a previous
  session (universe version, MTF config snapshot, setup-quality policy
  version, lifecycle policy version, alert policy version, reliability
  policy version, provider, timeframes, instrument universe, measurement
  window);
* the outcome-measurement window (``max_holding_bars`` — the number of
  COMPLETED forward primary-timeframe candles the outcome evaluator may
  inspect strictly after the observation timestamp);
* storage / idempotency knobs (``record_duplicates``,
  ``reject_out_of_order``) and session / reporting metadata.

DESIGN RULES:

* Frozen + slots dataclass; every attribute validated at construction
  (no magic values, no unsafe silent substitution).
* No strategy / scoring / ranking / alert / lifecycle semantics are
  configurable here — 19.9 records those policy versions VERBATIM from
  the FROZEN 19.4-19.8 configurations; it never alters their semantics.
* Default test mode is OFFLINE / DETERMINISTIC; live mode is OPT-IN at
  the CLI / runner level (never in this config).
* The validation policy version is derived deterministically from the
  canonical snapshot, so any 19.9 config change produces a NEW validation
  policy identity (old observations stay under the old policy).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _identity(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: Forward-validation MODEL structural version (documented; audit doc).
FORWARD_VALIDATION_MODEL_VERSION = 1

#: Schema version for persisted forward-observation / session documents
#: (19.9 persistence).
FORWARD_OBSERVATION_SCHEMA_VERSION = 1
FORWARD_SESSION_SCHEMA_VERSION = 1

#: Default measurement horizon (number of COMPLETED forward primary
#: candles strictly after ``T`` the outcome evaluator may inspect).
#: ``5`` = a 75-minute horizon on 15m candles — a short, conservative
#: forward window that keeps the outcome DESCRIPTIVE without pretending
#: long-horizon predictive power.
DEFAULT_MAX_HOLDING_BARS = 5

#: Timeframe of the forward candles the LATENCY estimator uses to infer
#: the expected candle duration when the observation's own primary
#: timeframe duration is unknown. Canonical intraday label.
DEFAULT_LATENCY_TIMEFRAME = "15m"


@dataclass(frozen=True, slots=True)
class ForwardValidationConfig:
    """
    Bundled forward-validation configuration (Checkpoint 19.9).

    Attributes:

    provider
        Market-data provider name (``"fixture"`` default deterministic /
        offline; ``"yahoo"`` OPT-IN live). Recorded on every session.

    timeframes
        Canonical intraday timeframe set (``"15m","1h"`` by default).
        The PRIMARY (setup / measurement) timeframe is the lowest-
        duration label (``timeframes[0]``) and must be an intraday
        timeframe. Recorded on every session.

    universe
        The canonical instrument tuple the session scans (default: the
        NIFTY Top 200 constituents WITHOUT the benchmark — the benchmark
        index is not a tradeable stock constituent). ``None`` is
        resolved lazily by the runner/CLI.

    max_holding_bars
        Outcome measurement window (positive; a forward outcome may
        only inspect, at most, this many COMPLETED primary-timeframe
        candles strictly after the observation timestamp).

    record_duplicates
        When True a duplicate observation (same observation identity as
        an already-recorded one) is recorded again with its integrity
        flag (useful for count reconciliation). When False (default) a
        duplicate is skipped atomically and surfaces only in counts.

    reject_out_of_order
        When True an out-of-order observation (timestamp strictly older
        than the session's latest observation timestamp) is rejected
        with ``late_rejected=True`` and NOT recorded (default True).
        When False it is recorded as an out-of-order entry.

    alert_policy_version / lifecycle_policy_version /
    setup_quality_policy_version / mtf_policy_version /
    reliability_policy_version / universe_version / measurement_window
        Explicitly recorded policy versions (deterministic; a change to
        any frozen 19.4-19.8 config is a policy change captured by the
        caller and recorded on every observation / session).

    label / metadata
        Identification for a run (metadata is a deterministic sequence
        of ``(name, value)`` pairs).

    VALIDATION POLICY VERSION:
        Derived from the canonical snapshot; a 19.9 config change yields
        a NEW validation-policy identity. Old observations remain under
        the old policy (configured-version separation).
    """

    provider: str = "fixture"
    timeframes: tuple[str, ...] = ("15m", "1h")
    universe: tuple[str, ...] | None = None
    max_holding_bars: int = DEFAULT_MAX_HOLDING_BARS
    record_duplicates: bool = False
    reject_out_of_order: bool = True
    alert_policy_version: str = ""
    lifecycle_policy_version: str = ""
    setup_quality_policy_version: str = ""
    mtf_policy_version: str = ""
    reliability_policy_version: str = ""
    universe_version: str = ""
    measurement_window: str = ""
    label: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise TypeError("provider must be a non-empty str.")
        if not isinstance(self.timeframes, tuple) or not self.timeframes:
            raise ValueError("timeframes must be a non-empty tuple.")
        from engine.data.historical_times import canonical_timeframe

        for tf in self.timeframes:
            if not isinstance(tf, str):
                raise TypeError("timeframe labels must be str.")
            canon = canonical_timeframe(tf)
            if canon is None or canon == "1D":
                raise ValueError(
                    f"timeframes must be canonical INTRADAY timeframes "
                    f"(got {tf!r}).",
                )
        if isinstance(self.max_holding_bars, bool) or not isinstance(
            self.max_holding_bars, int,
        ):
            raise TypeError("max_holding_bars must be an int, not bool.")
        if self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be positive.")
        if not isinstance(self.record_duplicates, bool):
            raise TypeError("record_duplicates must be a bool.")
        if not isinstance(self.reject_out_of_order, bool):
            raise TypeError("reject_out_of_order must be a bool.")
        if not isinstance(self.label, str):
            raise TypeError("label must be a str.")
        if not isinstance(self.metadata, tuple):
            raise TypeError("metadata must be a tuple of (name, value) pairs.")
        for pair in self.metadata:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError("metadata entries must be (name, value) pairs.")
            if not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise TypeError("metadata name and value must be str.")
        for name in (
            "alert_policy_version",
            "lifecycle_policy_version",
            "setup_quality_policy_version",
            "mtf_policy_version",
            "reliability_policy_version",
            "universe_version",
            "measurement_window",
        ):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a str.")
        if self.universe is not None:
            if not isinstance(self.universe, tuple):
                raise TypeError("universe must be a tuple of str or None.")
            for name in self.universe:
                if not isinstance(name, str) or not name.strip():
                    raise TypeError("universe entries must be non-empty str.")

    @property
    def primary_timeframe(self) -> str:
        """The primary (setup / measurement) timeframe label."""
        return self.timeframes[0]

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable full config snapshot (sorted)."""
        return tuple(sorted(
            (
                ("forward_validation_model_version", str(FORWARD_VALIDATION_MODEL_VERSION)),
                ("provider", self.provider),
                ("timeframes", ",".join(self.timeframes)),
                ("primary_timeframe", self.primary_timeframe),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("record_duplicates", str(self.record_duplicates)),
                ("reject_out_of_order", str(self.reject_out_of_order)),
                ("alert_policy_version", self.alert_policy_version),
                ("lifecycle_policy_version", self.lifecycle_policy_version),
                ("setup_quality_policy_version", self.setup_quality_policy_version),
                ("mtf_policy_version", self.mtf_policy_version),
                ("reliability_policy_version", self.reliability_policy_version),
                ("universe_version", self.universe_version),
                ("measurement_window", self.measurement_window),
                ("label", self.label),
            ) + tuple(
                (f"metadata.{k}", str(v)) for k, v in sorted(self.metadata)
            ),
        ))

    def policy_version(self) -> str:
        """
        Deterministic validation-policy version.

        Any 19.9 config change is a validation-policy change; the
        version is the sha256-prefix of the canonical snapshot so every
        deterministic forward observation / session embeds the exact
        rule-set version.
        """

        payload = ";".join(f"{k}={v}" for k, v in self.snapshot())
        return _identity(payload)


__all__ = [
    "DEFAULT_LATENCY_TIMEFRAME",
    "DEFAULT_MAX_HOLDING_BARS",
    "FORWARD_OBSERVATION_SCHEMA_VERSION",
    "FORWARD_SESSION_SCHEMA_VERSION",
    "FORWARD_VALIDATION_MODEL_VERSION",
    "ForwardValidationConfig",
]