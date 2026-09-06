"""
Alert delivery outbox (Checkpoint 19.8).

A small, deterministic DELIVERY OUTBOX for reliable alert delivery. The
outbox is the ONLY new delivery-reliability mechanism 19.8 introduces —
it is justified because a delivery failure must be retried with the
SAME alert identity and the SAME analytical payload, and the retry must
never become a NEW alert (19.7 deduplication must not be defeated).

The outbox pattern keeps the concerns CLEARLY SEPARATED:

    Lifecycle Event (19.6)
        -> AlertEvent (19.7 generation)
        -> Outbox (19.8 this module)
        -> Delivery Channel (19.7 abstraction)
        -> Delivery Status (19.7)

The outbox NEVER alters lifecycle semantics and NEVER alters the alert
identity. A delivery failure is recorded on the outbox entry and the
entry is retried according to the injected retry policy; the retried
alert retains its ORIGINAL ``alert_id`` (and its original analytical
payload — the alert object is retained BY REFERENCE, never rebuilt).

Design:

* :class:`PendingOutboxEntry` — one immutable outbox entry: the alert
  (by reference), the deterministic outbox id, the delivery attempts so
  far, the next-retry delay, and the last failure detail. The entry id
  is derived from the alert id + policy version, so re-enqueueing the
  same alert yields the SAME entry (idempotent; never a duplicate).
* :class:`AlertOutbox` — the deterministic outbox repository +
  processor:

    * ``enqueue(alert)`` — add a pending entry (idempotent by outbox
      id; an already-pending entry is returned unchanged);
    * ``process(now)`` — deliver every due entry through the delivery
      channels; a successful delivery marks the entry DELIVERED and
      removes it; a failure records the failure and, when the retry
      policy permits, keeps the entry pending with the next delay
      (bounded — never an infinite loop; when retries are exhausted the
      entry is marked FAILED and surfaced in the operational report);
    * ``pending / delivered / failed`` views (deterministic order);
    * ``pending_count`` — the outbox depth surfaced in the operational
      health report.

The outbox is OFFLINE and deterministic: delivery goes through the
EXISTING 19.7 :class:`AlertDeliveryChannel` abstraction (the default
console sink is offline; no network / credentials). The outbox does NOT
read candles, does NOT call wall-clock (``now`` is injected), and does
NOT import any broker / execution module.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from dashboard.user_alerts import AlertDeliveryChannel
from engine.config.reliability_config import RetryPolicy
from engine.models.operational_health import (
    ReliabilityFailureCategory,
)
from engine.models.user_alerts import (
    AlertDeliveryResult,
    AlertDeliveryStatus,
    AlertEvent,
)

OUTBOX_ID_PREFIX = "outbox-"


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None:
        raise ValueError(
            f"{name} must be timezone-aware (naive datetimes are never "
            "silently accepted).",
        )


def build_outbox_id(alert_id: str, policy_version: str) -> str:
    """Deterministic outbox entry id over the alert id + policy version.

    Re-enqueueing the same alert under the same policy yields the SAME
    outbox id — the retry can never become a second outbox entry.
    """

    if not alert_id.strip():
        raise ValueError("alert_id must not be empty.")
    if not policy_version.strip():
        raise ValueError("policy_version must not be empty.")
    payload = f"alert={alert_id}|policy={policy_version}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{OUTBOX_ID_PREFIX}{digest}"


@dataclass(frozen=True, slots=True)
class PendingOutboxEntry:
    """
    ONE immutable outbox entry.

    The alert is retained BY REFERENCE (frozen — never modified, never
    rebuilt); ``attempts`` counts the delivery attempts so far;
    ``next_retry_at`` is the deterministic instant the entry becomes due
    for its next attempt (``None`` when the entry is not currently
    scheduled for a retry); ``last_failure`` is the last delivery
    failure detail (``""`` when none).
    """

    outbox_id: str
    alert: AlertEvent
    policy_version: str
    attempts: int = 0
    next_retry_at: datetime | None = None
    last_failure: str = ""

    def __post_init__(self) -> None:
        if not self.outbox_id.strip():
            raise ValueError("outbox_id must not be empty.")
        if not isinstance(self.alert, AlertEvent):
            raise TypeError("alert must be an AlertEvent.")
        if not self.policy_version.strip():
            raise ValueError("policy_version must not be empty.")
        if isinstance(self.attempts, bool) or not isinstance(self.attempts, int):
            raise TypeError("attempts must be an int, not bool.")
        if self.attempts < 0:
            raise ValueError("attempts must be non-negative.")
        if self.next_retry_at is not None:
            _require_aware(self.next_retry_at, "next_retry_at")

    @property
    def alert_id(self) -> str:
        return self.alert.alert_id

    def is_due(self, now: datetime) -> bool:
        """True when the entry is due for a delivery attempt at ``now``.

        An entry with no ``next_retry_at`` is due immediately (first
        attempt). A future-dated ``next_retry_at`` is NOT due.
        """

        _require_aware(now, "now")
        if self.next_retry_at is None:
            return True
        return now >= self.next_retry_at


class AlertOutbox:
    """
    Deterministic alert delivery outbox (Checkpoint 19.8).

    ``enqueue`` adds an alert for delivery; ``process`` delivers every
    due entry through the configured channels, records the delivery
    result, and (on failure) schedules a bounded retry that preserves the
    ORIGINAL alert id. Delivery NEVER mutates the lifecycle and NEVER
    alters the alert identity.

    The outbox is stateless apart from its pending/delivered/failed
    maps and the injected retry policy. All timestamps are injected
    (``now``); no wall-clock.
    """

    def __init__(
        self,
        retry_policy: RetryPolicy | None = None,
        channels: Sequence[AlertDeliveryChannel] | None = None,
    ) -> None:
        self.retry_policy = retry_policy or RetryPolicy()
        self.channels = tuple(channels or ())
        self._pending: dict[str, PendingOutboxEntry] = {}
        self._delivered: dict[str, PendingOutboxEntry] = {}
        self._failed: dict[str, PendingOutboxEntry] = {}
        self._delivery_results: dict[str, AlertDeliveryResult] = {}

    # ------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------

    def enqueue(
        self,
        alert: AlertEvent,
        policy_version: str,
    ) -> PendingOutboxEntry:
        """Add an alert to the outbox (idempotent by outbox id).

        Re-enqueueing an alert that is already pending returns the
        existing entry unchanged (never a duplicate). An alert that was
        already DELIVERED is NOT re-enqueued (returns the delivered
        entry); an alert that was already FAILED (retries exhausted) is
        re-enqueued as a NEW attempt cycle (a later operator retry may
        succeed).
        """

        if not isinstance(alert, AlertEvent):
            raise TypeError("alert must be an AlertEvent.")
        outbox_id = build_outbox_id(alert.alert_id, policy_version)
        existing = self._pending.get(outbox_id)
        if existing is not None:
            return existing
        delivered = self._delivered.get(outbox_id)
        if delivered is not None:
            return delivered
        entry = PendingOutboxEntry(
            outbox_id=outbox_id,
            alert=alert,
            policy_version=policy_version,
            attempts=0,
            next_retry_at=None,
            last_failure="",
        )
        self._pending[outbox_id] = entry
        return entry

    def process(self, now: datetime) -> tuple[AlertDeliveryResult, ...]:
        """
        Deliver every DUE pending entry once.

        Deterministic order (outbox id ascending). A successful delivery
        marks the entry DELIVERED (removed from pending). A failure
        records the failure and schedules a bounded retry when the retry
        policy permits (the entry keeps its ORIGINAL alert id); when
        retries are exhausted the entry is marked FAILED (removed from
        pending, surfaced in the operational report). Returns the
        delivery results for this pass (deterministic order).
        """

        _require_aware(now, "now")
        due = sorted(
            (e for e in self._pending.values() if e.is_due(now)),
            key=lambda e: e.outbox_id,
        )
        results: list[AlertDeliveryResult] = []
        for entry in due:
            result = self._deliver_entry(entry, now)
            results.append(result)
            self._delivery_results[entry.alert_id] = result
        return tuple(results)

    def _deliver_entry(
        self,
        entry: PendingOutboxEntry,
        now: datetime,
    ) -> AlertDeliveryResult:
        """Deliver ONE entry through the channels (deterministic).

        Mirrors the 19.7 delivery semantics: with no channels the alert
        is SKIPPED (explicit); the first successful channel marks
        DELIVERED; a raising channel is recorded as a failure.
        """

        alert = entry.alert
        if not self.channels:
            result = AlertDeliveryResult(
                alert_id=alert.alert_id,
                status=AlertDeliveryStatus.SKIPPED,
                delivery_timestamp=now,
                channel="",
                reason=(
                    "no delivery channel configured — alert generated "
                    "but not delivered (explicit, auditable).",
                ),
            )
            self._mark_done(entry, result)
            return result

        last_error: Exception | None = None
        for channel in self.channels:
            try:
                channel.deliver(alert)
            except Exception as exc:  # delivery never raises into the outbox
                last_error = exc
                continue
            result = AlertDeliveryResult(
                alert_id=alert.alert_id,
                status=AlertDeliveryStatus.DELIVERED,
                delivery_timestamp=now,
                channel=channel.name,
                reason=f"alert delivered through channel {channel.name!r}.",
            )
            self._mark_done(entry, result)
            return result

        # All channels failed. Bounded retry preserves the alert id.
        attempts = entry.attempts + 1
        category = ReliabilityFailureCategory.DELIVERY_FAILURE
        failure_detail = (
            f"all delivery channels failed "
            f"({type(last_error).__name__}: {last_error})"
        )
        if self.retry_policy.should_retry(
            attempts - 1, category.name,
        ):
            delay = self.retry_policy.delay_for(attempts)
            updated = PendingOutboxEntry(
                outbox_id=entry.outbox_id,
                alert=alert,
                policy_version=entry.policy_version,
                attempts=attempts,
                next_retry_at=now + timedelta(seconds=delay),
                last_failure=failure_detail,
            )
            self._pending[entry.outbox_id] = updated
            return AlertDeliveryResult(
                alert_id=alert.alert_id,
                status=AlertDeliveryStatus.FAILED,
                delivery_timestamp=now,
                channel="",
                reason=(
                    f"delivery failed (attempt {attempts}); retry "
                    f"scheduled in {delay:.3f}s — alert id preserved "
                    f"({alert.alert_id})."
                ),
            )
        result = AlertDeliveryResult(
            alert_id=alert.alert_id,
            status=AlertDeliveryStatus.FAILED,
            delivery_timestamp=now,
            channel="",
            reason=(
                f"delivery failed after {attempts} attempt(s); retry "
                "budget exhausted — alert preserved for operator review "
                f"({alert.alert_id})."
            ),
        )
        self._mark_done(entry, result)
        return result

    def _mark_done(
        self,
        entry: PendingOutboxEntry,
        result: AlertDeliveryResult,
    ) -> None:
        """Move an entry out of pending (delivered or failed)."""

        self._pending.pop(entry.outbox_id, None)
        if result.status is AlertDeliveryStatus.DELIVERED:
            self._delivered[entry.outbox_id] = entry
        elif result.status is AlertDeliveryStatus.FAILED:
            self._failed[entry.outbox_id] = entry
        # SKIPPED entries are removed from pending and not retained
        # (they were never attempted; the 19.7 engine already records
        # the explicit skip).

    # ------------------------------------------------------------
    # READ (deterministic)
    # ------------------------------------------------------------

    @property
    def pending_entries(self) -> tuple[PendingOutboxEntry, ...]:
        return tuple(
            self._pending[k] for k in sorted(self._pending)
        )

    @property
    def delivered_entries(self) -> tuple[PendingOutboxEntry, ...]:
        return tuple(
            self._delivered[k] for k in sorted(self._delivered)
        )

    @property
    def failed_entries(self) -> tuple[PendingOutboxEntry, ...]:
        return tuple(
            self._failed[k] for k in sorted(self._failed)
        )

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def delivered_count(self) -> int:
        return len(self._delivered)

    @property
    def failed_count(self) -> int:
        return len(self._failed)

    def delivery_for(self, alert_id: str) -> AlertDeliveryResult | None:
        return self._delivery_results.get(alert_id)

    def pending_alert_ids(self) -> tuple[str, ...]:
        """The ORIGINAL alert ids of all pending entries (sorted)."""
        return tuple(
            e.alert_id for e in self.pending_entries
        )

    def reset(self) -> None:
        self._pending.clear()
        self._delivered.clear()
        self._failed.clear()
        self._delivery_results.clear()


__all__ = [
    "OUTBOX_ID_PREFIX",
    "AlertOutbox",
    "PendingOutboxEntry",
    "build_outbox_id",
]