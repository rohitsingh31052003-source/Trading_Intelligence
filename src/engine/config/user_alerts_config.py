"""
Configuration for the user alerts layer (Checkpoint 19.7).

All alert-policy thresholds and knobs live here; no magic numbers are
embedded in the alert engine or models. The defaults are deliberately
conservative and deterministic.

ALERT POLICY (documented):

``enabled``
    Master switch. When False, NO alert is emitted (every eligible
    lifecycle event is recorded as SUPPRESSED with the reason "alerting
    disabled by configuration"). Deterministic; never a silent drop.

``eligible_kinds``
    The alert event kinds that MAY generate alerts. Defaults to the
    MEANINGFUL lifecycle transitions: ``SETUP_CONFIRMED`` /
    ``SETUP_INVALIDATED`` / ``SETUP_EXPIRED``. ``SETUP_DETECTED`` is
    NOT eligible by default (a detection is informational and would be
    notification spam on the Top-200 universe); it can be enabled
    explicitly. This is the ONLY alert-eligibility knob — the alert
    vocabulary itself is fixed (see the audit document).

``min_quality_classification``
    Optional minimum 19.5 quality classification (a
    ``SetupQualityClassification`` member name) an alert's carried
    quality snapshot must reach for the alert to be eligible. ``None``
    = no quality gate (default). This is a FILTER on the carried 19.5
    classification — it NEVER alters the 19.5 quality semantics and
    NEVER recalculates quality.

``max_per_cycle``
    Optional cap on the number of alerts EMITTED per alert-processing
    cycle (``None`` = no cap). When the cap is reached, remaining
    eligible alerts are recorded as SUPPRESSED with the reason
    "maximum alerts per cycle reached" — explicit and auditable, never
    a silent drop. Alerts are selected deterministically (see the
    audit document "Multiple-alert ordering").

``max_qualified_top_n``
    Optional cap on the number of QUALIFIED candidates the alert layer
    will consider per cycle (a consumption of the EXISTING 19.5
    ``ranked_qualified`` / ``top_n`` ranking — NEVER a second ranking
    algorithm). ``None`` = all qualified candidates are considered.

``severity_policy``
    Reserved for a future severity-policy knob. Severity is currently a
    FIXED function of the event kind (see
    :func:`engine.models.user_alerts.severity_for_kind`) and is NOT
    configurable; this field is always ``"fixed"`` and validated.

``label`` / ``metadata``
    Identification for a run (metadata is a deterministic sequence of
    ``(name, value)`` pairs).

ALERT POLICY VERSIONING:

The alert layer is explicitly versioned:

    ``ALERT_MODEL_VERSION`` (module constant)
        Version of the alert MODEL structures (states, fields). Bumped
        only by a structural model change. Default 1.

    Alert-policy version
        Version of the alert RULES (eligibility / severity / caps).
        Derived from the config snapshot (a config change — e.g. a new
        eligible kind — is a policy change and is embedded in every
        deterministic alert identity). Default ``"1"``.

The audit document (``docs/checkpoint_19_7_user_alerts_audit.md``)
records these versioning conventions.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


def _identity(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


#: The canonical alert event kinds (fixed vocabulary; see the model).
_ALL_KINDS = ("SETUP_DETECTED", "SETUP_CONFIRMED", "SETUP_INVALIDATED", "SETUP_EXPIRED")

#: The default eligible kinds (the MEANINGFUL lifecycle transitions).
_DEFAULT_ELIGIBLE = ("SETUP_CONFIRMED", "SETUP_INVALIDATED", "SETUP_EXPIRED")


@dataclass(frozen=True, slots=True)
class UserAlertConfig:
    """
    Configuration for the user alerts engine.

    Every attribute is validated at construction (no magic values).
    """

    enabled: bool = True
    eligible_kinds: tuple[str, ...] = _DEFAULT_ELIGIBLE
    min_quality_classification: str | None = None
    max_per_cycle: int | None = None
    max_qualified_top_n: int | None = None
    severity_policy: str = "fixed"
    label: str = ""
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be a bool.")
        if not isinstance(self.eligible_kinds, tuple):
            raise TypeError("eligible_kinds must be a tuple of kind names.")
        if not self.eligible_kinds:
            raise ValueError("eligible_kinds must not be empty.")
        seen: set[str] = set()
        for kind in self.eligible_kinds:
            if not isinstance(kind, str):
                raise TypeError("eligible_kinds entries must be str.")
            if kind not in _ALL_KINDS:
                raise ValueError(
                    f"unknown alert event kind {kind!r} — the alert "
                    "vocabulary is fixed.",
                )
            if kind in seen:
                raise ValueError(
                    f"duplicate eligible kind {kind!r}.",
                )
            seen.add(kind)
        if self.min_quality_classification is not None:
            from engine.models.setup_quality import SetupQualityClassification

            if not isinstance(self.min_quality_classification, str):
                raise TypeError(
                    "min_quality_classification must be a str or None.",
                )
            try:
                SetupQualityClassification(self.min_quality_classification)
            except ValueError as exc:
                raise ValueError(
                    "min_quality_classification must be a "
                    "SetupQualityClassification member name "
                    f"(got {self.min_quality_classification!r}).",
                ) from exc
            if self.min_quality_classification in ("INCOMPLETE", "UNAVAILABLE"):
                raise ValueError(
                    "min_quality_classification must not be a data-gate "
                    "classification (INCOMPLETE / UNAVAILABLE).",
                )
        if self.max_per_cycle is not None:
            if isinstance(self.max_per_cycle, bool) or not isinstance(
                self.max_per_cycle, int,
            ):
                raise TypeError("max_per_cycle must be an int or None.")
            if self.max_per_cycle < 1:
                raise ValueError("max_per_cycle must be >= 1 when set.")
        if self.max_qualified_top_n is not None:
            if isinstance(self.max_qualified_top_n, bool) or not isinstance(
                self.max_qualified_top_n, int,
            ):
                raise TypeError("max_qualified_top_n must be an int or None.")
            if self.max_qualified_top_n < 1:
                raise ValueError("max_qualified_top_n must be >= 1 when set.")
        if self.severity_policy != "fixed":
            raise ValueError(
                "severity_policy must be 'fixed' — severity is a fixed "
                "function of the event kind and is NOT configurable.",
            )
        if not isinstance(self.label, str):
            raise TypeError("label must be a str.")
        if not isinstance(self.metadata, tuple):
            raise TypeError("metadata must be a tuple of (name, value) pairs.")
        for pair in self.metadata:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise TypeError(
                    "metadata entries must be (name, value) pairs.",
                )
            if not isinstance(pair[0], str) or not isinstance(pair[1], str):
                raise TypeError(
                    "metadata name and value must be str.",
                )

    def is_eligible(self, kind_name: str) -> bool:
        """Deterministic eligibility check for one event kind name."""
        return kind_name in self.eligible_kinds

    def snapshot(self) -> tuple[tuple[str, str], ...]:
        """Deterministic, auditable config snapshot (sorted)."""

        return tuple(sorted(
            (
                ("enabled", str(self.enabled)),
                ("eligible_kinds", ",".join(self.eligible_kinds)),
                (
                    "min_quality_classification",
                    self.min_quality_classification or "",
                ),
                ("max_per_cycle", str(self.max_per_cycle)),
                ("max_qualified_top_n", str(self.max_qualified_top_n)),
                ("severity_policy", self.severity_policy),
                ("label", self.label),
                ("metadata", ";".join(f"{k}={v}" for k, v in self.metadata)),
            ),
        ))


#: Default alert-policy version string (config-derived).
def alert_policy_version(config: UserAlertConfig) -> str:
    """Deterministic alert-policy version derived from the config.

    Any alert-config change is an alert-policy change; the version is
    the sha256-prefix of the config snapshot so deterministic alert
    identities embed the exact policy-set version.
    """

    payload = ";".join(f"{k}={v}" for k, v in config.snapshot())
    return _identity(payload)


__all__ = [
    "UserAlertConfig",
    "alert_policy_version",
]
