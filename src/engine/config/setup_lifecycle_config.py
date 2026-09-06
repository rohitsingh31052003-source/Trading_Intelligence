"""
Configuration for the setup lifecycle layer (Checkpoint 19.6).

All lifecycle thresholds, identity versioning and state-machine
parameters live here; no magic numbers are embedded in the lifecycle
engine or models. The defaults are deliberately conservative and
deterministic.

LIFECYCLE PARAMETERS (documented):

``max_consecutive_missing_observations``
    The number of CONSECUTIVE clean scan cycles in which a setup's
    identity is NOT present before the lifecycle EXPIRES. A "clean scan
    cycle" is a cycle where the symbol was successfully evaluated and
    the market was observed but produced NO directional setup
    candidate (19.5 ``NO_SETUP`` / a ``WATCH`` without direction).
    Data-failure observations (``INCOMPLETE`` / ``UNAVAILABLE`` /
    provider failure / missing symbol) do NOT count toward expiry —
    the distinction between "setup absent" and "data unavailable" is
    preserved structurally. Default 5.

    Why a cycle-count rule rather than a wall-clock timeout: the
    continuous scanner produces a deterministic SCAN-CYCLE stream
    (checkpoint 19.3), and the natural, scan-interval-independent
    measure of "how long has the setup been gone" is the number of
    consecutive cycles in which it was not observed. A wall-clock
    timeout would silently depend on the operator's scan interval and
    on wall-clock time; the cycle-count rule is deterministic,
    injected-clock-free and reproducible.

``identity_version``
    Version of the setup-identity rule embedded in every computed
    setup id. Bumping this version intentionally produces NEW setup
    ids for the same analytical properties (an identity-rule change
    creates new lifecycles, never silent reuse of old ones). Default 1.

``confirm_on_qualified``
    When True (default), a setup is CONFIRMED as soon as ONE observation
    carries a qualified 19.5 assessment (``QUALIFIED`` status / reused
    11Q ``POTENTIAL_SETUP``). This is the smallest deterministic
    confirmation rule that mirrors 19.5's own quality bar; it is the
    ONLY confirmation mode implemented in Checkpoint 19.6.

LIFECYCLE MODEL / TRANSITION-RULE VERSIONING:

The lifecycle layer is explicitly versioned:

    ``LIFECYCLE_MODEL_VERSION`` (module constant)
        Version of the lifecycle MODEL structures (states, fields).
        Bumped only by a structural model change. Default 1.

    Transition-rule version
        Version of the state-transition rules. Derived from the
        lifecycle config snapshot (a config change — e.g. a new
        expiry count — is a transition-rule change and is embedded in
        every deterministic lifecycle-processing identity). Default
        ``"1"``.

The audit document (``docs/checkpoint_19_6_setup_lifecycle_audit.md``)
records these versioning conventions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


#: Lifecycle MODEL structural version (documented; see audit doc).
LIFECYCLE_MODEL_VERSION = 1


def _identity(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class SetupLifecycleConfig:
    """
    Configuration for the setup lifecycle engine.

    Every attribute is validated at construction (no magic values).
    """

    max_consecutive_missing_observations: int = 5
    identity_version: int = 1
    confirm_on_qualified: bool = True

    def __post_init__(self) -> None:
        if isinstance(
            self.max_consecutive_missing_observations, bool,
        ) or not isinstance(self.max_consecutive_missing_observations, int):
            raise TypeError(
                "max_consecutive_missing_observations must be an int, "
                "not bool.",
            )
        if self.max_consecutive_missing_observations < 1:
            raise ValueError(
                "max_consecutive_missing_observations must be >= 1.",
            )
        if isinstance(self.identity_version, bool) or not isinstance(
            self.identity_version, int,
        ):
            raise TypeError("identity_version must be an int, not bool.")
        if self.identity_version < 1:
            raise ValueError("identity_version must be >= 1.")
        if not isinstance(self.confirm_on_qualified, bool):
            raise TypeError("confirm_on_qualified must be a bool.")

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable config snapshot (sorted)."""

        return tuple(sorted(
            (
                ("max_consecutive_missing_observations",
                 str(self.max_consecutive_missing_observations)),
                ("identity_version", str(self.identity_version)),
                ("confirm_on_qualified", str(self.confirm_on_qualified)),
                ("lifecycle_model_version", str(LIFECYCLE_MODEL_VERSION)),
            ),
        ))


#: Default transition-rule version string (config-derived).
def transition_rule_version(config: SetupLifecycleConfig) -> str:
    """Deterministic transition-rule version derived from the config.

    Any lifecycle-config change is a transition-rule change; the version
    is the sha256-prefix of the config snapshot so deterministic
    lifecycle-processing identities embed the exact rule-set version.
    """

    payload = ";".join(f"{k}={v}" for k, v in config.snapshot())
    return _identity(payload)


__all__ = [
    "LIFECYCLE_MODEL_VERSION",
    "SetupLifecycleConfig",
    "transition_rule_version",
]