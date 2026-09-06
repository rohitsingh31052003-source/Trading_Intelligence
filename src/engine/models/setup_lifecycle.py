"""
Setup lifecycle models (Checkpoint 19.6).

These models implement the SETUP LIFECYCLE layer that answers, over the
deterministic scan-cycle stream built by 19.1-19.5:

    "Is this the SAME setup across subsequent scan cycles, what
     lifecycle state is it currently in, and how has that setup evolved
     over time?"

The layer consumes the FROZEN 19.5 :class:`SetupQualityResult`
(point-in-time setup-quality assessment) + the FROZEN 19.3 scan-cycle
identity and provides STABLE identity, LIFECYCLE STATE, TEMPORAL
CONTINUITY, DETERMINISTIC TRANSITIONS, EXPLICIT TRANSITION REASONS and
an AUDITABLE lifecycle history.

DESIGN PRINCIPLES (documented in detail in the Checkpoint 19.6 audit
document):

* SETUP IDENTITY ≠ SETUP ASSESSMENT. A setup's identity is a
  deterministic function of its STABLE ANALYTICAL PROPERTIES
  (instrument, directional context, analytical setup type, primary
  timeframe + an identity-rule version). It deliberately does NOT
  depend on scan-cycle id, wall-clock time, provider order, random
  UUIDs, exact price, exact score, exact candle timestamps or transient
  data-quality fields. Two assessments of the SAME identity at different
  instants belong to the SAME lifecycle; a score change never creates a
  duplicate.

* DIRECTION IS PART OF IDENTITY (documented justification): the reused
  11R/19.5 setup model is intrinsically directional — a bullish
  candidate and a bearish candidate on the same symbol/timeframe are
  DIFFERENT setup objects. A directional-context reversal therefore
  creates a NEW identity and deterministically INVALIDATES the previously
  active opposite-direction lifecycle (the old setup's directional
  premise no longer holds).

* SETUP TYPE IS PART OF IDENTITY (documented justification): the reused
  11R taxonomy defines the analytical candidate CATEGORY — a
  BREAKOUT candidate and a TREND_CONTINUATION candidate on the same
  symbol/direction are different setup objects. A setup-type change is
  therefore a NEW setup (never a silent mutation of the old lifecycle).

* LIFECYCLE STATE ≠ QUALITY CLASSIFICATION. Quality score /
  classification changes are represented as ASSESSMENT changes on the
  lifecycle's observations; the lifecycle state changes only through
  the explicit transition table (confirmation / invalidation /
  expiration).

* DATA FAILURE ≠ SETUP INVALIDATION. A missing / stale / incomplete /
  unsupported symbol observation is recorded with an explicit
  observation status (DATA_UNAVAILABLE) and does NOT change the
  lifecycle state or the expiry counter. Only CLEAN non-observation
  cycles (the symbol was successfully scanned and produced NO
  directional candidate) count toward the documented expiration rule.

* LIFECYCLE EVENTS are immutable, timestamped, deterministic and
  explainable. They describe WHAT HAPPENED (an observation / a state
  transition) — they are never an execution command.

* NO RETROACTIVE MUTATION. Lifecycle history is an append-only
  immutable record of what the system knew at each observation instant.
  Processing future observations never rewrites an earlier observation.

* NO LOOK-AHEAD. Lifecycle decisions at instant T use only lifecycle
  history <= T and the 19.5 assessment at T. Future observations cannot
  alter earlier decisions (proven by tests).

* NO EXECUTION / ALERT / TRADE-PLAN / RELIABILITY / FORWARD-TESTING
  SEMANTICS. The lifecycle concerns the setup's analytical existence and
  evolution only. No entry/stop/target, no position, no broker/order,
  no notification, no persistence framework (19.7-19.9 own those).

Design rules (match the rest of the model layer):

* Frozen + slots dataclasses.
* Optional fields use ``None`` so "unobserved" / "unavailable" is never
  silently a real value.
* ``__post_init__`` performs structural validation only; the identity /
  transition logic lives in the engine and pure, exported helpers.
* No wall-clock dependence: timestamps are explicit, caller-supplied,
  timezone-aware values.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from engine.models.setup_confluence import SetupDirection


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


#: Setup-identity id prefix (documented, deterministic).
SETUP_ID_PREFIX = "setup-"

#: Lifecycle-instance id prefix (documented, deterministic).
LIFECYCLE_ID_PREFIX = "lifecycle-"

#: Lifecycle-observation id prefix (documented, deterministic).
LIFECYCLE_OBSERVATION_ID_PREFIX = "obs-"

#: Lifecycle-transition id prefix (documented, deterministic).
LIFECYCLE_TRANSITION_ID_PREFIX = "trans-"

#: Lifecycle-processing-cycle id prefix (documented, deterministic).
LIFECYCLE_CYCLE_ID_PREFIX = "lc-cycle-"


class LifecycleObservationStatus(Enum):
    """
    Per-observation data/observation status for a setup lifecycle.

    This enum is DELIBERATELY DISTINCT from the 19.5
    :class:`SetupQualityStatus` (evaluation state) and from the
    execution/paper-trade lifecycle statuses. It describes HOW the
    lifecycle's own setup identity was (or was not) observed in one
    scan cycle.

    OBSERVED
        The lifecycle's own setup identity was present this cycle (the
        symbol produced a directional setup candidate with the
        lifecycle's setup id). Advances the lifecycle: the observation
        is recorded and confirmation/expiry counters reset.

    ABSENT
        The symbol was CLEANLY scanned this cycle but the lifecycle's
        own setup identity was NOT present (the assessment produced NO
        directional candidate, or listed a DIFFERENT setup type in the
        SAME directional context). Counts toward the documented
        expiration rule (consecutive clean misses).

    DATA_UNAVAILABLE
        The symbol could NOT be cleanly evaluated this cycle (19.5
        INCOMPLETE / UNAVAILABLE data-gate status, provider failure,
        empty / stale-without-state data, or the symbol was absent from
        the cycle results). NEVER an invalidation; the lifecycle state
        and the expiry counter are left UNCHANGED.

    SUPERSEDED
        The lifecycle's own identity was not observed because an
        OPPOSING-direction candidate appeared for the same
        instrument/timeframe (directional-context reversal). This is
        the deterministic disproof that INVALIDATES the lifecycle. The
        expiry counter is irrelevant (the lifecycle becomes terminal).

    LATE_REJECTED
        A genuinely out-of-chronological-order observation (an
        observation timestamp STRICTLY older than the lifecycle's latest
        observation, and not already recorded). The observation is NOT
        persisted and the lifecycle state is NOT changed. This status
        appears ONLY on a returned processing result — never on a
        persisted lifecycle observation.
    """

    OBSERVED = "OBSERVED"
    ABSENT = "ABSENT"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    SUPERSEDED = "SUPERSEDED"
    LATE_REJECTED = "LATE_REJECTED"


class SetupLifecycleState(Enum):
    """
    Lifecycle state of a setup candidate (Checkpoint 19.6).

    This vocabulary is JUSTIFIED by the 19.5 semantics and the 19.6
    requirements (see the audit document section ``State vocabulary``).
    Four states — the smallest deterministic machine that satisfies the
    requirements:

    DETECTED
        A directional setup candidate (identity resolvable) has been
        observed, but it has never produced a QUALIFIED 19.5 assessment
        (the reused 11Q classification is not POTENTIAL_SETUP, or the
        quality bar was not reached yet). The candidate is present and
        being monitored — it has NOT been confirmed. Carries NO
        execution/trade semantics. Non-terminal.

    CONFIRMED
        The setup produced at least one QUALIFIED 19.5 assessment
        (reused 11Q POTENTIAL_SETUP) according to the configured
        confirmation rule. Confirmation is a HISTORICAL, one-way
        property: once confirmed, later quality fluctuations are
        recorded as assessment changes on observations but do NOT
        revert the state to DETECTED. Non-terminal.

    INVALIDATED
        Terminal. A deterministic disproof condition was met: an
        OPPOSING-direction setup candidate appeared for the same
        instrument/timeframe (the tracked setup's directional premise no
        longer holds). This is NEVER triggered by a data failure or a
        plain absence. Terminal; a later reappearance of the same
        identity starts a NEW lifecycle instance.

    EXPIRED
        Terminal. The setup's identity was not observed for
        ``max_consecutive_missing_observations`` CONSECUTIVE CLEAN scan
        cycles (19.5 NO_SETUP / WATCH-without-direction). Expiration is
        driven by the deterministic scan-cycle stream, never a
        wall-clock timeout, and never by data-unavailable cycles.
        Terminal; a later reappearance of the same identity starts a NEW
        lifecycle instance.
    """

    DETECTED = "DETECTED"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        """Whether the state is a terminal lifecycle state."""
        return self in (
            SetupLifecycleState.INVALIDATED,
            SetupLifecycleState.EXPIRED,
        )

    @property
    def rank_value(self) -> int:
        """Deterministic ordering (strongest-first for active states)."""
        return _LIFECYCLE_STATE_RANK[self]


_LIFECYCLE_STATE_RANK = {
    SetupLifecycleState.CONFIRMED: 3,
    SetupLifecycleState.DETECTED: 2,
    SetupLifecycleState.INVALIDATED: 1,
    SetupLifecycleState.EXPIRED: 0,
}


#: Directional context members used for setup identity.
_DIRECTIONAL = (SetupDirection.BULLISH, SetupDirection.BEARISH)

#: Opposite-direction map (BULLISH <-> BEARISH).
_OPPOSITE_DIRECTION = {
    SetupDirection.BULLISH: SetupDirection.BEARISH,
    SetupDirection.BEARISH: SetupDirection.BULLISH,
}


def opposite_direction(direction: SetupDirection) -> SetupDirection:
    """Deterministic opposite-direction mapping (BULLISH <-> BEARISH)."""

    if direction not in _OPPOSITE_DIRECTION:
        raise ValueError(
            f"opposite direction is only defined for BULLISH / BEARISH "
            f"(got {direction.value!r}).",
        )
    return _OPPOSITE_DIRECTION[direction]


def _canonical_timeframe(timeframe: str) -> str:
    """Canonical timeframe lookup via the engine canonical utility."""

    from engine.data.historical_times import canonical_timeframe

    resolved = canonical_timeframe(timeframe)
    if resolved is None:
        raise ValueError(
            f"unknown timeframe label {timeframe!r} — arbitrary strings "
            "are never silently accepted.",
        )
    return resolved


@dataclass(frozen=True, slots=True)
class SetupIdentity:
    """
    The deterministic identity of a setup candidate.

    Identity components (all STABLE ANALYTICAL properties):

    instrument
        Canonical instrument name.

    direction
        The candidate's DIRECTIONAL CONTEXT (BULLISH / BEARISH only —
        NEUTRAL / UNKNOWN never identify a setup). Part of identity
        because the 19.5/11R setup model is intrinsically directional
        (documented justification in the audit doc).

    setup_type
        The reused 11R analytical setup-type name (e.g. ``"BREAKOUT"``,
        ``"TREND_CONTINUATION"``, ``"STRUCTURE_CONTINUATION"``,
        ``"SETUP_CANDIDATE"``) — the analytical CATEGORY of the setup.
        Part of identity (documented justification in the audit doc).

    primary_timeframe
        Canonical primary (setup) timeframe the setup detection ran on —
        a stable analytical property of the evaluation configuration.

    identity_version
        Identity-rule version embedded in the setup id.

    setup_id
        Deterministic setup id (``"setup-" + sha256[:16]``) over the
        canonical identity payload. Sets the identity of the setup for
        its entire lifetime — independent of scan cycle, wall-clock,
        price, score, candle timestamps and data quality.
    """

    instrument: str
    direction: SetupDirection
    setup_type: str
    primary_timeframe: str
    setup_id: str
    identity_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _canonical_name(self.instrument))
        if not isinstance(self.direction, SetupDirection):
            raise TypeError("direction must be a SetupDirection.")
        if self.direction not in _DIRECTIONAL:
            raise ValueError(
                "setup identity direction must be BULLISH or BEARISH "
                "(NEUTRAL / UNKNOWN never identify a setup candidate).",
            )
        if not isinstance(self.setup_type, str) or not self.setup_type.strip():
            raise ValueError(
                "setup_type must be a non-empty analytical setup-type name.",
            )
        object.__setattr__(
            self, "primary_timeframe", _canonical_timeframe(self.primary_timeframe),
        )
        if isinstance(self.identity_version, bool) or not isinstance(
            self.identity_version, int,
        ):
            raise TypeError("identity_version must be an int, not bool.")
        if self.identity_version < 1:
            raise ValueError("identity_version must be >= 1.")
        if not isinstance(self.setup_id, str) or not self.setup_id.strip():
            raise ValueError("setup_id must be a non-empty str.")
        expected = build_setup_id(
            self.instrument,
            self.direction,
            self.setup_type,
            self.primary_timeframe,
            self.identity_version,
        )
        if self.setup_id != expected:
            raise ValueError(
                "setup_id is inconsistent with the identity components "
                f"(expected {expected!r}).",
            )

    def __str__(self) -> str:
        return (
            f"<{self.instrument} {self.direction.value} "
            f"{self.setup_type} {self.primary_timeframe}>"
        )


def build_setup_id(
    instrument: str,
    direction: SetupDirection,
    setup_type: str,
    primary_timeframe: str,
    identity_version: int = 1,
) -> str:
    """Deterministic setup identity (``"setup-" + sha256[:16]``)."""

    canon_instrument = _canonical_name(instrument)
    if not isinstance(direction, SetupDirection):
        raise TypeError("direction must be a SetupDirection.")
    if direction not in _DIRECTIONAL:
        raise ValueError(
            "setup identity direction must be BULLISH or BEARISH.",
        )
    tf = _canonical_timeframe(primary_timeframe)
    if not isinstance(setup_type, str) or not setup_type.strip():
        raise ValueError("setup_type must be a non-empty str.")
    payload = "|".join(
        (
            "instrument", canon_instrument,
            "direction", direction.value,
            "setup_type", setup_type.strip().upper(),
            "timeframe", tf,
            "identity_version", str(identity_version),
        ),
    )
    return _sha256_prefix(payload, SETUP_ID_PREFIX)


def _identity_payload(identity: SetupIdentity) -> str:
    return "|".join(
        (
            identity.instrument,
            identity.direction.value,
            identity.setup_type.strip().upper(),
            identity.primary_timeframe,
        ),
    )


@dataclass(frozen=True, slots=True)
class SetupLifecycleObservation:
    """
    ONE immutable, deterministic observation of a setup lifecycle at one
    scan instant.

    An observation records what the system KNEW at that instant — it is
    never retroactively modified when later observations arrive.

    Attributes:

    observation_id
        Deterministic observation id (``"obs-" + sha256[:16]``) over
        lifecycle + scan-cycle + timestamp + status + assessment
        fingerprint — the idempotency basis for replaying the same
        scan cycle.

    lifecycle_id
        The lifecycle instance this observation belongs to.

    setup_id
        The setup identity (same as the lifecycle's setup id).

    instrument
        Canonical instrument name.

    scan_cycle_id
        The 19.3 scan-cycle identity that produced this observation.

    observation_timestamp
        The deterministic observation instant (the 19.5 reference
        instant; explicit, timezone-aware).

    observation_status
        :class:`LifecycleObservationStatus` — how the setup was (or was
        not) observed.

    at_state
        The lifecycle state ARRIVED AT by this observation (the state
        recorded in the lifecycle after this observation was
        processed). ``None`` when the observation was rejected (never
        persisted for LATE_REJECTED).

    quality_status / quality_classification / quality_score
        Reused 19.5 status / classification / score snapshot at this
        instant (``None`` when the data gate prevented a score).

    setup_type / direction
        The lifecycle identity's analytical setup type and directional
        context (auditable redundancy).

    assessment
        The reused 19.5 :class:`SetupQualityResult` that drove this
        observation (BY REFERENCE — frozen, never modified), or ``None``
        for non-assessment-driven statuses (SUPERSEDED / LATE_REJECTED).

    reason
        Human-readable explanation of the observation (derived from the
        actual inputs).
    """

    observation_id: str
    lifecycle_id: str
    setup_id: str
    instrument: str
    scan_cycle_id: str
    observation_timestamp: datetime
    observation_status: LifecycleObservationStatus
    at_state: SetupLifecycleState | None
    quality_status: str | None = None
    quality_classification: str | None = None
    quality_score: int | None = None
    setup_type: str = ""
    direction: SetupDirection | None = None
    assessment: object | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _canonical_name(self.instrument))
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if not self.lifecycle_id.strip():
            raise ValueError("lifecycle_id must not be empty.")
        if not self.setup_id.strip():
            raise ValueError("setup_id must not be empty.")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        if not self.observation_id.strip():
            raise ValueError("observation_id must not be empty.")
        if isinstance(self.observation_status, bool) or not isinstance(
            self.observation_status, LifecycleObservationStatus,
        ):
            raise TypeError("observation_status must be a LifecycleObservationStatus.")
        if self.at_state is not None and not isinstance(
            self.at_state, SetupLifecycleState,
        ):
            raise TypeError("at_state must be a SetupLifecycleState or None.")
        if self.at_state is None and (
            self.observation_status is not LifecycleObservationStatus.LATE_REJECTED
        ):
            raise ValueError(
                "a persisted observation must carry the arrived-at state "
                "(None state is only valid for a LATE_REJECTED result).",
            )
        if self.direction is not None and not isinstance(
            self.direction, SetupDirection,
        ):
            raise TypeError("direction must be a SetupDirection or None.")


@dataclass(frozen=True, slots=True)
class SetupLifecycleTransition:
    """
    ONE immutable, deterministic lifecycle state transition.

    An explicit, explainable record of a state change:

        DETECTED -> CONFIRMED      "qualified assessment observed"
        DETECTED/CONFIRMED -> INVALIDATED
                                   "opposing directional setup observed"
        DETECTED/CONFIRMED -> EXPIRED
                                   "not observed for N consecutive cycles"

    Attributes:

    transition_id
        Deterministic transition id (``"trans-" + sha256[:16]``).

    lifecycle_id
        The lifecycle instance that transitioned.

    setup_id
        The setup identity.

    from_state / to_state
        The explicit state change.

    observation_timestamp
        The deterministic observation instant of the causing
        observation.

    scan_cycle_id
        The 19.3 scan-cycle identity of the causing observation.

    reason
        The explicit transition reason (matched to the implemented
        rule — see the transition table in the audit doc).
    """

    transition_id: str
    lifecycle_id: str
    setup_id: str
    from_state: SetupLifecycleState
    to_state: SetupLifecycleState
    observation_timestamp: datetime
    scan_cycle_id: str
    reason: str

    def __post_init__(self) -> None:
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if not self.lifecycle_id.strip():
            raise ValueError("lifecycle_id must not be empty.")
        if not self.setup_id.strip():
            raise ValueError("setup_id must not be empty.")
        if not self.transition_id.strip():
            raise ValueError("transition_id must not be empty.")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        if not isinstance(self.from_state, SetupLifecycleState):
            raise TypeError("from_state must be a SetupLifecycleState.")
        if not isinstance(self.to_state, SetupLifecycleState):
            raise TypeError("to_state must be a SetupLifecycleState.")
        if self.from_state == self.to_state:
            raise ValueError("a transition must change state.")
        if self.from_state.is_terminal:
            raise ValueError(
                "a terminal lifecycle state may never transition.",
            )
        if self.to_state.is_terminal and self.from_state is self.to_state:
            raise ValueError("self-terminal transitions are invalid.")
        if not self.reason.strip():
            raise ValueError("transition reason must not be empty.")

    @property
    def is_confirmation(self) -> bool:
        return (
            self.from_state is SetupLifecycleState.DETECTED
            and self.to_state is SetupLifecycleState.CONFIRMED
        )

    @property
    def is_invalidation(self) -> bool:
        return self.to_state is SetupLifecycleState.INVALIDATED

    @property
    def is_expiration(self) -> bool:
        return self.to_state is SetupLifecycleState.EXPIRED


@dataclass(frozen=True, slots=True)
class SetupLifecycle:
    """
    ONE immutable SNAPSHOT of a setup lifecycle instance.

    Lifecycle progression produces NEW snapshot records (the engine
    advances state by constructing new immutable snapshots; the store
    keeps the latest snapshot per lifecycle). Each snapshot carries the
    FULL append-only observation + transition history, so the lifecycle
    history is auditable and never retroactively mutated.

    Attributes:

    lifecycle_id
        Deterministic lifecycle-instance id
        (``"lifecycle-" + sha256[:16]`` over setup id + instance
        discriminator). A TERMINAL lifecycle is never reused: the same
        setup id reappearing after a terminal lifecycle starts a NEW
        instance (a distinct ``lifecycle_id``).

    instance_discriminator
        Deterministic, monotonically increasing instance index within a
        setup id (``1`` for the first instance, ``2`` for the next...).

    setup_id
        The setup identity this instance tracks.

    identity
        The full :class:`SetupIdentity` (embedded by value).

    state
        Current :class:`SetupLifecycleState`.

    created_at
        Timestamp of the first observation (explicit, timezone-aware).

    last_observation_timestamp
        Timestamp of the LATEST observation (``None`` when only
        created). Used for out-of-order detection.

    consecutive_missing
        Number of consecutive CLEAN non-observations (ABSENT
        observations) since the last OBSERVED observation.

    latest_observation_id
        Id of the latest observation (``""`` when none yet).

    observations
        Append-only immutable history of
        :class:`SetupLifecycleObservation` (deterministic order).

    transitions
        Append-only immutable history of
        :class:`SetupLifecycleTransition` (deterministic order).

    latest_score / latest_classification / latest_status
        Reused 19.5 score / classification / status of the latest
        observation (``None`` when unavailable / not-yet-observed).

    latest_observation_status
        :class:`LifecycleObservationStatus` of the latest observation.
    """

    lifecycle_id: str
    instance_discriminator: int
    setup_id: str
    identity: SetupIdentity
    state: SetupLifecycleState
    created_at: datetime
    last_observation_timestamp: datetime | None = None
    consecutive_missing: int = 0
    latest_observation_id: str = ""
    observations: tuple[SetupLifecycleObservation, ...] = field(
        default_factory=tuple,
    )
    transitions: tuple[SetupLifecycleTransition, ...] = field(
        default_factory=tuple,
    )
    latest_score: int | None = None
    latest_classification: str | None = None
    latest_status: str | None = None
    latest_observation_status: LifecycleObservationStatus = (
        LifecycleObservationStatus.DATA_UNAVAILABLE
    )

    def __post_init__(self) -> None:
        _require_aware(self.created_at, "created_at")
        if self.last_observation_timestamp is not None:
            _require_aware(
                self.last_observation_timestamp, "last_observation_timestamp",
            )
        if not self.lifecycle_id.strip():
            raise ValueError("lifecycle_id must not be empty.")
        if not self.setup_id.strip():
            raise ValueError("setup_id must not be empty.")
        if not isinstance(self.identity, SetupIdentity):
            raise TypeError("identity must be a SetupIdentity.")
        if self.identity.setup_id != self.setup_id:
            raise ValueError(
                "identity.setup_id must match the lifecycle setup_id.",
            )
        if not isinstance(self.state, SetupLifecycleState):
            raise TypeError("state must be a SetupLifecycleState.")
        if isinstance(self.instance_discriminator, bool) or not isinstance(
            self.instance_discriminator, int,
        ):
            raise TypeError("instance_discriminator must be an int, not bool.")
        if self.instance_discriminator < 1:
            raise ValueError("instance_discriminator must be >= 1.")
        if isinstance(self.consecutive_missing, bool) or not isinstance(
            self.consecutive_missing, int,
        ):
            raise TypeError("consecutive_missing must be an int, not bool.")
        if self.consecutive_missing < 0:
            raise ValueError("consecutive_missing must be >= 0.")
        # A terminal lifecycle carries no further activity by invariant.
        if self.state.is_terminal:
            if self.last_observation_timestamp is None:
                raise ValueError(
                    "a terminal lifecycle must carry a last observation.",
                )
            if self.latest_observation_id == "":
                raise ValueError(
                    "a terminal lifecycle must carry a latest observation id.",
                )
        else:
            if self.last_observation_timestamp is not None:
                if self.latest_observation_id == "":
                    raise ValueError(
                        "an observed active lifecycle must carry a latest "
                        "observation id.",
                    )
            elif self.latest_observation_id != "":
                raise ValueError(
                    "lifecycle with no observation cannot carry a latest "
                    "observation id.",
                )
        # Observations must be strictly chronological (no retroactive
        # mutation is possible) and must reference this lifecycle.
        last_ts: datetime | None = None
        seen_obs: set[str] = set()
        for obs in self.observations:
            if not isinstance(obs, SetupLifecycleObservation):
                raise TypeError(
                    "observations must be SetupLifecycleObservation objects.",
                )
            if obs.lifecycle_id != self.lifecycle_id:
                raise ValueError(
                    "observation lifecycle_id must match the lifecycle.",
                )
            if obs.setup_id != self.setup_id:
                raise ValueError(
                    "observation setup_id must match the lifecycle.",
                )
            if obs.observation_id in seen_obs:
                raise ValueError(
                    f"duplicate observation id {obs.observation_id!r} in "
                    "lifecycle history.",
                )
            seen_obs.add(obs.observation_id)
            if last_ts is None:
                last_ts = obs.observation_timestamp
            else:
                if obs.observation_timestamp <= last_ts:
                    raise ValueError(
                        "lifecycle observations must be strictly "
                        "chronological (a late observation is rejected "
                        "and never appended).",
                    )
                last_ts = obs.observation_timestamp
        # Transitions must be chronological and reference this lifecycle.
        last_t: datetime | None = None
        seen_t: set[str] = set()
        for tr in self.transitions:
            if not isinstance(tr, SetupLifecycleTransition):
                raise TypeError(
                    "transitions must be SetupLifecycleTransition objects.",
                )
            if tr.lifecycle_id != self.lifecycle_id:
                raise ValueError(
                    "transition lifecycle_id must match the lifecycle.",
                )
            if tr.setup_id != self.setup_id:
                raise ValueError(
                    "transition setup_id must match the lifecycle.",
                )
            if tr.transition_id in seen_t:
                raise ValueError(
                    f"duplicate transition id {tr.transition_id!r} in "
                    "lifecycle history.",
                )
            seen_t.add(tr.transition_id)
            if last_t is None:
                last_t = tr.observation_timestamp
            else:
                if tr.observation_timestamp <= last_t:
                    raise ValueError(
                        "lifecycle transitions must be strictly "
                        "chronological.",
                    )
                last_t = tr.observation_timestamp

    # ----------------------------------------------------------
    # CONVENIENCE VIEWS (read-only, deterministic)
    # ----------------------------------------------------------

    @property
    def instrument(self) -> str:
        return self.identity.instrument

    @property
    def direction(self) -> SetupDirection:
        return self.identity.direction

    @property
    def setup_type(self) -> str:
        return self.identity.setup_type

    @property
    def primary_timeframe(self) -> str:
        return self.identity.primary_timeframe

    @property
    def is_active(self) -> bool:
        return not self.state.is_terminal

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def observation_count(self) -> int:
        return len(self.observations)

    @property
    def transition_count(self) -> int:
        return len(self.transitions)

    @property
    def qualified_observations(self) -> int:
        """Count of observations carrying a QUALIFIED 19.5 status."""
        return sum(
            1
            for obs in self.observations
            if obs.quality_status == "QUALIFIED"
        )


@dataclass(frozen=True, slots=True)
class SetupLifecycleObservationResult:
    """
    The deterministic result of processing ONE symbol's assessment
    through the lifecycle engine.

    Attributes:

    instrument
        Canonical instrument name.

    observation_timestamp
        The observation instant (deterministic, aware).

    scan_cycle_id
        The 19.3 scan-cycle identity.

    setup_id
        The resolved setup identity id, or ``None`` when no directional
        candidate was present.

    lifecycle_id
        The lifecycle instance advanced / created, or ``None`` when the
        symbol carried no resolvable setup.

    state
        The lifecycle state AFTER processing (or ``None`` when nothing
        was tracked).

    created
        True when a NEW lifecycle instance was created.

    advanced
        True when an existing lifecycle instance was advanced
        (observation appended).

    duplicate
        True when the observation was already recorded (idempotent
        replay of the same scan cycle / same assessment).

    late_rejected
        True when the observation was genuinely out of chronological
        order (STRICTLY older than the lifecycle's latest observation)
        and was rejected without mutating the lifecycle.

    observation
        The appended observation (or ``None`` when rejected / nothing
        tracked).

    transition
        The transition event produced (or ``None`` when no state change
        occurred).

    observation_status
        :class:`LifecycleObservationStatus` of this processing.

    reason
        Human-readable summary of the processing outcome.
    """

    instrument: str
    observation_timestamp: datetime
    scan_cycle_id: str
    setup_id: str | None = None
    lifecycle_id: str | None = None
    state: SetupLifecycleState | None = None
    created: bool = False
    advanced: bool = False
    duplicate: bool = False
    late_rejected: bool = False
    observation: SetupLifecycleObservation | None = None
    transition: SetupLifecycleTransition | None = None
    observation_status: LifecycleObservationStatus | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument", _canonical_name(self.instrument))
        _require_aware(self.observation_timestamp, "observation_timestamp")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        if self.late_rejected and self.duplicate:
            raise ValueError(
                "late_rejected and duplicate are mutually exclusive.",
            )
        if self.observation is not None and not isinstance(
            self.observation, SetupLifecycleObservation,
        ):
            raise TypeError("observation must be a SetupLifecycleObservation.")
        if self.transition is not None and not isinstance(
            self.transition, SetupLifecycleTransition,
        ):
            raise TypeError("transition must be a SetupLifecycleTransition.")
        if self.observation_status is not None and not isinstance(
            self.observation_status, LifecycleObservationStatus,
        ):
            raise TypeError(
                "observation_status must be a LifecycleObservationStatus.",
            )


@dataclass(frozen=True, slots=True)
class SetupLifecycleCounts:
    """
    Deterministic aggregate counts over a lifecycle-processing cycle.

    ``created / advanced / duplicate / late_rejected`` reflect the
    observation-result classification; ``detected / confirmed /
    invalidated / expired`` tally the lifecycle states reached AFTER
    processing each symbol observation; ``active / terminal`` are the
    store-wide instance counts after the cycle.
    """

    created: int = 0
    advanced: int = 0
    duplicate: int = 0
    late_rejected: int = 0
    observed: int = 0
    absent: int = 0
    data_unavailable: int = 0
    superseded: int = 0
    detected: int = 0
    confirmed: int = 0
    invalidated: int = 0
    expired: int = 0
    active: int = 0
    terminal: int = 0

    @property
    def processed(self) -> int:
        """Number of symbol observations processed this cycle."""
        return (
            self.created
            + self.advanced
            + self.duplicate
            + self.late_rejected
        )


@dataclass(frozen=True, slots=True)
class SetupLifecycleCycleResult:
    """
    ONE deterministic universe-wide lifecycle-processing cycle.

    Consumes the FROZEN 19.5 :class:`SetupQualityUniverseResult`
    (every requested constituent appears EXACTLY ONCE) + the FROZEN
    19.3 scan-cycle identity, and produces one
    :class:`SetupLifecycleObservationResult` per symbol.

    Attributes:

    cycle_id
        Deterministic lifecycle-processing-cycle id
        (``"lc-cycle-" + sha256[:16]``) over scan-cycle id + reference
        instant + config snapshot. Reprocessing the same scan cycle with
        identical lifecycle-store state yields the SAME cycle id.

    scan_cycle_id
        The 19.3 scan-cycle identity consumed.

    reference_now
        The deterministic observation instant (from the 19.5 universe
        result).

    instruments
        Processed instruments (canonical, sorted).

    results
        Per-symbol :class:`SetupLifecycleObservationResult` — EXACTLY
        ONE per processed instrument (deterministic order).

    counts
        :class:`SetupLifecycleCounts`.

    active_count / terminal_count
        Store-wide lifecycle instance counts after processing.
    """

    cycle_id: str
    scan_cycle_id: str
    reference_now: datetime
    instruments: tuple[str, ...] = field(default_factory=tuple)
    results: tuple[SetupLifecycleObservationResult, ...] = field(
        default_factory=tuple,
    )
    counts: SetupLifecycleCounts = field(default_factory=SetupLifecycleCounts)
    active_count: int = 0
    terminal_count: int = 0

    def __post_init__(self) -> None:
        _require_aware(self.reference_now, "reference_now")
        if not self.cycle_id.strip():
            raise ValueError("cycle_id must not be empty.")
        if not self.scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must not be empty.")
        canon = tuple(sorted(set(_canonical_name(n) for n in self.instruments)))
        object.__setattr__(self, "instruments", canon)
        if self.results and self.instruments:
            for r in self.results:
                if not isinstance(r, SetupLifecycleObservationResult):
                    raise TypeError(
                        "results must be SetupLifecycleObservationResult.",
                    )
                if r.instrument not in canon:
                    raise ValueError(
                        "observation results reference an instrument "
                        f"outside the processed universe "
                        f"({r.instrument!r}).",
                    )

    def result_for(self, instrument: str) -> SetupLifecycleObservationResult | None:
        """Primary per-symbol lookup (``None`` when not present).

        A symbol may touch multiple lifecycles in one cycle (e.g. an
        opposing-direction candidate both invalidates the old setup and
        creates the new one); this returns the FIRST deterministic
        per-lifecycle result for the symbol (the tracked-lifecycle
        result when a directional candidate was present).
        """
        canon = _canonical_name(instrument)
        for r in self.results:
            if r.instrument == canon:
                return r
        return None

    @property
    def is_empty(self) -> bool:
        return not self.results


__all__ = [
    "LIFECYCLE_CYCLE_ID_PREFIX",
    "LIFECYCLE_ID_PREFIX",
    "LIFECYCLE_OBSERVATION_ID_PREFIX",
    "LIFECYCLE_TRANSITION_ID_PREFIX",
    "LifecycleObservationStatus",
    "SETUP_ID_PREFIX",
    "SetupIdentity",
    "SetupLifecycle",
    "SetupLifecycleCounts",
    "SetupLifecycleCycleResult",
    "SetupLifecycleObservation",
    "SetupLifecycleObservationResult",
    "SetupLifecycleState",
    "SetupLifecycleTransition",
    "build_setup_id",
    "opposite_direction",
]