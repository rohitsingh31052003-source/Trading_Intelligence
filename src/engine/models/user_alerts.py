"""
User alerts models (Checkpoint 19.7).

These models implement the USER ALERTS layer that answers, over the
deterministic setup-lifecycle stream built by Checkpoint 19.6:

    "Which lifecycle events are important enough to notify the user
     about, what should the notification contain, and how should
     duplicate / noisy notifications be prevented?"

The layer is DOWNSTREAM of the FROZEN 19.6 lifecycle: it consumes the
deterministic lifecycle EVENTS (lifecycle creation / confirmation /
invalidation / expiration exposed by
:class:`SetupLifecycleCycleResult` and its per-symbol
:class:`SetupLifecycleObservationResult`) and converts the MEANINGFUL
ones into deterministic, user-facing alert events. It does NOT detect
setups, does NOT calculate setup quality, does NOT create setup
identities, does NOT manage lifecycle state and does NOT reconstruct
lifecycle history.

DESIGN PRINCIPLES (documented in the Checkpoint 19.7 audit document):

* LIFECYCLE EVENT vs ALERT EVENT are SEPARATE concerns. A lifecycle
  event means "something happened to this setup"; an alert means "this
  event is important enough to tell the user". The alert layer
  CONSUMES lifecycle events — it never re-derives them.

* SMALLEST ALERT VOCABULARY justified by the 19.6 semantics. The 19.6
  lifecycle has exactly four distinguishable state-arrival events:
  DETECTED (a lifecycle instance is created in the DETECTED state) /
  CONFIRMED (a lifecycle reaches CONFIRMED — either created qualified
  or later confirmed) / INVALIDATED (superseded by an opposing
  direction) / EXPIRED (absent for N clean cycles). No other alert
  kind is justified; quality-score churn, repeated unchanged
  observations, DATA_UNAVAILABLE and non-terminal misses are lifecycle
  OBSERVATIONS, not alert EVENTS.

* DETERMINISTIC ALERT IDENTITY derived ONLY from stable event
  information: the alert-policy version + event kind + lifecycle id +
  the 19.6 observation id (the lifecycle-layer idempotency basis).
  Alert ids never depend on random UUIDs, wall-clock generation,
  provider response order or object memory addresses. The same logical
  alert always produces the same alert id; a DIFFERENT lifecycle
  transition of the same setup produces a DIFFERENT alert id (one
  setup may legitimately produce several distinct alerts).

* IDENTITY-BASED DEDUPLICATION (not time-based cooldowns). Since the
  alert id is a deterministic function of the underlying event, the
  same event processed twice — or the same scan cycle processed twice,
  or the same setup observed unchanged across many cycles — resolves
  to the SAME alert id and is SUPPRESSED without emitting a new alert.
  Repeated unchanged lifecycle observations never create new
  transitions, therefore never create new alert events.

* SEVERITY IS A SMALL, FIXED, EXPLAINABLE VOCABULARY (INFO / IMPORTANT
  / CRITICAL) bound DETERMINISTICALLY to the event kind (see
  :func:`severity_for_kind`). It is NOT configurable (severity is a
  semantic property of the event kind, not a policy knob) and is NEVER
  a disguised trade recommendation — it is not a probability, not a
  confidence and not a profitability claim.

* EXPLICIT EVENT TIMESTAMPS. The analytical timestamp of an alert is
  the upstream 19.6 observation instant (explicit, timezone-aware,
  never wall-clock). An optional delivery timestamp (for operational
  purposes) is carried SEPARATELY on the delivery result and is never
  used for analytical identity.

* NO RETROACTIVE MUTATION. :class:`AlertEvent` is a frozen, immutable
  record of what the lifecycle event carried at construction time.
  Later lifecycle events can never alter an earlier alert (proven by
  tests).

* NO EXECUTION / DELIVERY / RELIABILITY / FORWARD-TESTING SEMANTICS.
  The alert models carry analytical content only: no entry/stop/
  target, no position/quantity, no risk/reward, no order, no broker,
  no external-channel endpoint, no persistence framework (19.8 owns
  durability / recovery; 19.9 owns forward-testing).

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses.
* Optional fields use ``None`` so "unobserved" / "unavailable" is never
  silently a real value.
* ``__post_init__`` performs structural validation only; identity /
  severity logic lives in pure exported helpers.
* No wall-clock dependence: timestamps are explicit, caller-supplied,
  timezone-aware values.
* The model layer keeps NO import from ``dashboard`` (dependency
  direction: models <- config <- dashboard).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from engine.models.setup_lifecycle import (
    LifecycleObservationStatus,
    SetupLifecycleState,
)

#: Alert-event id prefix (documented, deterministic).
ALERT_ID_PREFIX = "alert-"

#: Alert-processing-cycle id prefix (documented, deterministic).
ALERT_CYCLE_ID_PREFIX = "alert-cycle-"

#: Alert MODEL structural version (documented; see audit doc).
ALERT_MODEL_VERSION = 1


def _sha256_prefix(payload: str, prefix: str) -> str:
    """Deterministic ``"<prefix>" + sha256[:16]`` over a canonical string."""

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def _canonical_name(name: str) -> str:
    """Canonicalize + validate a single instrument name."""

    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


class AlertEventKind(Enum):
    """
    The smallest alert vocabulary justified by the 19.6 lifecycle
    semantics — one alert kind per DISTINGUISHABLE lifecycle
    state-arrival event:

    SETUP_DETECTED
        A new setup lifecycle instance was created in the DETECTED
        state (a directional setup candidate began monitoring, but the
        confirmation predicate was not yet satisfied). This is the
        LIFECYCLE EVENT "detection"; whether it is ALERTED is a policy
        decision (default: not eligible — see the audit document,
        "Alert eligibility policy").

    SETUP_CONFIRMED
        A lifecycle reached CONFIRMED — either a new lifecycle created
        directly in CONFIRMED (first observation already qualified) or
        a DETECTED lifecycle confirmed by a later qualified observation.
        This is the positive meaningful event of the setup lifecycle.

    SETUP_INVALIDATED
        A lifecycle reached INVALIDATED (terminal) — superseded by an
        opposing-direction setup for the same instrument / timeframe.
        The tracked setup's directional premise no longer holds.

    SETUP_EXPIRED
        A lifecycle reached EXPIRED (terminal) — the setup's identity
        was not observed for ``max_consecutive_missing_observations``
        consecutive clean scan cycles.

    The vocabulary is intentionally minimal: there is no "score change"
    alert, no "still confirmed" alert, no "data unavailable" alert and
    no "setup observed" alert — those are lifecycle OBSERVATIONS, not
    state-arrival EVENTS, and alerting on them would be notification
    spam.
    """

    SETUP_DETECTED = "SETUP_DETECTED"
    SETUP_CONFIRMED = "SETUP_CONFIRMED"
    SETUP_INVALIDATED = "SETUP_INVALIDATED"
    SETUP_EXPIRED = "SETUP_EXPIRED"


class AlertSeverity(Enum):
    """
    Small, fixed, explainable severity vocabulary.

    Severity is a DETERMINISTIC, FIXED function of the event kind (see
    :func:`severity_for_kind`) — it is NOT configurable and NOT a
    probability / confidence / profitability claim. It is descriptive
    only: it tells the user how much attention the lifecycle event
    deserves, never whether to trade.

    INFO
        Informational lifecycle events (e.g. a setup was detected).

    IMPORTANT
        Meaningful lifecycle transitions worth the user's attention
        (e.g. a setup was confirmed).

    CRITICAL
        Terminal / disproof lifecycle events (e.g. a setup was
        invalidated or expired) — the user should be aware the tracked
        setup is no longer active.
    """

    INFO = "INFO"
    IMPORTANT = "IMPORTANT"
    CRITICAL = "CRITICAL"


#: Fixed severity mapping (event kind -> severity). Not configurable.
_SEVERITY_BY_KIND = {
    AlertEventKind.SETUP_DETECTED: AlertSeverity.INFO,
    AlertEventKind.SETUP_CONFIRMED: AlertSeverity.IMPORTANT,
    AlertEventKind.SETUP_INVALIDATED: AlertSeverity.CRITICAL,
    AlertEventKind.SETUP_EXPIRED: AlertSeverity.CRITICAL,
}


def severity_for_kind(kind: AlertEventKind) -> AlertSeverity:
    """Deterministic severity for an alert event kind."""

    if not isinstance(kind, AlertEventKind):
        raise TypeError("kind must be an AlertEventKind.")
    return _SEVERITY_BY_KIND[kind]


class AlertDeliveryStatus(Enum):
    """
    Deterministic delivery status of one alert event.

    DELIVERED
        The alert was handed to the delivery channel and the channel
        accepted it (the console sink always delivers).

    FAILED
        The delivery channel raised / rejected the alert. A delivery
        failure is a DELIVERY concern only — it NEVER invalidates a
        setup, NEVER mutates lifecycle state and NEVER alters the
        alert event itself.

    SKIPPED
        The alert was eligible but the delivery channel was not
        available / not configured (e.g. no channel registered). An
        explicit, auditable outcome — never a silent drop.

    SUPPRESSED
        The alert was NOT emitted because its deterministic alert id
        was already recorded (identity-based deduplication) or because
        the alert policy / configuration made it ineligible. Explicit
        and auditable — never a silent drop.
    """

    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    SUPPRESSED = "SUPPRESSED"


#: Lifecycle state -> the alert event kind that ARRIVES at that state.
_STATE_TO_KIND = {
    SetupLifecycleState.DETECTED: AlertEventKind.SETUP_DETECTED,
    SetupLifecycleState.CONFIRMED: AlertEventKind.SETUP_CONFIRMED,
    SetupLifecycleState.INVALIDATED: AlertEventKind.SETUP_INVALIDATED,
    SetupLifecycleState.EXPIRED: AlertEventKind.SETUP_EXPIRED,
}


def kind_for_state(state: SetupLifecycleState) -> AlertEventKind:
    """Deterministic alert-event kind for an arrived-at lifecycle state."""

    if not isinstance(state, SetupLifecycleState):
        raise TypeError("state must be a SetupLifecycleState.")
    return _STATE_TO_KIND[state]


#: The 19.6 observation statuses that can carry a state-arrival event.
#: (OBSERVED / SUPERSEDED / ABSENT are the statuses that can drive a
#: state change; DATA_UNAVAILABLE and LATE_REJECTED never do.)
_STATE_ARRIVAL_STATUSES = (
    LifecycleObservationStatus.OBSERVED,
    LifecycleObservationStatus.SUPERSEDED,
    LifecycleObservationStatus.ABSENT,
)


def build_alert_id(
    policy_version: str,
    kind: AlertEventKind,
    lifecycle_id: str,
    observation_id: str,
) -> str:
    """
    Deterministic alert id (``"alert-" + sha256[:16]``).

    The id is a function of the alert-policy version + event kind +
    lifecycle id + the 19.6 observation id (the lifecycle-layer
    idempotency basis). It NEVER depends on random UUIDs, wall-clock
    generation, provider response order or object memory addresses.

    Invariants (see the audit document, "Alert ID invariants"):

    * the same logical lifecycle event (same lifecycle + same
      observation + same kind + same policy) ALWAYS yields the same
      alert id;
    * a DIFFERENT lifecycle transition of the SAME setup (different
      observation / different kind) yields a DIFFERENT alert id — one
      setup may legitimately produce several distinct alerts;
    * a policy-rule change (different policy version) yields a
      different alert id for the same event (deterministic policy
      versioning).
    """

    if not isinstance(kind, AlertEventKind):
        raise TypeError("kind must be an AlertEventKind.")
    if not policy_version.strip():
        raise ValueError("policy_version must be a non-empty str.")
    if not lifecycle_id.strip():
        raise ValueError("lifecycle_id must be a non-empty str.")
    if not observation_id.strip():
        raise ValueError("observation_id must be a non-empty str.")
    payload = "|".join(
        (
            "policy", policy_version,
            "kind", kind.value,
            "lifecycle", lifecycle_id,
            "observation", observation_id,
        ),
    )
    return _sha256_prefix(payload, ALERT_ID_PREFIX)


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """
    ONE immutable, deterministic user-facing alert event.

    An alert is a frozen record of what the lifecycle event carried at
    construction time. It is NEVER retroactively modified when later
    lifecycle events occur (point-in-time safety).

    Attributes:

    alert_id
        Deterministic alert id (``"alert-" + sha256[:16]``) — the
        identity-based deduplication key.

    kind
        :class:`AlertEventKind` — the lifecycle state-arrival event.

    severity
        :class:`AlertSeverity` — deterministic, fixed per kind.

    lifecycle_id
        The 19.6 lifecycle instance that produced the event.

    setup_id
        The 19.6 setup identity.

    instrument
        Canonical instrument name.

    lifecycle_state
        The arrived-at :class:`SetupLifecycleState` (DETECTED /
        CONFIRMED / INVALIDATED / EXPIRED).

    previous_lifecycle_state
        The lifecycle state BEFORE the event (``None`` for a lifecycle
        created directly in CONFIRMED, where there was no prior state).

    transition_reason
        The explicit 19.6 transition / creation reason (never
        fabricated by the alert layer).

    setup_type
        The 19.6 identity's analytical setup type (reused 11R
        taxonomy, e.g. ``"BREAKOUT"``).

    direction
        The 19.6 identity's directional context (BULLISH / BEARISH) —
        analytical information, never an execution command.

    primary_timeframe
        The canonical primary (setup) timeframe.

    observation_id
        The 19.6 observation id that carried the event (the
        lifecycle-layer idempotency basis, embedded so the alert
        identity is fully auditable).

    observation_timestamp
        The upstream 19.6 observation instant (explicit, aware) — the
        analytical event timestamp. NEVER wall-clock.

    scan_cycle_id
        The 19.3 scan-cycle identity that produced the lifecycle event.

    observation_status
        The 19.6 :class:`LifecycleObservationStatus` that carried the
        event (OBSERVED / SUPERSEDED / ABSENT).

    policy_version
        The alert-policy version that produced this alert (embedded in
        the alert id).

    quality_status / quality_classification / quality_score
        Reused 19.5 status / classification / score snapshot carried by
        the lifecycle observation (``None`` when unavailable). The
        alert layer NEVER recalculates quality.

    mtf_alignment / mtf_completeness
        Reused 19.4 alignment / completeness carried by the 19.5
        assessment's MTF result (``None`` when unavailable). The alert
        layer NEVER recalculates MTF state.

    setup_classification / confluence_score / has_conflict
        Reused 11Q setup classification / confluence score / conflict
        flag carried by the 19.5 assessment (``None`` when
        unavailable). Never recalculated.

    reason
        Human-readable explanation of the alert (derived from the
        actual inputs).
    """

    alert_id: str
    kind: AlertEventKind
    severity: AlertSeverity
    lifecycle_id: str
    setup_id: str
    instrument: str
    lifecycle_state: SetupLifecycleState
    previous_lifecycle_state: SetupLifecycleState | None
    transition_reason: str
    setup_type: str
    direction: str
    primary_timeframe: str
    observation_id: str
    observation_timestamp: datetime
    scan_cycle_id: str
    observation_status: LifecycleObservationStatus
    policy_version: str
    quality_status: str | None = None
    quality_classification: str | None = None
    quality_score: int | None = None
    mtf_alignment: str | None = None
    mtf_completeness: str | None = None
    setup_classification: str | None = None
    confluence_score: int | None = None
    has_conflict: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _canonical_name(self.instrument))
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if not self.alert_id.strip():
            raise ValueError("alert_id must not be empty.")
        if not self.lifecycle_id.strip():
            raise ValueError("lifecycle_id must not be empty.")
        if not self.setup_id.strip():
            raise ValueError("setup_id must not be empty.")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty.")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty.")
        if not isinstance(self.kind, AlertEventKind):
            raise TypeError("kind must be an AlertEventKind.")
        if not isinstance(self.severity, AlertSeverity):
            raise TypeError("severity must be an AlertSeverity.")
        if not isinstance(self.lifecycle_state, SetupLifecycleState):
            raise TypeError("lifecycle_state must be a SetupLifecycleState.")
        if self.previous_lifecycle_state is not None and not isinstance(
            self.previous_lifecycle_state, SetupLifecycleState,
        ):
            raise TypeError(
                "previous_lifecycle_state must be a SetupLifecycleState or None.",
            )
        if self.previous_lifecycle_state is not None and (
            self.previous_lifecycle_state == self.lifecycle_state
        ):
            raise ValueError(
                "previous and arrived-at lifecycle states must differ.",
            )
        if not isinstance(self.observation_status, LifecycleObservationStatus):
            raise TypeError(
                "observation_status must be a LifecycleObservationStatus.",
            )
        # Severity must match the kind's fixed mapping (never overridden).
        expected_severity = severity_for_kind(self.kind)
        if self.severity is not expected_severity:
            raise ValueError(
                "severity must be the fixed severity for the event kind "
                f"(expected {expected_severity.value!r}).",
            )
        # The kind must match the arrived-at lifecycle state.
        expected_kind = kind_for_state(self.lifecycle_state)
        if self.kind is not expected_kind:
            raise ValueError(
                "kind must match the arrived-at lifecycle state "
                f"(expected {expected_kind.value!r}).",
            )
        # The alert id must be consistent with the identity inputs.
        expected_id = build_alert_id(
            self.policy_version,
            self.kind,
            self.lifecycle_id,
            self.observation_id,
        )
        if self.alert_id != expected_id:
            raise ValueError(
                "alert_id is inconsistent with the identity inputs "
                f"(expected {expected_id!r}).",
            )
        if not self.setup_type.strip():
            raise ValueError("setup_type must not be empty.")
        if not self.direction.strip():
            raise ValueError("direction must not be empty.")
        if not self.primary_timeframe.strip():
            raise ValueError("primary_timeframe must not be empty.")
        if not self.transition_reason.strip():
            raise ValueError("transition_reason must not be empty.")
        if isinstance(self.confluence_score, bool) or (
            self.confluence_score is not None
            and not isinstance(self.confluence_score, int)
        ):
            raise TypeError("confluence_score must be an int or None.")
        if self.confluence_score is not None and not 0 <= self.confluence_score <= 5:
            raise ValueError("confluence_score must lie within [0, 5].")
        if isinstance(self.quality_score, bool) or (
            self.quality_score is not None and not isinstance(self.quality_score, int)
        ):
            raise TypeError("quality_score must be an int or None.")
        if self.quality_score is not None and not 0 <= self.quality_score <= 100:
            raise ValueError("quality_score must lie within [0, 100].")

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    @property
    def is_terminal_event(self) -> bool:
        """True for a terminal lifecycle state-arrival event."""
        return self.lifecycle_state.is_terminal

    @property
    def has_quality(self) -> bool:
        """True when the alert carries a 19.5 quality snapshot."""
        return self.quality_score is not None


@dataclass(frozen=True, slots=True)
class AlertDeliveryResult:
    """
    Deterministic delivery outcome for ONE alert event.

    Attributes:

    alert_id
        The alert event id this delivery refers to.

    status
        :class:`AlertDeliveryStatus` (DELIVERED / FAILED / SKIPPED /
        SUPPRESSED).

    delivery_timestamp
        The optional delivery instant (explicit, aware). Kept
        SEPARATE from the analytical event timestamp and NEVER used for
        analytical identity. ``None`` when no delivery was performed
        (SKIPPED / SUPPRESSED).

    channel
        The delivery channel name (``"console"`` for the default sink,
        ``""`` when no channel was involved).

    reason
        Human-readable delivery outcome detail.
    """

    alert_id: str
    status: AlertDeliveryStatus
    delivery_timestamp: datetime | None = None
    channel: str = ""
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.alert_id.strip():
            raise ValueError("alert_id must not be empty.")
        if not isinstance(self.status, AlertDeliveryStatus):
            raise TypeError("status must be an AlertDeliveryStatus.")
        if self.delivery_timestamp is not None:
            _require_aware(self.delivery_timestamp, "delivery_timestamp")
        # A DELIVERED result must carry a delivery timestamp + channel.
        if self.status is AlertDeliveryStatus.DELIVERED:
            if self.delivery_timestamp is None:
                raise ValueError(
                    "a DELIVERED result must carry a delivery timestamp.",
                )
            if not self.channel.strip():
                raise ValueError(
                    "a DELIVERED result must carry a channel name.",
                )
        # A FAILED result must carry a reason.
        if self.status is AlertDeliveryStatus.FAILED:
            if not self.reason.strip():
                raise ValueError("a FAILED result must carry a reason.")


@dataclass(frozen=True, slots=True)
class SuppressedAlert:
    """
    ONE deterministic audit record of a suppressed alert event.

    A suppressed alert is a lifecycle event that was examined but NOT
    emitted — either because its deterministic alert id was already
    recorded (identity-based deduplication), because the alert policy /
    configuration made it ineligible, or because a per-cycle cap was
    reached. Suppression is EXPLICIT and AUDITABLE — never a silent
    drop.

    Attributes:

    alert_id
        The deterministic alert id that WOULD have been emitted.

    kind
        :class:`AlertEventKind` of the suppressed event.

    setup_id / lifecycle_id / instrument / observation_id
        Traceability back to the 19.6 lifecycle event.

    observation_timestamp
        The upstream 19.6 observation instant (explicit, aware).

    policy_version
        The alert-policy version under which the event was suppressed.

    reason
        The explicit suppression reason (``"deduplicated"`` /
        ``"alerting disabled"`` / ``"kind not eligible"`` /
        ``"quality gate not met"`` / ``"maximum alerts per cycle"``).
    """

    alert_id: str
    kind: AlertEventKind
    setup_id: str
    lifecycle_id: str
    instrument: str
    observation_id: str
    observation_timestamp: datetime
    policy_version: str
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _canonical_name(self.instrument))
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if not self.alert_id.strip():
            raise ValueError("alert_id must not be empty.")
        if not self.setup_id.strip():
            raise ValueError("setup_id must not be empty.")
        if not self.lifecycle_id.strip():
            raise ValueError("lifecycle_id must not be empty.")
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty.")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty.")
        if not isinstance(self.kind, AlertEventKind):
            raise TypeError("kind must be an AlertEventKind.")
        if not self.reason.strip():
            raise ValueError("reason must not be empty.")


@dataclass(frozen=True, slots=True)
class AlertCounts:
    """
    Deterministic aggregate counts over an alert-processing cycle.

    ``eligible`` counts the lifecycle events examined for alerting
    (state-arrival events); ``emitted`` counts the alerts that passed
    every gate and were attempted for delivery; ``delivered`` counts
    the successfully delivered alerts; ``failed`` / ``skipped`` are the
    delivery outcomes; ``suppressed`` counts the events that were NOT
    emitted (identity-dedup / policy-ineligible / cap). ``by_kind``
    tallies DELIVERED alerts per event kind (deterministic order).

    Invariants: ``emitted == delivered + failed + skipped`` and
    ``eligible == emitted + suppressed``.
    """

    eligible: int = 0
    emitted: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0
    suppressed: int = 0
    by_kind: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @property
    def processed(self) -> int:
        """Number of lifecycle events examined this cycle."""
        return self.eligible


@dataclass(frozen=True, slots=True)
class AlertCycleResult:
    """
    ONE deterministic universe-wide alert-processing cycle.

    Consumes the FROZEN 19.6 :class:`SetupLifecycleCycleResult` (which
    itself consumes the FROZEN 19.5 universe result + the FROZEN 19.3
    scan-cycle identity) and produces one
    :class:`AlertDeliveryResult` per alert event.

    Attributes:

    cycle_id
        Deterministic alert-processing-cycle id
        (``"alert-cycle-" + sha256[:16]``) over the lifecycle cycle id
        + policy version + config snapshot. Reprocessing the same
        lifecycle cycle with identical alert-store state yields the
        SAME cycle id.

    lifecycle_cycle_id
        The 19.6 lifecycle-processing-cycle id consumed.

    scan_cycle_id
        The 19.3 scan-cycle identity consumed.

    reference_now
        The deterministic observation instant (from the lifecycle
        cycle).

    instruments
        Processed instruments (canonical, sorted).

    alerts
        The EMITTED :class:`AlertEvent` objects this cycle (deterministic
        order — see the audit document "Multiple-alert ordering").

    deliveries
        Per-alert :class:`AlertDeliveryResult` (one per emitted alert,
        in the same deterministic order).

    suppressed
        The :class:`SuppressedAlert` audit records for events that were
        NOT emitted this cycle (deterministic order).

    counts
        :class:`AlertCounts`.

    policy_version
        The alert-policy version used.
    """

    cycle_id: str
    lifecycle_cycle_id: str
    scan_cycle_id: str
    reference_now: datetime
    instruments: tuple[str, ...] = field(default_factory=tuple)
    alerts: tuple[AlertEvent, ...] = field(default_factory=tuple)
    deliveries: tuple[AlertDeliveryResult, ...] = field(default_factory=tuple)
    suppressed: tuple[SuppressedAlert, ...] = field(default_factory=tuple)
    counts: AlertCounts = field(default_factory=AlertCounts)
    policy_version: str = ""

    def __post_init__(self) -> None:
        _require_aware(self.reference_now, "reference_now")
        if not self.cycle_id.strip():
            raise ValueError("cycle_id must not be empty.")
        if not self.lifecycle_cycle_id.strip():
            raise ValueError("lifecycle_cycle_id must not be empty.")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty.")
        canon = tuple(sorted(set(_canonical_name(n) for n in self.instruments)))
        object.__setattr__(self, "instruments", canon)
        # deliveries must reference exactly the emitted alerts, in order.
        if self.deliveries or self.alerts:
            if len(self.deliveries) != len(self.alerts):
                raise ValueError(
                    "deliveries must have one entry per emitted alert.",
                )
            for delivery, alert in zip(self.deliveries, self.alerts):
                if not isinstance(delivery, AlertDeliveryResult):
                    raise TypeError(
                        "deliveries must be AlertDeliveryResult objects.",
                    )
                if not isinstance(alert, AlertEvent):
                    raise TypeError("alerts must be AlertEvent objects.")
                if delivery.alert_id != alert.alert_id:
                    raise ValueError(
                        "delivery alert_id must match the emitted alert.",
                    )
        for suppressed in self.suppressed:
            if not isinstance(suppressed, SuppressedAlert):
                raise TypeError(
                    "suppressed must be SuppressedAlert objects.",
                )

    def alert_for(self, alert_id: str) -> AlertEvent | None:
        """Primary alert lookup (``None`` when not present)."""
        for alert in self.alerts:
            if alert.alert_id == alert_id:
                return alert
        return None

    @property
    def is_empty(self) -> bool:
        return not self.alerts


__all__ = [
    "ALERT_CYCLE_ID_PREFIX",
    "ALERT_ID_PREFIX",
    "ALERT_MODEL_VERSION",
    "AlertCounts",
    "AlertCycleResult",
    "AlertDeliveryResult",
    "AlertDeliveryStatus",
    "AlertEvent",
    "AlertEventKind",
    "AlertSeverity",
    "SuppressedAlert",
    "build_alert_id",
    "kind_for_state",
    "severity_for_kind",
]