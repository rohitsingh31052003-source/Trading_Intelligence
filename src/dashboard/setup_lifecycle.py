"""
Setup lifecycle engine + in-memory store (Checkpoint 19.6).

This module transforms the FROZEN 19.5 setup-quality universe result
into TEMPORAL, DETERMINISTIC SETUP LIFECYCLES:

    Scan Cycle (19.3 identity)
            |
    19.4 MTF
            |
    19.5 Setup Quality
            |
    19.6 SETUP LIFECYCLE (this engine)
            |
    19.7 USER ALERTS (future)

The engine is a downstream consumer of setup-quality evaluation. It
implements NO new setup detection / scoring / classification (19.5 owns
setup quality) and NO other scanner (it consumes the 19.5 universe
result, whose per-symbol order is already canonical/frozen).

The engine is STATELESS with respect to lifecycle state — all mutable
lifecycle storage is isolated behind the injected
:class:`SetupLifecycleStore` (in-memory at this stage, per the 19.6
requirement; 19.8 owns advanced persistence/recovery).

DESIGN SUMMARY (detail in the Checkpoint 19.6 audit document):

* SETUP IDENTITY: deterministic function of stable analytical
  properties (instrument + directional context + analytical setup type
  + primary timeframe + identity-rule version). Score / price / candle
  timestamp / scan-cycle id / data-quality fields are NOT identity
  components — a score change never creates a duplicate lifecycle.

* FIRST OBSERVATION: a directional candidate (identity resolvable)
  with no active lifecycle creates a NEW lifecycle instance. The
  instance begins in DETECTED, and the SAME processing call applies the
  CONFIRMED transition when the first observation is already qualified.

* REPEATED OBSERVATION: the same setup id resolves the SAME active
  lifecycle; the new 19.5 assessment is appended as an observation and
  the transition rules are applied. No duplicate lifecycle is created.

* QUALITY CHANGES: score / classification fluctuations are recorded on
  observations; they do NOT change the lifecycle state (once CONFIRMED,
  the state stays CONFIRMED while the setup remains present).

* DATA FAILURE vs INVALIDATION: a data-gate / unavailable symbol
  observation (19.5 INCOMPLETE / UNAVAILABLE, provider failure, missing
  symbol) is recorded with ``DATA_UNAVAILABLE`` and neither changes the
  state nor the expiry counter. Only CLEAN non-observation cycles count
  toward EXPIRATION.

* DIRECTION REVERSAL: an opposing-direction candidate on the same
  instrument/timeframe deterministically INVALIDATES every active
  lifecycle with the opposite directional context (the old setup's
  directional premise no longer holds) — a NEW lifecycle starts for the
  new identity.

* SETUP-TYPE CHANGE: a different analytical setup type in the SAME
  directional context is a NEW setup (identity component); the previous
  active lifecycle(s) of the same direction receive ABSENT
  observations (documented as "displaced by a different-type setup").

* EXPIRATION: N consecutive clean non-observations (ABSENT) => EXPIRED.
  Driven by the deterministic scan-cycle stream, never a wall-clock
  timeout, never by data-unavailable cycles.

* TERMINAL STATES: INVALIDATED / EXPIRED are absorbing. A later
  reappearance of the same setup id starts a NEW instance (a distinct
  ``lifecycle_id`` with an incremented instance discriminator) — a
  terminal lifecycle is never reactivated, never reopened.

* IDEMPOTENCY: processing the same scan cycle with the same assessment
  twice produces the same deterministic observation id and is detected
  as a duplicate (no new observation / transition / counter change).

* OUT-OF-ORDER: a genuinely stale observation (timestamp STRICTLY older
  than the lifecycle's latest observation and not already recorded) is
  rejected with ``late_rejected=True`` and does NOT mutate the
  lifecycle.

* NO LOOK-AHEAD: lifecycle decisions at T use only lifecycle history
  <= T and the 19.5 assessment at T. Future observations never alter
  earlier decisions / history (proven by tests).

The engine implements NO trading logic: NO trade execution, NO broker
order, NO entry/stop/target, NO portfolio/position management, NO user
notification, NO persistence framework (19.7-19.9 own those).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Sequence

from engine.config.setup_lifecycle_config import (
    SetupLifecycleConfig,
    transition_rule_version,
)
from engine.models.setup_confluence import SetupDirection
from engine.models.setup_lifecycle import (
    LIFECYCLE_CYCLE_ID_PREFIX,
    LIFECYCLE_ID_PREFIX,
    LIFECYCLE_OBSERVATION_ID_PREFIX,
    LIFECYCLE_TRANSITION_ID_PREFIX,
    LifecycleObservationStatus,
    SETUP_ID_PREFIX,
    SetupIdentity,
    SetupLifecycle,
    SetupLifecycleCounts,
    SetupLifecycleCycleResult,
    SetupLifecycleObservation,
    SetupLifecycleObservationResult,
    SetupLifecycleState,
    SetupLifecycleTransition,
    build_setup_id,
    opposite_direction,
)
from engine.models.setup_quality import (
    SetupQualityResult,
    SetupQualityStatus,
    SetupQualityUniverseResult,
)
from engine.models.setup_confluence import SetupClassification


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
    return f"{prefix}{__import__('hashlib').sha256(payload.encode('utf-8')).hexdigest()[:16]}"


#: Status value canonical ordering for observation fingerprints.
_STATUS_FINGERPRINT_ORDER = [
    SetupQualityStatus.QUALIFIED,
    SetupQualityStatus.WATCH,
    SetupQualityStatus.NO_SETUP,
    SetupQualityStatus.INCOMPLETE,
    SetupQualityStatus.UNAVAILABLE,
]


def _assessment_fingerprint(result: SetupQualityResult) -> str:
    """Stable fingerprint of the assessment's identity-relevant + state
    fields (used only for idempotency of identical replays)."""

    parts = [
        result.instrument,
        result.primary_timeframe,
        result.status.value,
        result.classification.value,
        "" if result.score is None else str(result.score),
        (
            result.setup_classification.value
            if result.setup_classification is not None
            else "none"
        ),
        (
            result.setup_direction.value
            if result.setup_direction is not None
            else "none"
        ),
        result.setup_type or "none",
    ]
    return "|".join(parts)


def resolve_setup_identity(
    result: SetupQualityResult,
    identity_version: int = 1,
) -> SetupIdentity | None:
    """
    Deterministic setup-identity resolution from a 19.5 assessment.

    Returns a :class:`SetupIdentity` ONLY when the assessment carries a
    DIRECTIONAL setup candidate (a non-None analytical setup type with
    BULLISH / BEARISH directional context). WATCH-level and NO_SETUP
    assessments without a directional candidate resolve to ``None``
    (no setup to track at this instant). Data-gate assessments
    (INCOMPLETE / UNAVAILABLE) resolve to ``None`` (data unavailable —
    never a setup).
    """

    if not isinstance(result, SetupQualityResult):
        raise TypeError("result must be a SetupQualityResult.")
    direction = result.setup_direction
    setup_type = result.setup_type
    if direction is None or setup_type is None:
        return None
    if direction not in (SetupDirection.BULLISH, SetupDirection.BEARISH):
        return None
    setup_id = build_setup_id(
        result.instrument,
        direction,
        setup_type,
        result.primary_timeframe,
        identity_version,
    )
    return SetupIdentity(
        instrument=result.instrument,
        direction=direction,
        setup_type=setup_type,
        primary_timeframe=result.primary_timeframe,
        setup_id=setup_id,
        identity_version=identity_version,
    )


def is_qualified_assessment(result: SetupQualityResult) -> bool:
    """Deterministic confirmation predicate over the reused 19.5 /
    11Q evidence: the setup is a POTENTIAL_SETUP (11Q), carries the
    19.5 QUALIFIED status, and is NOT a stale-data assessment.

    A data-capped LOW assessment (stale data) still carries the 11Q
    POTENTIAL_SETUP classification and even the 19.5 QUALIFIED status
    label, but its 19.5 ``stale_data`` flag is True, so it NEVER
    confirms a DETECTED lifecycle. (Incomplete / unavailable data-gate
    states resolve to no identity and never reach this predicate.)
    """

    return (
        result.setup_classification is SetupClassification.POTENTIAL_SETUP
        and result.status is SetupQualityStatus.QUALIFIED
        and result.stale_data is not True
    )


# ==================================================================
# IN-MEMORY LIFECYCLE STORE
# ==================================================================


class SetupLifecycleStore:
    """
    In-memory, deterministic setup lifecycle repository.

    The store keeps the LATEST immutable :class:`SetupLifecycle`
    snapshot per ``lifecycle_id`` (matching the Project's
    SubmissionLifecycle "one current snapshot per record" convention),
    plus deterministic lookup indexes. It is deliberately NOT a
    database: 19.6 requires only an in-memory store / repository
    abstraction; 19.8 owns advanced persistence / recovery.

    Deterministic ordering: all enumerations are sorted by
    ``lifecycle_id`` (canonical string order). No unordered iteration.
    """

    def __init__(self) -> None:
        # lifecycle_id -> latest immutable snapshot.
        self._lifecycles: dict[str, SetupLifecycle] = {}

    # ------------------------------------------------------------
    # WRITE (deterministic replace-by-id)
    # ------------------------------------------------------------

    def save(self, lifecycle: SetupLifecycle) -> None:
        """Persist the LATEST snapshot of a lifecycle (replace-by-id).

        A lifecycle id is the deterministically-computed instance id —
        the store NEVER invents ids and NEVER rewrites an older snapshot
        for a NEWER one in reverse order (the engine enforces strictly
        chronological advancement).
        """

        if not isinstance(lifecycle, SetupLifecycle):
            raise TypeError("lifecycle must be a SetupLifecycle.")
        self._lifecycles[lifecycle.lifecycle_id] = lifecycle

    def delete(self, lifecycle_id: str) -> bool:
        """Remove a lifecycle from the store (returns whether it existed).

        Provided for store symmetry; the engine never deletes an active
        lifecycle during normal processing.
        """

        if self._lifecycles.pop(lifecycle_id, None) is not None:
            return True
        return False

    def reset(self) -> None:
        """Clear the store entirely (used by tests / fresh runs only)."""
        self._lifecycles.clear()

    # ------------------------------------------------------------
    # READ (deterministic)
    # ------------------------------------------------------------

    def load(self, lifecycle_id: str) -> SetupLifecycle | None:
        """The latest snapshot of one lifecycle (``None`` when absent)."""
        return self._lifecycles.get(lifecycle_id)

    def list_lifecycles(self) -> tuple[SetupLifecycle, ...]:
        """All lifecycle snapshots, sorted by lifecycle_id."""
        return tuple(
            self._lifecycles[k]
            for k in sorted(self._lifecycles)
        )

    def active_lifecycles(self) -> tuple[SetupLifecycle, ...]:
        """Non-terminal lifecycle snapshots, sorted by lifecycle_id."""
        return tuple(
            lc for lc in self.list_lifecycles() if lc.is_active
        )

    def terminal_lifecycles(self) -> tuple[SetupLifecycle, ...]:
        """Terminal lifecycle snapshots, sorted by lifecycle_id."""
        return tuple(
            lc for lc in self.list_lifecycles() if lc.is_terminal
        )

    def lifecycles_for_setup(self, setup_id: str) -> tuple[SetupLifecycle, ...]:
        """ALL instances of one setup id (terminal and active), sorted."""
        return tuple(
            lc
            for lc in self.list_lifecycles()
            if lc.setup_id == setup_id
        )

    def active_for_setup(self, setup_id: str) -> SetupLifecycle | None:
        """The SINGLE active lifecycle of a setup id.

        Invariant: at most ONE active lifecycle may exist per setup id
        (the engine enforces this by creating a new instance only when
        none is active). ``None`` when none active.
        """

        active = [lc for lc in self.active_lifecycles() if lc.setup_id == setup_id]
        if not active:
            return None
        # Deterministic: exactly one is expected; canonical order is
        # lifecycle_id ascending.
        return active[0]

    def lifecycles_for_instrument(
        self,
        instrument: str,
    ) -> tuple[SetupLifecycle, ...]:
        """All lifecycle snapshots for one instrument, sorted."""
        canon = _canonical_name(instrument)
        return tuple(
            lc for lc in self.list_lifecycles() if lc.instrument == canon
        )

    def active_for_instrument(
        self,
        instrument: str,
        primary_timeframe: str | None = None,
    ) -> tuple[SetupLifecycle, ...]:
        """Active lifecycles for one instrument (optionally filtered by
        primary timeframe), sorted by lifecycle_id."""

        canon = _canonical_name(instrument)
        return tuple(
            lc
            for lc in self.active_lifecycles()
            if lc.instrument == canon
            and (primary_timeframe is None
                 or lc.primary_timeframe == primary_timeframe)
        )

    def lifecycles_for_timeframe(
        self,
        primary_timeframe: str,
    ) -> tuple[SetupLifecycle, ...]:
        """All lifecycle snapshots for one primary timeframe, sorted."""
        return tuple(
            lc for lc in self.list_lifecycles()
            if lc.primary_timeframe == primary_timeframe
        )

    @property
    def active_count(self) -> int:
        return len(self.active_lifecycles())

    @property
    def terminal_count(self) -> int:
        return len(self.terminal_lifecycles())

    @property
    def total_count(self) -> int:
        return len(self._lifecycles)


# ==================================================================
# LIFECYCLE ENGINE
# ==================================================================


class SetupLifecycleEngine:
    """
    Deterministic setup lifecycle engine (Checkpoint 19.6).

    Public API:

        process(result, scan_cycle_id, timestamp=None, ...)
            -> SetupLifecycleObservationResult
            ONE symbol's 19.5 assessment advanced through its lifecycle.

        process_universe(analysis, scan_cycle_id, timestamp=None, ...)
            -> SetupLifecycleCycleResult
            Universe-wide lifecycle processing (deterministic,
            failure-isolated, per-symbol results).

        create_store(...)  (classmethod)
            Fresh in-memory store builder.

    The engine is stateless EXCEPT for the injected store; identical
    inputs + identical store state produce identical outputs. No
    wall-clock dependence: every observation timestamp is explicit and
    defaulted to the 19.5 reference instant.
    """

    def __init__(
        self,
        config: SetupLifecycleConfig | None = None,
        store: SetupLifecycleStore | None = None,
    ) -> None:
        self.config = config or SetupLifecycleConfig()
        self.store = store or SetupLifecycleStore()

    @classmethod
    def build(cls) -> "SetupLifecycleEngine":
        """Fresh engine + empty in-memory store (deterministic)."""
        return cls(SetupLifecycleConfig(), SetupLifecycleStore())

    # ============================================================
    # PUBLIC API
    # ============================================================

    def process(
        self,
        result: SetupQualityResult,
        scan_cycle_id: str,
        *,
        timestamp: datetime | None = None,
        label: str = "",
        metadata: Sequence[tuple[str, str]] | None = None,
    ) -> SetupLifecycleObservationResult:
        """
        Advance the lifecycle for ONE symbol's 19.5 assessment.

        ``scan_cycle_id`` is the FROZEN 19.3 cycle identity that produced
        this assessment. ``timestamp`` defaults to the assessment's
        reference instant (never wall-clock). Idempotent for the same
        scan cycle + same assessment; genuinely out-of-order
        observations are rejected (``late_rejected=True``).

        Returns the PRIMARY outcome for the symbol (the tracked
        lifecycle's result when a directional candidate was present;
        otherwise the first lifecycle resolution). All lifecycle
        mutations are already applied to the store.
        """

        del label, metadata  # presentation-only; not part of lifecycle logic
        if not isinstance(result, SetupQualityResult):
            raise TypeError("result must be a SetupQualityResult.")
        if not isinstance(scan_cycle_id, str) or not scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must be a non-empty str.")
        observation_timestamp = _require_aware(
            timestamp if timestamp is not None else result.reference_now,
            "timestamp",
        )

        results = self._process_symbol(
            result, scan_cycle_id, observation_timestamp,
        )
        if not results:
            return SetupLifecycleObservationResult(
                instrument=result.instrument,
                observation_timestamp=observation_timestamp,
                scan_cycle_id=scan_cycle_id,
                observation_status=LifecycleObservationStatus.DATA_UNAVAILABLE,
                reason=(
                    "no resolvable setup identity and no tracked "
                    "lifecycle for this symbol; nothing to do."
                ),
            )
        return _primary_result(results)

    def _process_symbol(
        self,
        result: SetupQualityResult,
        scan_cycle_id: str,
        observation_timestamp: datetime,
    ) -> list[SetupLifecycleObservationResult]:
        """Advance every affected lifecycle for ONE symbol.

        Returns the FULL, deterministic list of per-lifecycle outcomes
        for the symbol (used by :meth:`process` for the primary result
        and by :meth:`process_universe` for accurate cycle counts).
        """

        instrument = result.instrument
        timeframe = result.primary_timeframe
        identity = resolve_setup_identity(
            result, self.config.identity_version,
        )

        # ---- Symbol-level active lifecycles on this timeframe -------
        active_symbol = self.store.active_for_instrument(
            instrument, timeframe,
        )

        results: list[SetupLifecycleObservationResult] = []

        if identity is None:
            data_status = (
                LifecycleObservationStatus.DATA_UNAVAILABLE
                if result.status in (
                    SetupQualityStatus.INCOMPLETE,
                    SetupQualityStatus.UNAVAILABLE,
                )
                else LifecycleObservationStatus.ABSENT
            )
            for lc in active_symbol:
                results.append(
                    self._advance_existing(
                        lc, result, scan_cycle_id,
                        observation_timestamp, data_status,
                    ),
                )
            return results

        # ---- A directional candidate is present ---------------------
        # 1. Advance / create the lifecycle of THIS identity (the
        #    symbol's primary outcome this cycle).
        tracked = self.store.active_for_setup(identity.setup_id)
        if tracked is None:
            results.append(
                self._create_lifecycle(
                    identity, result, scan_cycle_id, observation_timestamp,
                ),
            )
        else:
            results.append(
                self._advance_existing(
                    tracked, result, scan_cycle_id,
                    observation_timestamp,
                    LifecycleObservationStatus.OBSERVED,
                ),
            )
        # 2. Cross-invalidate active lifecycles whose directional
        #    context is OPPOSITE (the old setup's premise no longer
        #    holds) — recorded as a secondary outcome for the symbol.
        for lc in active_symbol:
            if lc.identity.direction is opposite_direction(identity.direction):
                results.append(
                    self._advance_existing(
                        lc, result, scan_cycle_id,
                        observation_timestamp,
                        LifecycleObservationStatus.SUPERSEDED,
                    ),
                )
        # 3. Same-direction, DIFFERENT-setup-type active lifecycles are
        #    displaced (absent) — a different analytical setup type is a
        #    new setup, never a silent mutation of the old lifecycle.
        for lc in active_symbol:
            if (
                lc.identity.direction is identity.direction
                and lc.identity.setup_type != identity.setup_type
                and lc.identity.setup_id != identity.setup_id
            ):
                results.append(
                    self._advance_existing(
                        lc, result, scan_cycle_id,
                        observation_timestamp,
                        LifecycleObservationStatus.ABSENT,
                    ),
                )
        results = _dedupe_results(results)
        return results

    def process_universe(
        self,
        analysis: SetupQualityUniverseResult,
        scan_cycle_id: str,
        *,
        timestamp: datetime | None = None,
        label: str = "",
        metadata: Sequence[tuple[str, str]] | None = None,
    ) -> SetupLifecycleCycleResult:
        """
        Universe-wide lifecycle processing.

        ``analysis`` is the FROZEN 19.5
        :class:`SetupQualityUniverseResult` (exactly-one per-symbol
        result, canonical order). One symbol's problem NEVER terminates
        the run: defensive exceptions are captured per-symbol and
        recorded with an explicit reason (never raised, never
        fabricated). Deterministic ordering: results follow the
        analysis' canonical instrument order.
        """

        del label, metadata
        if not isinstance(analysis, SetupQualityUniverseResult):
            raise TypeError("analysis must be a SetupQualityUniverseResult.")
        if not isinstance(scan_cycle_id, str) or not scan_cycle_id.strip():
            raise ValueError("scan_cycle_id must be a non-empty str.")
        reference_now = _require_aware(
            timestamp if timestamp is not None else analysis.reference_now,
            "timestamp",
        )

        results: list[SetupLifecycleObservationResult] = []
        for result in analysis.results:
            try:
                results.extend(
                    self._process_symbol(result, scan_cycle_id, reference_now),
                )
            except Exception as exc:  # defensive failure isolation
                results.append(
                    SetupLifecycleObservationResult(
                        instrument=result.instrument,
                        observation_timestamp=reference_now,
                        scan_cycle_id=scan_cycle_id,
                        reason=(
                            "defensive isolation: lifecycle processing "
                            f"failed for this symbol ({type(exc).__name__}: "
                            f"{exc})"
                        ),
                    ),
                )

        counts = _counts(results, self.store)
        instruments = tuple(
            r.instrument for r in results
        )
        return SetupLifecycleCycleResult(
            cycle_id=lifecycle_cycle_id(
                scan_cycle_id,
                reference_now,
                self.config,
            ),
            scan_cycle_id=scan_cycle_id,
            reference_now=reference_now,
            instruments=instruments,
            results=tuple(results),
            counts=counts,
            active_count=self.store.active_count,
            terminal_count=self.store.terminal_count,
        )

    # ============================================================
    # INTERNALS
    # ============================================================

    def _create_lifecycle(
        self,
        identity: SetupIdentity,
        result: SetupQualityResult,
        scan_cycle_id: str,
        observation_timestamp: datetime,
    ) -> SetupLifecycleObservationResult:
        """First observation: create a NEW lifecycle instance.

        Initial state is DETECTED; the SAME processing call applies the
        CONFIRMED transition when the first observation is already
        qualified (deterministic initial-state semantics).
        """

        prior = self.store.lifecycles_for_setup(identity.setup_id)
        discriminator = len(prior) + 1
        lifecycle_id = build_lifecycle_id(identity.setup_id, discriminator)

        qualified = is_qualified_assessment(result)
        state = SetupLifecycleState.CONFIRMED if qualified else SetupLifecycleState.DETECTED
        observation = self._build_observation(
            lifecycle_id=lifecycle_id,
            identity=identity,
            result=result,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
            observation_status=LifecycleObservationStatus.OBSERVED,
            at_state=state,
            reason=(
                "first observation — setup detected"
                + (" and qualified (confirmed)." if qualified else ".")
            ),
        )
        transitions: list[SetupLifecycleTransition] = []
        if qualified:
            transitions.append(
                self._build_transition(
                    lifecycle_id=lifecycle_id,
                    identity=identity,
                    from_state=SetupLifecycleState.DETECTED,
                    to_state=SetupLifecycleState.CONFIRMED,
                    scan_cycle_id=scan_cycle_id,
                    observation_timestamp=observation_timestamp,
                    reason=(
                        "qualified assessment observed — setup confirmed "
                        "on first observation."
                    ),
                ),
            )
        lifecycle = SetupLifecycle(
            lifecycle_id=lifecycle_id,
            instance_discriminator=discriminator,
            setup_id=identity.setup_id,
            identity=identity,
            state=state,
            created_at=observation_timestamp,
            last_observation_timestamp=observation_timestamp,
            consecutive_missing=0,
            latest_observation_id=observation.observation_id,
            observations=(observation,),
            transitions=tuple(transitions),
            latest_score=result.score,
            latest_classification=(
                result.classification.value
                if result.classification is not None else None
            ),
            latest_status=result.status.value,
            latest_observation_status=LifecycleObservationStatus.OBSERVED,
        )
        self.store.save(lifecycle)
        return SetupLifecycleObservationResult(
            instrument=identity.instrument,
            observation_timestamp=observation_timestamp,
            scan_cycle_id=scan_cycle_id,
            setup_id=identity.setup_id,
            lifecycle_id=lifecycle_id,
            state=state,
            created=True,
            advanced=True,
            observation=observation,
            transition=transitions[0] if transitions else None,
            observation_status=LifecycleObservationStatus.OBSERVED,
            reason=(
                "new setup lifecycle created — "
                f"state [{state.value}]."
            ),
        )

    def _advance_existing(
        self,
        lifecycle: SetupLifecycle,
        result: SetupQualityResult,
        scan_cycle_id: str,
        observation_timestamp: datetime,
        observation_status: LifecycleObservationStatus,
    ) -> SetupLifecycleObservationResult:
        """Advance an existing lifecycle with one observation.

        Deterministic semantics:

        * OUT-OF-ORDER (strictly older than the latest observation and
          not already recorded) -> rejected, no mutation.
        * DUPLICATE (same deterministic observation id already recorded)
          -> idempotent, no mutation.
        * DATA_UNAVAILABLE -> observation appended, state + counter
          unchanged.
        * ABSENT -> observation appended, counter += 1; EXPIRE when the
          counter reaches ``max_consecutive_missing_observations``.
        * SUPERSEDED -> observation appended; INVALIDATED (terminal).
        * OBSERVED -> observation appended, counter reset; CONFIRM when
          DETECTED and the observation is qualified.
        """

        if lifecycle.is_terminal:
            return SetupLifecycleObservationResult(
                instrument=lifecycle.instrument,
                observation_timestamp=observation_timestamp,
                scan_cycle_id=scan_cycle_id,
                setup_id=lifecycle.setup_id,
                lifecycle_id=lifecycle.lifecycle_id,
                state=lifecycle.state,
                observation_status=observation_status,
                reason=(
                    "terminal lifecycle is not advanced (a reappearance "
                    "starts a NEW lifecycle instance)."
                ),
            )

        # ---- Out-of-order check -----------------------------------
        last_ts = lifecycle.last_observation_timestamp
        update_identity = lifecycle.identity
        if last_ts is not None and observation_timestamp < last_ts:
            return SetupLifecycleObservationResult(
                instrument=lifecycle.instrument,
                observation_timestamp=observation_timestamp,
                scan_cycle_id=scan_cycle_id,
                setup_id=lifecycle.setup_id,
                lifecycle_id=lifecycle.lifecycle_id,
                state=lifecycle.state,
                late_rejected=True,
                observation_status=LifecycleObservationStatus.LATE_REJECTED,
                reason=(
                    "out-of-order observation (timestamp older than the "
                    "lifecycle's last observation) rejected — late "
                    "observations never mutate lifecycle state."
                ),
            )

        observation_id = build_observation_id(
            lifecycle_id=lifecycle.lifecycle_id,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
            observation_status=observation_status,
            fingerprint=_assessment_fingerprint(result),
        )
        if any(o.observation_id == observation_id for o in lifecycle.observations):
            return SetupLifecycleObservationResult(
                instrument=lifecycle.instrument,
                observation_timestamp=observation_timestamp,
                scan_cycle_id=scan_cycle_id,
                setup_id=lifecycle.setup_id,
                lifecycle_id=lifecycle.lifecycle_id,
                state=lifecycle.state,
                duplicate=True,
                observation_status=observation_status,
                reason=(
                    "duplicate observation — the same scan cycle + "
                    "assessment was already recorded (idempotent)."
                ),
            )

        # ---- Compute the arrived-at state --------------------------
        to_state, transition, observation_at_state, reason = self._apply_rule(
            lifecycle,
            result,
            scan_cycle_id,
            observation_timestamp,
            observation_status,
            update_identity,
        )

        observation = self._build_observation(
            lifecycle_id=lifecycle.lifecycle_id,
            identity=update_identity,
            result=result,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
            observation_status=observation_status,
            at_state=observation_at_state,
            reason=reason,
        )

        new_observations = lifecycle.observations + (observation,)
        new_transitions = (
            lifecycle.transitions + (transition,)
            if transition is not None
            else lifecycle.transitions
        )
        consecutive = (
            0
            if observation_status is LifecycleObservationStatus.OBSERVED
            else (
                lifecycle.consecutive_missing + 1
                if observation_status is LifecycleObservationStatus.ABSENT
                else lifecycle.consecutive_missing
            )
        )
        advanced = SetupLifecycle(
            lifecycle_id=lifecycle.lifecycle_id,
            instance_discriminator=lifecycle.instance_discriminator,
            setup_id=lifecycle.setup_id,
            identity=lifecycle.identity,
            state=to_state,
            created_at=lifecycle.created_at,
            last_observation_timestamp=observation_timestamp,
            consecutive_missing=consecutive,
            latest_observation_id=observation.observation_id,
            observations=new_observations,
            transitions=new_transitions,
            latest_score=result.score,
            latest_classification=(
                result.classification.value
                if result.classification is not None else None
            ),
            latest_status=result.status.value,
            latest_observation_status=(
                observation_status
                if observation_status is not LifecycleObservationStatus.SUPERSEDED
                else LifecycleObservationStatus.SUPERSEDED
            ),
        )
        self.store.save(advanced)

        return SetupLifecycleObservationResult(
            instrument=lifecycle.instrument,
            observation_timestamp=observation_timestamp,
            scan_cycle_id=scan_cycle_id,
            setup_id=lifecycle.setup_id,
            lifecycle_id=lifecycle.lifecycle_id,
            state=to_state,
            created=False,
            advanced=True,
            observation=observation,
            transition=transition,
            observation_status=observation_status,
            reason=reason,
        )

    def _apply_rule(
        self,
        lifecycle: SetupLifecycle,
        result: SetupQualityResult,
        scan_cycle_id: str,
        observation_timestamp: datetime,
        observation_status: LifecycleObservationStatus,
        identity: SetupIdentity,
    ) -> tuple[
        SetupLifecycleState,
        SetupLifecycleTransition | None,
        SetupLifecycleState,
        str,
    ]:
        """Deterministic transition-rule application. Returns
        ``(to_state, transition, observation_at_state, reason)``."""

        if observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE:
            return (
                lifecycle.state,
                None,
                lifecycle.state,
                (
                    "data unavailable for this symbol this cycle — "
                    "lifecycle state unchanged (data failure is never "
                    "invalidation)."
                ),
            )

        if observation_status is LifecycleObservationStatus.SUPERSEDED:
            return (
                SetupLifecycleState.INVALIDATED,
                self._build_transition(
                    lifecycle_id=lifecycle.lifecycle_id,
                    identity=lifecycle.identity,
                    from_state=lifecycle.state,
                    to_state=SetupLifecycleState.INVALIDATED,
                    scan_cycle_id=scan_cycle_id,
                    observation_timestamp=observation_timestamp,
                    reason=(
                        "opposing directional setup observed for the "
                        "same instrument/timeframe — the tracked "
                        "setup's directional context no longer holds."
                    ),
                ),
                SetupLifecycleState.INVALIDATED,
                (
                    "superseded by an opposing-direction setup — "
                    "lifecycle invalidated."
                ),
            )

        if observation_status is LifecycleObservationStatus.ABSENT:
            new_missing = lifecycle.consecutive_missing + 1
            if new_missing >= self.config.max_consecutive_missing_observations:
                return (
                    SetupLifecycleState.EXPIRED,
                    self._build_transition(
                        lifecycle_id=lifecycle.lifecycle_id,
                        identity=lifecycle.identity,
                        from_state=lifecycle.state,
                        to_state=SetupLifecycleState.EXPIRED,
                        scan_cycle_id=scan_cycle_id,
                        observation_timestamp=observation_timestamp,
                        reason=(
                            f"setup not observed for "
                            f"{self.config.max_consecutive_missing_observations}"
                            " consecutive clean scan cycles — expired."
                        ),
                    ),
                    SetupLifecycleState.EXPIRED,
                    (
                        f"absent for "
                        f"{self.config.max_consecutive_missing_observations}"
                        " consecutive clean cycles — lifecycle expired."
                    ),
                )
            return (
                lifecycle.state,
                None,
                lifecycle.state,
                (
                    f"clean non-observation "
                    f"({new_missing}/{self.config.max_consecutive_missing_observations}"
                    ") — state unchanged, waiting for reappearance."
                ),
            )

        # OBSERVED: identity present.
        if lifecycle.state is SetupLifecycleState.CONFIRMED:
            return (
                lifecycle.state,
                None,
                lifecycle.state,
                "setup observed and confirmed — quality changes recorded "
                "as assessment changes, state unchanged.",
            )
        # DETECTED: confirm when the observation is qualified.
        qualified = is_qualified_assessment(result)
        if qualified:
            return (
                SetupLifecycleState.CONFIRMED,
                self._build_transition(
                    lifecycle_id=lifecycle.lifecycle_id,
                    identity=lifecycle.identity,
                    from_state=SetupLifecycleState.DETECTED,
                    to_state=SetupLifecycleState.CONFIRMED,
                    scan_cycle_id=scan_cycle_id,
                    observation_timestamp=observation_timestamp,
                    reason=(
                        "qualified assessment observed — setup confirmed."
                    ),
                ),
                SetupLifecycleState.CONFIRMED,
                "qualified assessment observed — setup confirmed.",
            )
        return (
            lifecycle.state,
            None,
            lifecycle.state,
            "setup observed but not yet qualified — state unchanged.",
        )

    # ------------------------------------------------------------
    # BUILDERS
    # ------------------------------------------------------------

    def _build_observation(
        self,
        lifecycle_id: str,
        identity: SetupIdentity,
        result: SetupQualityResult,
        scan_cycle_id: str,
        observation_timestamp: datetime,
        observation_status: LifecycleObservationStatus,
        at_state: SetupLifecycleState,
        reason: str,
    ) -> SetupLifecycleObservation:
        observation_id = build_observation_id(
            lifecycle_id=lifecycle_id,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
            observation_status=observation_status,
            fingerprint=_assessment_fingerprint(result),
        )
        return SetupLifecycleObservation(
            observation_id=observation_id,
            lifecycle_id=lifecycle_id,
            setup_id=identity.setup_id,
            instrument=identity.instrument,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
            observation_status=observation_status,
            at_state=at_state,
            quality_status=result.status.value,
            quality_classification=(
                result.classification.value
                if result.classification is not None else None
            ),
            quality_score=result.score,
            setup_type=identity.setup_type,
            direction=identity.direction,
            assessment=result,
            reason=reason,
        )

    def _build_transition(
        self,
        lifecycle_id: str,
        identity: SetupIdentity,
        from_state: SetupLifecycleState,
        to_state: SetupLifecycleState,
        scan_cycle_id: str,
        observation_timestamp: datetime,
        reason: str,
    ) -> SetupLifecycleTransition:
        transition_id = build_transition_id(
            lifecycle_id=lifecycle_id,
            from_state=from_state,
            to_state=to_state,
            scan_cycle_id=scan_cycle_id,
            observation_timestamp=observation_timestamp,
        )
        return SetupLifecycleTransition(
            transition_id=transition_id,
            lifecycle_id=lifecycle_id,
            setup_id=identity.setup_id,
            from_state=from_state,
            to_state=to_state,
            observation_timestamp=observation_timestamp,
            scan_cycle_id=scan_cycle_id,
            reason=reason,
        )


# ==================================================================
# DETERMINISTIC ID HELPERS
# ==================================================================


def build_lifecycle_id(setup_id: str, instance_discriminator: int) -> str:
    """Deterministic lifecycle-instance id."""

    if not isinstance(instance_discriminator, int) or isinstance(
        instance_discriminator, bool,
    ):
        raise TypeError("instance_discriminator must be an int, not bool.")
    if instance_discriminator < 1:
        raise ValueError("instance_discriminator must be >= 1.")
    payload = f"{setup_id}|instance:{instance_discriminator}"
    return _sha256_prefix(payload, LIFECYCLE_ID_PREFIX)


def build_observation_id(
    lifecycle_id: str,
    scan_cycle_id: str,
    observation_timestamp: datetime,
    observation_status: LifecycleObservationStatus,
    fingerprint: str,
) -> str:
    """Deterministic observation id (idempotency basis)."""

    if observation_timestamp.tzinfo is None:
        raise ValueError("observation_timestamp must be timezone-aware.")
    payload = "|".join(
        (
            lifecycle_id,
            scan_cycle_id,
            observation_timestamp.astimezone(UTC).isoformat(),
            observation_status.value,
            fingerprint,
        ),
    )
    return _sha256_prefix(payload, LIFECYCLE_OBSERVATION_ID_PREFIX)


def build_transition_id(
    lifecycle_id: str,
    from_state: SetupLifecycleState,
    to_state: SetupLifecycleState,
    scan_cycle_id: str,
    observation_timestamp: datetime,
) -> str:
    """Deterministic transition id."""

    if observation_timestamp.tzinfo is None:
        raise ValueError("observation_timestamp must be timezone-aware.")
    payload = "|".join(
        (
            lifecycle_id,
            from_state.value,
            to_state.value,
            scan_cycle_id,
            observation_timestamp.astimezone(UTC).isoformat(),
        ),
    )
    return _sha256_prefix(payload, LIFECYCLE_TRANSITION_ID_PREFIX)


def lifecycle_cycle_id(
    scan_cycle_id: str,
    reference_now: datetime,
    config: SetupLifecycleConfig,
) -> str:
    """Deterministic lifecycle-processing-cycle identity."""

    if reference_now.tzinfo is None:
        raise ValueError("reference_now must be timezone-aware.")
    payload = "|".join(
        (
            scan_cycle_id,
            reference_now.astimezone(UTC).isoformat(),
            transition_rule_version(config),
        ),
    )
    return _sha256_prefix(payload, LIFECYCLE_CYCLE_ID_PREFIX)


# ==================================================================
# PURE HELPERS
# ==================================================================


def _dedupe_results(
    results: list[SetupLifecycleObservationResult],
) -> list[SetupLifecycleObservationResult]:
    """Deterministic de-duplication by (instrument, lifecycle_id) — a
    symbol's processing NEVER produces more than one result per tracked
    lifecycle."""

    seen: set[tuple[str, str | None]] = set()
    out: list[SetupLifecycleObservationResult] = []
    for r in results:
        key = (r.instrument, r.lifecycle_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _primary_result(
    results: list[SetupLifecycleObservationResult],
) -> SetupLifecycleObservationResult:
    """Deterministic PRIMARY outcome for a symbol.

    The primary result is the TRACKED-lifecycle result (a directional
    candidate present, or the first lifecycle resolution), matching the
    per-lifecycle results order produced by ``_process_symbol``.
    """

    if not results:
        raise ValueError("_primary_result requires at least one result.")
    return results[0]


def _counts(
    results: Sequence[SetupLifecycleObservationResult],
    store: SetupLifecycleStore,
) -> SetupLifecycleCounts:
    """Deterministic aggregate counts over a processing cycle."""

    created = advanced = duplicate = late = 0
    observed = absent = data_unavailable = superseded = 0
    detected = confirmed = invalidated = expired = 0
    for r in results:
        if r.created:
            created += 1
        if r.advanced:
            advanced += 1
        if r.duplicate:
            duplicate += 1
        if r.late_rejected:
            late += 1
        st = r.observation_status
        if st is LifecycleObservationStatus.OBSERVED:
            observed += 1
        elif st is LifecycleObservationStatus.ABSENT:
            absent += 1
        elif st is LifecycleObservationStatus.DATA_UNAVAILABLE:
            data_unavailable += 1
        elif st is LifecycleObservationStatus.SUPERSEDED:
            superseded += 1
        if r.state is SetupLifecycleState.DETECTED:
            detected += 1
        elif r.state is SetupLifecycleState.CONFIRMED:
            confirmed += 1
        elif r.state is SetupLifecycleState.INVALIDATED:
            invalidated += 1
        elif r.state is SetupLifecycleState.EXPIRED:
            expired += 1
    return SetupLifecycleCounts(
        created=created,
        advanced=advanced,
        duplicate=duplicate,
        late_rejected=late,
        observed=observed,
        absent=absent,
        data_unavailable=data_unavailable,
        superseded=superseded,
        detected=detected,
        confirmed=confirmed,
        invalidated=invalidated,
        expired=expired,
        active=store.active_count,
        terminal=store.terminal_count,
    )


#: Id prefix re-export for the CLI / JSON surface.
SETUP_LIFECYCLE_ID_PREFIXES = (
    SETUP_ID_PREFIX,
    LIFECYCLE_ID_PREFIX,
    LIFECYCLE_OBSERVATION_ID_PREFIX,
    LIFECYCLE_TRANSITION_ID_PREFIX,
    LIFECYCLE_CYCLE_ID_PREFIX,
)


__all__ = [
    "SetupLifecycleEngine",
    "SetupLifecycleStore",
    "build_lifecycle_id",
    "build_observation_id",
    "build_transition_id",
    "is_qualified_assessment",
    "lifecycle_cycle_id",
    "resolve_setup_identity",
]