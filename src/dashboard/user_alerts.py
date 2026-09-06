"""
User alerts engine + store + delivery channels (Checkpoint 19.7).

This module implements the USER ALERTS layer on top of the FROZEN 19.6
setup-lifecycle boundary:

    Scan Cycle (19.3 identity)
        |
    19.5 Setup Quality
        |
    19.6 Setup Lifecycle -> Lifecycle Events (transitions)
        |
    19.7 USER ALERTS (this module) -> Alert Events
        |
    Alert Delivery Channel -> User

The engine is DOWNSTREAM of 19.6. It CONSUMES the deterministic
LIFECYCLE EVENTS that 19.6 exposes — the per-symbol
:class:`SetupLifecycleObservationResult.transition` records (the
authoritative 19.6 :class:`SetupLifecycleTransition` objects, plus the
creation events on :class:`SetupLifecycleObservationResult.created`)
— and converts the MEANINGFUL ones into deterministic, user-facing
:class:`AlertEvent` objects. The alert layer NEVER reconstructs a
lifecycle event from candles / assessments / history: it consumes the
event that 19.6 already produced.

LIFECYCLE EVENT vs ALERT EVENT (the mandatory separation): a lifecycle
transition means "something happened to this setup"; an alert means
"this event is important enough to tell the user". Not every lifecycle
observation becomes an alert — and in fact most do NOT (repeated
unchanged observations produce NO lifecycle transition and therefore NO
alert event at all).

ALERT ELIGIBILITY POLICY (explicit, deterministic — see the audit
document):

1. The lifecycle events examined are the transactions that 19.6
   produced per symbol per cycle: the DETECTED/CONFIRMED/INVALIDATED/
   EXPIRED state-arrival events carried by
   :class:`SetupLifecycleObservationResult`.
2. A lifecycle TRANSITION (a real state change) IS an event; a CREATED
   lifecycle is an event (detection or first-observation confirmation);
   repeated unchanged observations are NOT events.
3. The alert KIND is a deterministic function of the arrived-at
   lifecycle state (DETECTED -> SETUP_DETECTED, CONFIRMED ->
   SETUP_CONFIRMED, INVALIDATED -> SETUP_INVALIDATED, EXPIRED ->
   SETUP_EXPIRED).
4. Kind eligibility is governed by the config ``eligible_kinds``
   (default: confirmation / invalidation / expiration ONLY — a
   detection is informational and would be spam on the Top 200).
5. ``enabled=False`` suppresses every eligible event explicitly.
6. An optional quality-classification gate filters on the CARRIED 19.5
   classification (never recalculates quality).
7. Deterministic IDENTITY-BASED DEDUPLICATION: the same logical event
   (same alert id) recorded before is suppressed explicitly.

NOISE CONTROL (a major 19.7 requirement): the same setup appearing in
many scan cycles CANNOT produce repeated alerts because unchanged
lifecycles produce NO new lifecycle transitions in 19.6, and every
event has a distinct, stable alert id. The alert set is therefore a
deterministic function of the lifecycle event stream — not of scan
frequency, clock time, provider order or universe order.

DELIVERY ABSTRACTION (mandatory separation): ALERT GENERATION is fully
separate from ALERT DELIVERY.

* :class:`AlertDeliveryChannel` is the channel protocol — deliberately
  trivial (``deliver(alert) -> str``) so any future external
  integration (console / file / http / Slack / Discord / email ...)
  can be layered WITHOUT contaminating the core logic.
* :class:`ConsoleAlertSink` is the DEFAULT deterministic offline sink:
  records delivered alerts internally and returns the deterministic
  text formatter output. No network, no credentials, no external
  service.
* Delivery FAILURE cannot corrupt lifecycle state: a raising channel is
  caught per-alert and recorded as ``FAILED`` (never raising into the
  alert engine, never touching the lifecycle store).

POINT-IN-TIME SAFETY: the engine is a pure function of the consumed
lifecycle cycle result + the delivery channels + the alert store; it
never reads candles / providers and never calls wall-clock. A future
lifecycle event can never alter an earlier alert (frozen records).

RELIABILITY / FORWARD-TESTING BOUNDARIES: this module implements NO
durable persistence / crash recovery / retries / watchdogs (19.8) and
NO forward-testing (19.9). The in-memory :class:`AlertStore` is a
deterministic repository that keeps the alert + delivery history for a
run; 19.8 will add durability around it.

DESIGN RULES (match the rest of the project):

* The engine is stateless except for the injected alert store.
* All timestamps are explicit, timezone-aware, caller-supplied.
* Deterministic ordering everywhere (no unordered iteration).
* No wall-clock dependence.
* No broker / execution / paper-trading / historical-research imports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Sequence, runtime_checkable

from engine.config.user_alerts_config import UserAlertConfig, alert_policy_version
from engine.models.setup_lifecycle import (
    SetupLifecycleCycleResult,
    SetupLifecycleObservationResult,
    SetupLifecycleState,
    SetupLifecycleTransition,
)
from engine.models.setup_quality import SetupQualityClassification
from engine.models.user_alerts import (
    ALERT_CYCLE_ID_PREFIX,
    ALERT_ID_PREFIX,
    AlertCounts,
    AlertCycleResult,
    AlertDeliveryResult,
    AlertDeliveryStatus,
    AlertEvent,
    AlertEventKind,
    AlertSeverity,
    SuppressedAlert,
    build_alert_id,
    kind_for_state,
    severity_for_kind,
)


def _canonical_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"instrument name must be a str, got {type(name).__name__}")
    canonical = name.strip().upper()
    if not canonical:
        raise ValueError("instrument name cannot be empty")
    return canonical


def _require_aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )
    return value


def _sha256_prefix(payload: str, prefix: str) -> str:
    import hashlib

    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:16]}"


def alert_cycle_id(
    lifecycle_cycle_id: str,
    policy_version: str,
    config: UserAlertConfig,
) -> str:
    """Deterministic alert-processing-cycle identity.

    Any alert-config change is an alert-policy change; the cycle id
    embeds the exact policy-set version so reprocessing the same
    lifecycle cycle under two different policies yields DIFFERENT
    alert cycle ids.
    """

    if not lifecycle_cycle_id.strip():
        raise ValueError("lifecycle_cycle_id must not be empty.")
    if not policy_version.strip():
        raise ValueError("policy_version must not be empty.")
    payload = "|".join(
        (
            "lifecycle_cycle", lifecycle_cycle_id,
            "policy", policy_version,
            "config_hash", _config_hash(config),
        ),
    )
    return _sha256_prefix(payload, ALERT_CYCLE_ID_PREFIX)


def _config_hash(config: UserAlertConfig) -> str:
    payload = ";".join(f"{k}={v}" for k, v in config.snapshot())
    return _sha256_prefix(payload, "cfg-")


# ------------------------------------------------------------------
# DELIVERY CHANNEL ABSTRACTION
# ------------------------------------------------------------------


@runtime_checkable
class AlertDeliveryChannel(Protocol):
    """Delivery-channel protocol (alert generation is separated from
    alert delivery).

    An implementation MUST be deterministic for the same alert event
    and MUST NOT long-block; it returns the delivered content (str).
    The channel MUST NOT mutate the alert (it is frozen) and MUST NOT
    raise into the alert engine — the engine catches exceptions
    per-alert and records them as ``AlertDeliveryStatus.FAILED``.
    """

    name: str

    def deliver(self, alert: AlertEvent) -> str: ...


class ConsoleAlertSink:
    """Default deterministic, OFFLINE delivery channel.

    The console sink records every delivered alert in memory
    (``delivered_events`` / ``delivered_text``) and returns the
    deterministic human-readable text formatter output. It represents
    "the user sees this message" and is the smallest safe default
    delivery mechanism: no network, no credentials, fully testable.
    """

    name = "console"

    def __init__(self) -> None:
        self._delivered: list[AlertEvent] = []
        self._text: list[str] = []

    def deliver(self, alert: AlertEvent) -> str:
        from engine.reporting.user_alerts import AlertFormatter

        text = AlertFormatter().format_alert(alert)
        self._delivered.append(alert)
        self._text.append(text)
        return text

    @property
    def delivered_events(self) -> tuple[AlertEvent, ...]:
        return tuple(self._delivered)

    @property
    def delivered_text(self) -> tuple[str, ...]:
        return tuple(self._text)

    @property
    def count(self) -> int:
        return len(self._delivered)


class FailingAlertSink:
    """A deterministic delivery channel that ALWAYS raises.

    Used to prove (a) the alert engine survives a delivery failure
    without corrupting lifecycle state and (b) a delivery failure is
    recorded as ``AlertDeliveryStatus.FAILED`` (never a silent success,
    never an engine crash). Test-only surface; no network.
    """

    name = "failing"

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error or RuntimeError("simulated delivery failure")

    def deliver(self, alert: AlertEvent) -> str:
        raise self._error


# ------------------------------------------------------------------
# ALERT STORE (in-memory, deterministic)
# ------------------------------------------------------------------


class AlertStore:
    """
    In-memory, deterministic alert history repository.

    The store keeps the EMITTED alerts + their delivery results +
    the suppressed-audit records, indexed for deduplication. It is
    deliberately NOT a database: 19.8 owns durable persistence /
    recovery.

    Deterministic ordering: all enumerations are sorted by ``alert_id``
    / ``observation_timestamp``. No unordered iteration.
    """

    def __init__(self) -> None:
        self._alerts: dict[str, AlertEvent] = {}
        self._deliveries: dict[str, AlertDeliveryResult] = {}
        self._suppressed: list[SuppressedAlert] = []

    # ------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------

    def record_alert(self, alert: AlertEvent) -> None:
        """Record an emitted alert (replace-by-id; the engine never
        emits a duplicate — the id is the dedup key)."""
        if not isinstance(alert, AlertEvent):
            raise TypeError("alert must be an AlertEvent.")
        self._alerts[alert.alert_id] = alert

    def record_delivery(self, delivery: AlertDeliveryResult) -> None:
        if not isinstance(delivery, AlertDeliveryResult):
            raise TypeError("delivery must be an AlertDeliveryResult.")
        self._deliveries[delivery.alert_id] = delivery

    def record_suppressed(self, suppressed: SuppressedAlert) -> None:
        if not isinstance(suppressed, SuppressedAlert):
            raise TypeError("suppressed must be a SuppressedAlert.")
        self._suppressed.append(suppressed)

    def reset(self) -> None:
        self._alerts.clear()
        self._deliveries.clear()
        self._suppressed.clear()

    # ------------------------------------------------------------
    # READ (deterministic)
    # ------------------------------------------------------------

    def has(self, alert_id: str) -> bool:
        return alert_id in self._alerts

    def load(self, alert_id: str) -> AlertEvent | None:
        return self._alerts.get(alert_id)

    def delivery_for(self, alert_id: str) -> AlertDeliveryResult | None:
        return self._deliveries.get(alert_id)

    def list_alerts(self) -> tuple[AlertEvent, ...]:
        return tuple(self._alerts[k] for k in sorted(self._alerts))

    def list_deliveries(self) -> tuple[AlertDeliveryResult, ...]:
        return tuple(self._deliveries[k] for k in sorted(self._deliveries))

    def list_suppressed(self) -> tuple[SuppressedAlert, ...]:
        return tuple(
            sorted(
                self._suppressed,
                key=lambda s: (s.observation_timestamp.isoformat(), s.alert_id),
            ),
        )

    def alerts_for_setup(self, setup_id: str) -> tuple[AlertEvent, ...]:
        return tuple(
            a for a in self.list_alerts()
            if a.setup_id == setup_id
        )

    def alerts_for_instrument(self, instrument: str) -> tuple[AlertEvent, ...]:
        canon = _canonical_name(instrument)
        return tuple(
            a for a in self.list_alerts()
            if a.instrument == canon
        )

    @property
    def total_count(self) -> int:
        return len(self._alerts)

    @property
    def suppressed_count(self) -> int:
        return len(self._suppressed)


# ------------------------------------------------------------------
# ALERT ENGINE
# ------------------------------------------------------------------


class AlertEngine:
    """
    Deterministic user alerts engine (Checkpoint 19.7).

    Public API:

        process(cycle, *, at=None)
            -> AlertCycleResult
            ONE 19.6 lifecycle-processing cycle converted into alert
            events + deliveries + suppression audits.

    The engine is stateless EXCEPT for the injected alert store and
    delivery channels. Identical inputs + identical store/channel state
    produce identical outputs. No wall-clock dependence: the delivery
    timestamp is injected (``at``) and the analytical timestamps always
    come from the consumed lifecycle events.

    Delivery is separated from generation: zero or more channels may be
    passed; with NO channels every emitted alert is recorded as
    ``AlertDeliveryStatus.SKIPPED`` (explicit, auditable), never
    silently dropped.
    """

    def __init__(
        self,
        config: UserAlertConfig | None = None,
        store: AlertStore | None = None,
        channels: Sequence[AlertDeliveryChannel] | None = None,
    ) -> None:
        self.config = config or UserAlertConfig()
        self.store = store or AlertStore()
        self.channels = tuple(channels or ())

    @classmethod
    def build(
        cls,
        channels: Sequence[AlertDeliveryChannel] | None = None,
        config: UserAlertConfig | None = None,
    ) -> "AlertEngine":
        """Fresh engine + empty in-memory store + channels."""
        return cls(
            config or UserAlertConfig(),
            AlertStore(),
            channels,
        )

    # ============================================================
    # PUBLIC API
    # ============================================================

    def process(
        self,
        cycle: SetupLifecycleCycleResult,
        *,
        at: datetime | None = None,
    ) -> AlertCycleResult:
        """
        Convert ONE 19.6 lifecycle-processing cycle into alert events.

        ``cycle`` is the FROZEN 19.6 :class:`SetupLifecycleCycleResult`.
        ``at`` is the optional DELIVERY instant (explicit, aware; NEVER
        used for analytical identity — the analytical timestamps always
        come from the consumed lifecycle events).

        Deterministic, failure-isolated, idempotent-per-event.
        Returns the :class:`AlertCycleResult` for this cycle.
        """

        if not isinstance(cycle, SetupLifecycleCycleResult):
            raise TypeError("cycle must be a SetupLifecycleCycleResult.")
        policy_version = alert_policy_version(self.config)
        if at is not None:
            at = _require_aware(at, "at")

        # ---- Deterministic event enumeration (the lifecycle events
        #      that 19.6 produced this cycle). -------------------------
        events = self._enum_events(cycle)

        # ---- Per-event processing -----------------------------------
        alerts: list[AlertEvent] = []
        deliveries: list[AlertDeliveryResult] = []
        suppressed: list[SuppressedAlert] = []
        emitted = 0
        for event in events:
            kind, lifecycle_id, setup_id, instrument, observation, previous, reason = event
            alert_id = build_alert_id(
                policy_version, kind, lifecycle_id, observation.observation_id,
            )
            suppression = self._suppression_reason(kind, observation)
            is_dup = self.store.has(alert_id)
            if suppression is not None or is_dup:
                supp_reason = (
                    "deduplicated (this lifecycle event already produced "
                    "an alert)"
                    if is_dup
                    else suppression
                )
                suppressed_entry = SuppressedAlert(
                    alert_id=alert_id,
                    kind=kind,
                    setup_id=setup_id,
                    lifecycle_id=lifecycle_id,
                    instrument=instrument,
                    observation_id=observation.observation_id,
                    observation_timestamp=observation.observation_timestamp,
                    policy_version=policy_version,
                    reason=supp_reason,
                )
                suppressed.append(suppressed_entry)
                self.store.record_suppressed(suppressed_entry)
                continue

            if (
                self.config.max_per_cycle is not None
                and emitted >= self.config.max_per_cycle
            ):
                suppressed_entry = SuppressedAlert(
                    alert_id=alert_id,
                    kind=kind,
                    setup_id=setup_id,
                    lifecycle_id=lifecycle_id,
                    instrument=instrument,
                    observation_id=observation.observation_id,
                    observation_timestamp=observation.observation_timestamp,
                    policy_version=policy_version,
                    reason=(
                        f"maximum alerts per cycle reached "
                        f"({self.config.max_per_cycle})."
                    ),
                )
                suppressed.append(suppressed_entry)
                self.store.record_suppressed(suppressed_entry)
                continue

            alert = self._build_alert(event, policy_version, alert_id)
            delivery = self._deliver(alert, at=at)
            alerts.append(alert)
            deliveries.append(delivery)
            self.store.record_alert(alert)
            self.store.record_delivery(delivery)
            emitted += 1

        counts = self._counts(len(events), emitted, deliveries, len(suppressed))
        instruments = tuple(sorted(set(cycle.instruments)))
        return AlertCycleResult(
            cycle_id=alert_cycle_id(cycle.cycle_id, policy_version, self.config),
            lifecycle_cycle_id=cycle.cycle_id,
            scan_cycle_id=cycle.scan_cycle_id,
            reference_now=cycle.reference_now,
            instruments=instruments,
            alerts=tuple(alerts),
            deliveries=tuple(deliveries),
            suppressed=tuple(suppressed),
            counts=counts,
            policy_version=policy_version,
        )

    # ============================================================
    # INTERNALS — EVENT ENUMERATION (consumes 19.6 events)
    # ============================================================

    def _enum_events(self, cycle: SetupLifecycleCycleResult):
        """
        Enumerate the deterministic, deduplicated lifecycle EVENTS in
        one 19.6 cycle.

        An event exists for a per-symbol result when the result carries
        a state-arrival event:

        * ``result.transition is not None`` — a real 19.6 state
          transition (the authoritative event), OR
        * ``result.created`` — a new lifecycle instance (a detection,
          or a first-observation confirmation that 19.6 emits as the
          creation transition).

        The alert kind is a deterministic function of the EVENT's
        arrived-at lifecycle state. Results that carry NO state change
        (repeated observations, data-unavailable, late-rejected,
        duplicates) produce NO event and therefore NO alert.

        Returns a list of ``(kind, lifecycle_id, setup_id, instrument,
        observation, previous_state, transition_reason)`` tuples in
        deterministic order.
        """

        events: list[tuple] = []
        seen: set[tuple[str, str]] = set()
        ordered = sorted(
            cycle.results,
            key=lambda r: (
                r.instrument,
                r.observation_timestamp.isoformat(),
                r.lifecycle_id or "",
            ),
        )
        for result in ordered:
            if not isinstance(result, SetupLifecycleObservationResult):
                continue
            event = self._event_for_result(result)
            if event is None:
                continue
            kind, lifecycle_id, setup_id, instrument, observation, previous, reason = event
            key = (lifecycle_id, observation.observation_id)
            if key in seen:
                continue
            seen.add(key)
            events.append(
                (
                    kind,
                    lifecycle_id,
                    setup_id,
                    instrument,
                    observation,
                    previous,
                    reason,
                ),
            )
        return events

    def _event_for_result(
        self, result: SetupLifecycleObservationResult,
    ):
        """
        The state-arrival lifecycle event of ONE per-symbol lifecycle
        processing result, or ``None`` when the result carries none.

        The event is consumed VERBATIM from the 19.6 result:

        * the authoritative ``result.transition`` (a real state change
          19.6 recorded, with its exact ``from_state`` and ``reason``),
          OR
        * a ``result.created`` lifecycle (19.6 created the instance —
          the event is detection or first-observation confirmation;
          the previous state is ``None`` because the lifecycle began
          here).

        The event KIND is a deterministic function of the arrived-at
        state. The observation is the lifecycle observation that
        carried the event (and hence the only analytical payload the
        alert layer needs).
        """

        observation = result.observation
        if observation is None or observation.at_state is None:
            return None
        arrived = observation.at_state
        kind = kind_for_state(arrived)
        if result.transition is not None:
            transition: SetupLifecycleTransition = result.transition
            if transition.to_state != arrived:
                # Defensive: the transition's arrived state must match
                # the observation's arrived state (19.6 invariant).
                return None
            return (
                kind,
                result.lifecycle_id,
                result.setup_id,
                result.instrument,
                observation,
                transition.from_state,
                transition.reason,
            )
        if result.created:
            # A lifecycle creation is itself the state-arrival event
            # (detection, or first-observation confirmation). The
            # previous state is None (the lifecycle began here).
            return (
                kind,
                result.lifecycle_id,
                result.setup_id,
                result.instrument,
                observation,
                None,
                observation.reason,
            )
        return None

    # ============================================================
    # INTERNALS — ALERT CONSTRUCTION
    # ============================================================

    def _build_alert(self, event, policy_version: str, alert_id: str) -> AlertEvent:
        """Construct the user-facing alert from the lifecycle event.

        The alert copies ONLY information carried by the lifecycle
        event (identity, state, observation payload). It never
        recalculates quality / MTF / confluence and never adds fields
        the upstream models did not already produce.
        """

        kind, lifecycle_id, setup_id, instrument, observation, previous, reason = event
        observation_status = observation.observation_status
        assessment = getattr(observation, "assessment", None)

        mtf_alignment: str | None = None
        mtf_completeness: str | None = None
        if assessment is not None:
            try:
                mtf_alignment = assessment.mtf.alignment.value
                mtf_completeness = assessment.mtf.completeness.value
            except AttributeError:
                pass

        setup_classification: str | None = None
        confluence_score: int | None = None
        has_conflict = False
        if assessment is not None:
            try:
                setup_classification = (
                    assessment.setup_classification.value
                    if assessment.setup_classification is not None
                    else None
                )
            except AttributeError:
                pass
            try:
                if assessment.confluence_score is not None:
                    confluence_score = int(assessment.confluence_score)
            except AttributeError:
                pass
            try:
                has_conflict = bool(assessment.has_conflict)
            except AttributeError:
                pass

        arrived = observation.at_state
        alert = AlertEvent(
            alert_id=alert_id,
            kind=kind,
            severity=severity_for_kind(kind),
            lifecycle_id=lifecycle_id,
            setup_id=setup_id,
            instrument=instrument,
            lifecycle_state=arrived,
            previous_lifecycle_state=previous,
            transition_reason=(
                reason
                or f"lifecycle arrived at state {arrived.value}."
            ),
            setup_type=observation.setup_type or "SETUP_CANDIDATE",
            direction=(
                observation.direction.value
                if observation.direction is not None
                else "UNKNOWN"
            ),
            primary_timeframe=self._timeframe_for(observation, assessment),
            observation_id=observation.observation_id,
            observation_timestamp=observation.observation_timestamp,
            scan_cycle_id=observation.scan_cycle_id,
            observation_status=observation_status,
            policy_version=policy_version,
            quality_status=observation.quality_status,
            quality_classification=observation.quality_classification,
            quality_score=observation.quality_score,
            mtf_alignment=mtf_alignment,
            mtf_completeness=mtf_completeness,
            setup_classification=setup_classification,
            confluence_score=confluence_score,
            has_conflict=has_conflict,
            reason=self._alert_reason(kind, reason),
        )
        return alert

    def _timeframe_for(self, observation, assessment) -> str:
        """The canonical primary timeframe carried by the lifecycle
        observation's assessment, or the project convention
        (lowest-duration setup frame "15m") when unavailable."""
        if assessment is not None:
            try:
                timeframe = assessment.primary_timeframe
                if isinstance(timeframe, str) and timeframe.strip():
                    return timeframe
            except AttributeError:
                pass
        return "15m"

    def _alert_reason(self, kind: AlertEventKind, transition_reason: str) -> str:
        """Human-readable alert-level explanation (analytical only)."""
        verb = {
            AlertEventKind.SETUP_DETECTED: "detected",
            AlertEventKind.SETUP_CONFIRMED: "confirmed",
            AlertEventKind.SETUP_INVALIDATED: "invalidated",
            AlertEventKind.SETUP_EXPIRED: "expired",
        }[kind]
        return f"setup {verb} — {transition_reason}"

    # ============================================================
    # INTERNALS — SUPPRESSION / DELIVERY / COUNTS
    # ============================================================

    def _suppression_reason(self, kind: AlertEventKind, observation) -> str | None:
        """Deterministic suppression decision for an eligible event.
        Returns a suppression reason (not emitted) or ``None`` (emit)."""

        if not self.config.enabled:
            return "alerting disabled by configuration."
        if not self.config.is_eligible(kind.value):
            return (
                f"alert kind {kind.value} is not in the eligible set."
            )
        if self.config.min_quality_classification is not None:
            carried = observation.quality_classification
            if carried is None or not self._classification_at_least(
                carried, self.config.min_quality_classification,
            ):
                return (
                    f"carried quality classification {carried!r} is below "
                    f"the configured minimum "
                    f"{self.config.min_quality_classification!r}."
                )
        return None

    @staticmethod
    def _classification_at_least(carried: str, minimum: str) -> bool:
        """Deterministic quality-gate comparison (never recalculates
        quality)."""
        try:
            carried_value = SetupQualityClassification(carried)
            minimum_value = SetupQualityClassification(minimum)
        except ValueError:
            return False
        return carried_value.rank_value >= minimum_value.rank_value

    def _deliver(self, alert: AlertEvent, *, at: datetime | None) -> AlertDeliveryResult:
        """Deliver one alert through the configured channels.

        Deterministic: with zero channels the alert is SKIPPED
        (explicit, auditable). With one or more channels, delivery is
        attempted channel-by-channel in channel order; the FIRST
        successful delivery marks the alert DELIVERED; a raising
        channel is recorded as FAILED (never raising into the engine,
        never touching lifecycle state).
        """

        if not self.channels:
            return AlertDeliveryResult(
                alert_id=alert.alert_id,
                status=AlertDeliveryStatus.SKIPPED,
                delivery_timestamp=at,
                channel="",
                reason=(
                    "no delivery channel configured — alert generated "
                    "but not delivered (explicit, auditable)."
                ),
            )
        # The delivery timestamp defaults deterministically to the
        # alert's analytical observation instant when no explicit
        # delivery instant was injected (NEVER wall-clock; the delivery
        # timestamp is never used for analytical identity).
        delivery_at = at if at is not None else alert.observation_timestamp
        last_error: Exception | None = None
        for channel in self.channels:
            try:
                channel.deliver(alert)
            except Exception as exc:  # defensive: delivery never raises
                last_error = exc
                continue
            return AlertDeliveryResult(
                alert_id=alert.alert_id,
                status=AlertDeliveryStatus.DELIVERED,
                delivery_timestamp=delivery_at,
                channel=channel.name,
                reason=(
                    "alert delivered through channel "
                    f"{channel.name!r}."
                ),
            )
        return AlertDeliveryResult(
            alert_id=alert.alert_id,
            status=AlertDeliveryStatus.FAILED,
            delivery_timestamp=delivery_at,
            channel="",
            reason=(
                "all delivery channels failed "
                f"({type(last_error).__name__}: {last_error})."
            ),
        )

    def _counts(
        self,
        eligible: int,
        emitted: int,
        deliveries: Sequence[AlertDeliveryResult],
        suppressed_count: int,
    ) -> AlertCounts:
        delivered = sum(
            1 for d in deliveries
            if d.status is AlertDeliveryStatus.DELIVERED
        )
        failed = sum(
            1 for d in deliveries
            if d.status is AlertDeliveryStatus.FAILED
        )
        skipped = sum(
            1 for d in deliveries
            if d.status is AlertDeliveryStatus.SKIPPED
        )
        by_kind: dict[str, int] = {}
        for d in deliveries:
            if d.status is AlertDeliveryStatus.DELIVERED:
                alert = self.store.load(d.alert_id)
                if alert is not None:
                    kind = alert.kind.value
                    by_kind[kind] = by_kind.get(kind, 0) + 1
        return AlertCounts(
            eligible=eligible,
            emitted=emitted,
            delivered=delivered,
            failed=failed,
            skipped=skipped,
            suppressed=suppressed_count,
            by_kind=tuple(sorted(by_kind.items())),
        )


#: Id prefix re-export for the CLI / JSON surface.
ALERT_ID_PREFIXES = (ALERT_ID_PREFIX, ALERT_CYCLE_ID_PREFIX)

__all__ = [
    "ALERT_ID_PREFIXES",
    "AlertDeliveryChannel",
    "AlertEngine",
    "AlertStore",
    "ConsoleAlertSink",
    "FailingAlertSink",
    "alert_cycle_id",
]