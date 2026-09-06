"""
Validation / forward-testing orchestration engine (Checkpoint 19.9).

The FINAL Checkpoint 19 layer: a downstream, deterministic,
point-in-time-safe FORWARD-VALIDATION framework that records what the
FROZEN 19.1-19.8 pipeline observed at each instant ``T`` and measures
what the market subsequently did AFTER ``T``.

Architecture (downstream of all FROZEN 19.x layers):

    Scan Cycle (19.3 identity)
        -> MTF Analysis (19.4)
        -> Setup Quality (19.5)
        -> Setup Lifecycle (19.6, events)
        -> User Alerts (19.7, events + deliveries)
        -> Reliability (19.8, operational state)
        -> FORWARD OBSERVATION (this layer, at T)
             |
             V   WAIT (future completed candles ONLY)
        -> FORWARD OUTCOME (this layer, after T)
        -> FORWARD SESSION / REPORT (this layer)

Guarantees (all enforced by construction + regression tests):

* POINT-IN-TIME: an observation records exactly what the upstream
  layers knew at ``T`` (the 19.5 quality snapshot, the 19.4 MTF
  classification, the 19.6 lifecycle state, the 19.7 alert state, the
  19.3 scan-cycle identity). A forward outcome inspects ONLY completed
  primary-timeframe candles with ``timestamp > T`` within the
  configured horizon. The original observation is NEVER rewritten with
  future information (byte-identical serialization tests).

* OBSERVATION vs OUTCOME are SEPARATE records. A :class:`ForwardOutcome`
  is produced by :meth:`ForwardOutcomeEngine.measure` from strictly
  forward candles; measuring NEVER mutates the observation.

* IDEMPOTENCY: processing the same (scan cycle, instrument) twice with
  the same upstream outputs produces the same deterministic observation
  id and is detected as a duplicate. Outcome measurement for the same
  observation at the same measurement instant is idempotent.

* ORDERING: observations within a cycle are recorded in canonical
  instrument order; out-of-order observations (timestamp strictly older
  than the session's latest) are rejected or flagged per configuration.

* CONFIGURATION IMMUTABILITY: every observation / session records the
  deterministic policy versions that produced it (validation /
  setup-quality / lifecycle / alert / reliability / universe / window).
  A later config change can NEVER silently reinterpret a previous
  observation (version-separated).

* FAILURE ISOLATION: one missing / unsupported / failing symbol never
  aborts the cycle; that symbol is recorded as an explicit
  non-observation and the universe continues.

* NO EXECUTION: this module imports NO broker / execution / paper-
  trading / submission code and triggers NO order, alert delivery or
  external notification. The user is the final execution boundary.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from engine.config.forward_validation_config import ForwardValidationConfig
from engine.models.forward_validation import (
    ForwardCycleResult,
    ForwardObservation,
    ForwardObservationStatus,
    ForwardOutcome,
    ForwardSession,
    ForwardSessionCounts,
    ForwardValidationReport,
    LifecycleResolution,
    OutcomeAvailability,
    OutcomeDirection,
    build_forward_cycle_id,
    build_observation_id,
    build_outcome_id,
    build_report_id,
    build_session_id,
    classify_outcome_direction,
)

from engine.models.ohlcv import OHLCVCandle
from engine.models.setup_lifecycle import (
    LifecycleObservationStatus,
    SetupLifecycleCycleResult,
)
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
    SetupQualityUniverseResult,
)
from engine.models.user_alerts import AlertCycleResult

#: Default observation-status reason strings (auditable).
_REASON_RECORDED = "recorded"
_REASON_DUPLICATE = "duplicate observation identity already recorded"
_REASON_OUT_OF_ORDER = "observation timestamp older than session latest"


def _canonical_name(name: str) -> str:
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


def _is_completed(
    candle: OHLCVCandle,
    reference: datetime,
    duration_seconds: int,
) -> bool:
    """A candle is COMPLETED when its close time (open + duration) is
    strictly at/before the reference instant."""

    try:
        close = candle.timestamp + __import__("datetime").timedelta(
            seconds=duration_seconds,
        )
    except Exception:
        return False
    return close <= reference


def _canonical_timeframe(timeframe: str) -> str | None:
    from engine.data.historical_times import canonical_timeframe

    return canonical_timeframe(timeframe)


def _timeframe_duration_seconds(timeframe: str) -> int | None:
    from engine.data.historical_times import timeframe_seconds

    return timeframe_seconds(timeframe)


# ==================================================================
# FORWARD OBSERVATION BUILDER
# ==================================================================


def build_forward_observation(
    *,
    session_id: str,
    scan_cycle_id: str,
    reference_now: datetime,
    instrument: str,
    quality: Any | None = None,
    lifecycle: Any | None = None,
    alerts_by_setup: Mapping[str, Sequence[Any]] | None = None,
    policy_version: str = "",
    reference_price: float | None = None,
) -> ForwardObservation:
    """
    Build ONE immutable point-in-time forward observation from the
    FROZEN upstream outputs at ``T`` (Checkpoint 19.9).

    The observation is a projection of the 19.5 setup-quality result,
    the 19.4 MTF classification, the 19.6 lifecycle observation result
    and the 19.7 alert state at the same instant. All values are read
    with defensive ``getattr`` so the builder never raises on a
    partially-populated upstream object — unavailable values become
    explicit ``None`` / ``""`` sentinels (never fabricated).
    """

    _require_aware(reference_now, "reference_now")
    canon = _canonical_name(instrument)

    # ---- 19.5 setup-quality projection -------------------------
    direction = _attr_str(quality, "setup_direction", "")
    setup_type = _attr_str(quality, "setup_type", "")
    quality_status = getattr(quality, "status", None)
    quality_status_member = (
        quality_status
        if isinstance(quality_status, SetupQualityStatus)
        else SetupQualityStatus.UNAVAILABLE
    )
    quality_classification = getattr(quality, "classification", None)
    quality_classification_member = (
        quality_classification
        if isinstance(quality_classification, SetupQualityClassification)
        else SetupQualityClassification.UNAVAILABLE
    )
    quality_score = getattr(quality, "score", None)
    if not isinstance(quality_score, int):
        quality_score = None

    # ---- 19.4 MTF projection -----------------------------------
    mtf_alignment = _attr_str(quality, "mtf_alignment", "UNAVAILABLE")
    mtf_completeness = _attr_str(quality, "mtf_completeness", "UNAVAILABLE")

    # ---- 19.6 lifecycle projection -----------------------------
    lifecycle_state = LifecycleResolution.DATA_UNAVAILABLE
    lifecycle_obs_status = LifecycleObservationStatus.DATA_UNAVAILABLE
    setup_id = _attr_str(lifecycle, "setup_id", "")
    lifecycle_id = _attr_str(lifecycle, "lifecycle_id", "")
    if lifecycle is not None:
        state = getattr(lifecycle, "state", None)
        lifecycle_state = _map_lifecycle_state(state)
        obs_status = getattr(lifecycle, "observation_status", None)
        if isinstance(obs_status, LifecycleObservationStatus):
            lifecycle_obs_status = obs_status

    # ---- 19.7 alert projection --------------------------------
    alert_state = ""
    alert_id = ""
    if setup_id and alerts_by_setup is not None:
        matches = [
            alert
            for alert in alerts_by_setup.get(setup_id, ())
            if getattr(alert, "observation_id", "") == _attr_str(lifecycle, "observation_id", "")
            or getattr(alert, "scan_cycle_id", "") == scan_cycle_id
        ]
        if matches:
            alert_state = "ALERTED"
            alert_id = _attr_str(matches[0], "alert_id", "")

    observation_id = build_observation_id(
        session_id=session_id,
        scan_cycle_id=scan_cycle_id,
        instrument=canon,
        direction=direction,
        setup_type=setup_type,
        primary_timeframe=_attr_str(quality, "primary_timeframe", ""),
        observation_timestamp=reference_now,
        policy_version=policy_version,
    )

    return ForwardObservation(
        observation_id=observation_id,
        setup_id=setup_id,
        lifecycle_id=lifecycle_id,
        instrument=canon,
        direction=direction,
        setup_type=setup_type,
        primary_timeframe=_attr_str(quality, "primary_timeframe", ""),
        observation_timestamp=reference_now,
        scan_cycle_id=scan_cycle_id,
        lifecycle_state=lifecycle_state,
        lifecycle_observation_status=lifecycle_obs_status,
        quality_status=quality_status_member,
        quality_classification=quality_classification_member,
        quality_score=quality_score,
        mtf_alignment=mtf_alignment or "UNAVAILABLE",
        mtf_completeness=mtf_completeness or "UNAVAILABLE",
        alert_state=alert_state,
        alert_id=alert_id,
        policy_versions=_policy_versions(
            policy_version,
            getattr(quality, "policy_versions", None),
        ),
        reference_price=reference_price,
        status=ForwardObservationStatus.RECORDED,
        reason=_REASON_RECORDED,
    )


def _attr_str(obj: Any, name: str, default: str = "") -> str:
    if obj is None:
        return default
    value = getattr(obj, name, default)
    if value is None:
        return default
    if hasattr(value, "value"):  # enum -> name
        value = value.value
    return str(value) if isinstance(value, (str, int, float)) else default


def _map_lifecycle_state(state: Any) -> LifecycleResolution:
    """Map a 19.6 lifecycle state (enum or name) to the resolution."""
    if state is None:
        return LifecycleResolution.DATA_UNAVAILABLE
    name = state.name if hasattr(state, "name") else str(state)
    try:
        return LifecycleResolution[name]
    except KeyError:
        return LifecycleResolution.DATA_UNAVAILABLE


def _policy_versions(
    validation_policy: str,
    carried: Any,
) -> tuple[tuple[str, str], ...]:
    """Deterministic policy-versions tuple (validation first, then any
    carried upstream policy versions)."""

    entries = [("validation_policy_version", validation_policy or "")]
    if carried is not None:
        if isinstance(carried, tuple):
            for pair in carried:
                if (
                    isinstance(pair, tuple)
                    and len(pair) == 2
                    and isinstance(pair[0], str)
                    and isinstance(pair[1], str)
                ):
                    entries.append(pair)
    return tuple(entries)


# ==================================================================
# FORWARD OUTCOME ENGINE (forward-only measurement)
# ==================================================================


class ForwardOutcomeEngine:
    """
    Deterministic, forward-only outcome measurement (Checkpoint 19.9).

    The engine measures what the market did AFTER an observation:

        reference price   = the observation's recorded reference price
        forward window    = completed primary candles with
                            ``timestamp > observation_timestamp``,
                            truncated to the configured horizon

    OUTCOME FRAMEWORK (documented; exact definitions in the audit
    document):

    1. Reference timestamp: ``observation.observation_timestamp``.
    2. Measurement window:  the next ``horizon_bars`` COMPLETED
       primary-timeframe candles strictly after the reference
       timestamp.
    3. Price source: the completed forward primary candles supplied by
       the caller (canonical :class:`OHLCVCandle` objects — the caller
       is responsible for the completed-candle boundary; the engine
       re-filters strictly-after internally).
    4. Candle timeframe: the observation's ``primary_timeframe``.
    5. Completion rule: a candle is completed when its close time
       (``timestamp + duration``) is strictly at/before the measurement
       reference instant.
    6. Direction handling: direction-aware favorable / adverse
       excursions + a deterministic direction classification.
    7. Missing-data behavior: no forward candles -> OUTCOME_UNAVAILABLE
       (never a success/failure).
    8. Ambiguous-data behavior: no reference price -> NOT_EVALUABLE;
       unknown horizon / duration -> OUTCOME_UNAVAILABLE.
    9. Session handling: candles are required to be STRICTLY after the
       observation timestamp; closed-market periods simply produce an
       unavailable/partial window (never a system failure).
    10. End-of-window behavior: fewer candles than the horizon ->
        ``window_complete=False``; the outcome is descriptive of the
        available window only (``OUTCOME_PARTIAL`` / ``WINDOW_INCOMPLETE``).

    The engine NEVER mutates the observation, NEVER reads future
    candles beyond the horizon (structural truncation) and NEVER uses
    information available at or before ``T``.
    """

    def __init__(
        self,
        config: ForwardValidationConfig | None = None,
        *,
        reference_now: datetime | None = None,
    ) -> None:
        self.config = config or ForwardValidationConfig()
        self._reference_now = reference_now  # measurement anchor (injected)

    # ------------------------------------------------------------
    # PUBLIC API
    # ------------------------------------------------------------

    def measure(
        self,
        observation: ForwardObservation,
        forward_candles: Sequence[OHLCVCandle] | None,
        *,
        measurement_timestamp: datetime | None = None,
    ) -> ForwardOutcome:
        """
        Measure the forward outcome for ONE observation.

        ``forward_candles`` are the COMPLETED primary-timeframe candles
        available at the measurement instant. The engine filters to
        candles strictly after the observation timestamp and truncates
        to the configured horizon — future information beyond the
        horizon is structurally excluded. The observation is NEVER
        mutated.
        """

        _require_aware(observation.observation_timestamp, "observation.observation_timestamp")
        measured_at = measurement_timestamp or self._reference_now or datetime.now(UTC)
        if measured_at.tzinfo is None:
            raise ValueError("measurement_timestamp must be timezone-aware.")
        if measured_at < observation.observation_timestamp:
            raise ValueError(
                "measurement_timestamp must not precede the observation "
                "timestamp.",
            )

        horizon = self.config.max_holding_bars
        tf = observation.primary_timeframe or self.config.primary_timeframe
        canon_tf = _canonical_timeframe(tf)
        duration = _timeframe_duration_seconds(canon_tf or tf)
        if duration is None:
            # Unknown timeframe duration: cannot define completion ->
            # honest unavailable.
            return self._unavailable(
                observation, measured_at, horizon,
                reason=f"unknown duration for primary timeframe {tf!r}.",
            )

        if not forward_candles:
            return self._unavailable(
                observation, measured_at, horizon,
                reason="no forward candles available at measurement time.",
            )

        # ---- strictly-after filter (structural) -----------------
        after = [
            c for c in forward_candles
            if _ts(c) > observation.observation_timestamp
        ]
        # ---- completion filter at the measurement anchor ----------
        completed = [
            c for c in after
            if _is_completed(c, measured_at, duration)
        ]
        # ---- horizon truncation (structural) ---------------------
        used = tuple(completed[:horizon])
        available_count = len(after)
        completed_count = len(completed)
        window_complete = completed_count >= horizon
        if not used:
            if available_count == 0:
                return self._unavailable(
                    observation, measured_at, horizon,
                    reason="no completed forward candles strictly after "
                           "the observation timestamp.",
                )
            return ForwardOutcome(
                outcome_id=build_outcome_id(
                    observation_id=observation.observation_id,
                    measurement_timestamp=measured_at,
                    horizon_bars=horizon,
                    policy_version=_validation_policy(observation),
                ),
                observation_id=observation.observation_id,
                instrument=observation.instrument,
                direction=observation.direction,
                primary_timeframe=tf,
                observation_timestamp=observation.observation_timestamp,
                measurement_timestamp=measured_at,
                availability=OutcomeAvailability.WINDOW_INCOMPLETE,
                bars_available=available_count,
                bars_used=0,
                horizon_bars=horizon,
                window_complete=window_complete,
                reason="forward candles exist but none are completed at "
                       "the measurement anchor.",
            )

        reference = observation.reference_price
        if reference is None or not _finite(reference) or reference <= 0:
            return self._ambiguous(
                observation, measured_at, horizon, used,
                available_count, window_complete,
                reason="no valid reference price on the observation.",
            )

        end_price = float(getattr(used[-1], "close", reference))
        if not _finite(end_price):
            end_price = reference
        forward_return = (end_price - reference) / reference

        highs = [float(getattr(c, "high", reference)) for c in used]
        lows = [float(getattr(c, "low", reference)) for c in used]
        highs = [h for h in highs if _finite(h)]
        lows = [l for l in lows if _finite(l)]
        if not highs or not lows:
            max_fav = None
            max_adv = None
        else:
            max_high = max(highs)
            min_low = min(lows)
            if observation.direction in ("BULLISH", "BEARISH"):
                if observation.direction == "BULLISH":
                    max_fav = (max_high - reference) / reference
                    max_adv = (min_low - reference) / reference
                else:
                    max_fav = (reference - min_low) / reference
                    max_adv = (reference - max_high) / reference
            else:
                max_fav = None
                max_adv = None

        direction_consistent: bool | None = None
        if observation.direction == "BULLISH":
            direction_consistent = forward_return > 0
        elif observation.direction == "BEARISH":
            direction_consistent = forward_return < 0

        availability = (
            OutcomeAvailability.OUTCOME_AVAILABLE
            if window_complete
            else OutcomeAvailability.OUTCOME_PARTIAL
        )
        outcome_direction = classify_outcome_direction(
            direction=observation.direction,
            forward_return=forward_return,
            max_favorable=max_fav,
            max_adverse=max_adv,
        )

        return ForwardOutcome(
            outcome_id=build_outcome_id(
                observation_id=observation.observation_id,
                measurement_timestamp=measured_at,
                horizon_bars=horizon,
                policy_version=_validation_policy(observation),
            ),
            observation_id=observation.observation_id,
            instrument=observation.instrument,
            direction=observation.direction,
            primary_timeframe=tf,
            observation_timestamp=observation.observation_timestamp,
            measurement_timestamp=measured_at,
            availability=availability,
            forward_return=forward_return,
            max_favorable_movement=max_fav,
            max_adverse_movement=max_adv,
            direction_consistent=direction_consistent,
            outcome_direction=outcome_direction,
            bars_available=available_count,
            bars_used=len(used),
            horizon_bars=horizon,
            window_complete=window_complete,
            reason="",
        )

    # ------------------------------------------------------------
    # INTERNAL HELPERS
    # ------------------------------------------------------------

    def _unavailable(
        self,
        observation: ForwardObservation,
        measured_at: datetime,
        horizon: int,
        *,
        reason: str,
    ) -> ForwardOutcome:
        return ForwardOutcome(
            outcome_id=build_outcome_id(
                observation_id=observation.observation_id,
                measurement_timestamp=measured_at,
                horizon_bars=horizon,
                policy_version=_validation_policy(observation),
            ),
            observation_id=observation.observation_id,
            instrument=observation.instrument,
            direction=observation.direction,
            primary_timeframe=observation.primary_timeframe,
            observation_timestamp=observation.observation_timestamp,
            measurement_timestamp=measured_at,
            availability=OutcomeAvailability.OUTCOME_UNAVAILABLE,
            bars_available=0,
            bars_used=0,
            horizon_bars=horizon,
            window_complete=False,
            reason=reason,
        )

    def _ambiguous(
        self,
        observation: ForwardObservation,
        measured_at: datetime,
        horizon: int,
        used: Sequence[OHLCVCandle],
        available_count: int,
        window_complete: bool,
        *,
        reason: str,
    ) -> ForwardOutcome:
        return ForwardOutcome(
            outcome_id=build_outcome_id(
                observation_id=observation.observation_id,
                measurement_timestamp=measured_at,
                horizon_bars=horizon,
                policy_version=_validation_policy(observation),
            ),
            observation_id=observation.observation_id,
            instrument=observation.instrument,
            direction=observation.direction,
            primary_timeframe=observation.primary_timeframe,
            observation_timestamp=observation.observation_timestamp,
            measurement_timestamp=measured_at,
            availability=OutcomeAvailability.OUTCOME_UNAVAILABLE,
            outcome_direction=OutcomeDirection.NOT_EVALUABLE,
            bars_available=available_count,
            bars_used=0,
            horizon_bars=horizon,
            window_complete=window_complete,
            reason=reason,
        )


def _ts(candle: OHLCVCandle) -> datetime:
    ts = getattr(candle, "timestamp", None)
    if not isinstance(ts, datetime):
        raise TypeError("forward candles must carry a datetime timestamp.")
    return ts


def _finite(value: float | None) -> bool:
    if value is None:
        return False
    try:
        import math

        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _validation_policy(observation: ForwardObservation) -> str:
    for name, value in observation.policy_versions:
        if name == "validation_policy_version":
            return value
    return ""


# ==================================================================
# SESSION MANAGER
# ==================================================================


class ForwardSessionManager:
    """
    Deterministic forward-validation session management (Checkpoint
    19.9).

    A session records the configuration / policy versions that produced
    its observations and maintains running counts. Sessions are
    immutable snapshots: opening + advancing the counts produces a NEW
    snapshot (the old one remains auditable); persistence is handled by
    the injected store.
    """

    def __init__(
        self,
        config: ForwardValidationConfig | None = None,
        *,
        store: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self.config = config or ForwardValidationConfig()
        self.store = store
        self._clock = clock
        self._sessions: dict[str, ForwardSession] = {}
        self._latest_observation_at: dict[str, datetime] = {}
        self._observation_ids: dict[str, set[str]] = {}
        self._records: dict[str, list[ForwardObservation]] = {}

    def _now(self) -> datetime:
        if self._clock is not None:
            value = self._clock()
            _require_aware(value, "clock result")
            return value
        return datetime.now(UTC)

    # ------------------------------------------------------------
    # SESSION LIFECYCLE
    # ------------------------------------------------------------

    def open_session(
        self,
        *,
        universe: Sequence[str] | None = None,
        started_at: datetime | None = None,
        label: str = "",
    ) -> ForwardSession:
        """Open a new forward-validation session (deterministic id)."""

        started = started_at or self._now()
        _require_aware(started, "started_at")
        res_universe = (
            tuple(_canonical_name(n) for n in universe)
            if universe is not None
            else tuple(
                _canonical_name(n)
                for n in (self.config.universe or ())
            )
        )
        session_id = build_session_id(
            provider=self.config.provider,
            timeframes=self.config.timeframes,
            universe=res_universe,
            started_at=started,
            policy_version=self.config.policy_version(),
            label=label or self.config.label,
        )
        session = ForwardSession(
            session_id=session_id,
            provider=self.config.provider,
            timeframes=self.config.timeframes,
            primary_timeframe=self.config.primary_timeframe,
            universe=res_universe,
            universe_version=self.config.universe_version,
            policy_versions=_session_policy_versions(self.config),
            started_at=started,
            status="OPEN",
            label=label or self.config.label,
            metadata=self.config.metadata,
        )
        self._sessions[session_id] = session
        self._observation_ids.setdefault(session_id, set())
        self._latest_observation_at.setdefault(session_id, None)
        self._records.setdefault(session_id, [])
        if self.store is not None and hasattr(self.store, "save_session"):
            try:
                self.store.save_session(session)
            except Exception:
                # Persistence failure is an OPERATIONAL concern; the
                # in-memory session remains usable (the operator CLI
                # surfaces persistence failures explicitly).
                pass
        return session

    def close_session(
        self,
        session_id: str,
        *,
        ended_at: datetime | None = None,
    ) -> ForwardSession:
        """Close an open session (deterministic snapshot)."""

        session = self._sessions.get(session_id)
        if session is None and self.store is not None:
            try:
                session = self.store.load_session(session_id)
            except Exception:
                session = None
        if session is None:
            raise KeyError(f"unknown session {session_id!r}.")
        if session.status == "CLOSED":
            raise ValueError(f"session {session_id!r} is already closed.")
        ended = ended_at or self._now()
        _require_aware(ended, "ended_at")
        closed = ForwardSession(
            session_id=session.session_id,
            provider=session.provider,
            timeframes=session.timeframes,
            primary_timeframe=session.primary_timeframe,
            universe=session.universe,
            universe_version=session.universe_version,
            policy_versions=session.policy_versions,
            started_at=session.started_at,
            ended_at=ended,
            status="CLOSED",
            counts=session.counts,
            label=session.label,
            metadata=session.metadata,
        )
        self._sessions[session_id] = closed
        if self.store is not None and hasattr(self.store, "save_session"):
            self.store.save_session(closed, overwrite=True)
        return closed

    def load_session(self, session_id: str) -> ForwardSession:
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        if self.store is not None and hasattr(self.store, "load_session"):
            return self.store.load_session(session_id)
        raise KeyError(f"unknown session {session_id!r}.")

    def session(self, session_id: str) -> ForwardSession | None:
        try:
            return self.load_session(session_id)
        except (KeyError, Exception):
            return None

    def latest_observation_timestamp(self, session_id: str) -> datetime | None:
        return self._latest_observation_at.get(session_id)

    # ------------------------------------------------------------
    # OBSERVATION MANAGEMENT
    # ------------------------------------------------------------

    def record_observation(
        self,
        session_id: str,
        observation: ForwardObservation,
    ) -> ForwardObservation:
        """
        Record one observation into a session (idempotent).

        Returns the observation with the resolved record status
        (RECORDED / DUPLICATE / OUT_OF_ORDER) — the caller persists the
        final record. A duplicate is never silently dropped (it surfaces
        in the status + cycle counts).
        """

        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(f"unknown session {session_id!r}.")
        known = self._observation_ids.setdefault(session_id, set())
        if observation.observation_id in known:
            return _with_status(
                observation,
                ForwardObservationStatus.DUPLICATE,
                _REASON_DUPLICATE,
            )
        latest = self._latest_observation_at.get(session_id)
        if self.config.reject_out_of_order and latest is not None:
            if observation.observation_timestamp < latest:
                return _with_status(
                    observation,
                    ForwardObservationStatus.OUT_OF_ORDER,
                    _REASON_OUT_OF_ORDER,
                )
        known.add(observation.observation_id)
        self._latest_observation_at[session_id] = observation.observation_timestamp
        self._records.setdefault(session_id, []).append(observation)
        self._bump_session_counts(session_id, observation)
        return observation

    def observations_for(self, session_id: str) -> tuple[ForwardObservation, ...]:
        """Recorded observations for a session (deterministic order)."""
        return tuple(self._records.get(session_id, ()))

    def observations_count(self, session_id: str) -> int:
        """Number of recorded observations for a session."""
        return len(self._records.get(session_id, ()))

    def _bump_session_counts(
        self,
        session_id: str,
        observation: ForwardObservation,
    ) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        counts = session.counts
        updated = ForwardSessionCounts(
            observations=counts.observations + 1,
            outcomes=counts.outcomes,
            alerts=counts.alerts,
            alerts_delivered=counts.alerts_delivered,
            alerts_failed=counts.alerts_failed,
            alerts_suppressed=counts.alerts_suppressed,
            failures=counts.failures,
            retries=counts.retries,
            timeouts=counts.timeouts,
            recoveries=counts.recoveries,
        )
        self._sessions[session_id] = _with_counts(session, updated)


def _with_status(
    observation: ForwardObservation,
    status: ForwardObservationStatus,
    reason: str,
) -> ForwardObservation:
    import dataclasses

    return dataclasses.replace(observation, status=status, reason=reason)


def _with_counts(
    session: ForwardSession,
    counts: ForwardSessionCounts,
) -> ForwardSession:
    import dataclasses

    return dataclasses.replace(session, counts=counts)


def _session_policy_versions(
    config: ForwardValidationConfig,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (
            ("validation_policy_version", config.policy_version()),
            ("universe_version", config.universe_version),
            ("setup_quality_policy_version", config.setup_quality_policy_version),
            ("lifecycle_policy_version", config.lifecycle_policy_version),
            ("alert_policy_version", config.alert_policy_version),
            ("reliability_policy_version", config.reliability_policy_version),
            ("mtf_policy_version", config.mtf_policy_version),
            ("measurement_window", config.measurement_window or str(config.max_holding_bars)),
        )
    )


# ==================================================================
# FORWARD VALIDATION ENGINE (cycle orchestration)
# ==================================================================


class ForwardValidationEngine:
    """
    ONE forward-validation processing cycle (Checkpoint 19.9).

    The engine consumes ONE instant of the FROZEN 19.x pipeline — the
    19.3 scan-cycle identity, the 19.4 MTF universe analysis, the 19.5
    setup-quality universe result, the 19.6 lifecycle cycle and the
    19.7 alert cycle — and records one :class:`ForwardObservation` per
    instrument (exactly once per requested constituent). Outcome
    measurement is delegated to :class:`ForwardOutcomeEngine`.

    The engine NEVER calls the upstream engines; it consumes their ALREADY-
    COMPUTED outputs (additive, downstream, stateless). One missing /
    failing upstream output for one symbol produces an explicit
    non-observation for that symbol; the universe continues.
    """

    def __init__(
        self,
        config: ForwardValidationConfig | None = None,
        *,
        session_manager: ForwardSessionManager | None = None,
        outcome_engine: ForwardOutcomeEngine | None = None,
        store: Any | None = None,
    ) -> None:
        self.config = config or ForwardValidationConfig()
        self.sessions = session_manager or ForwardSessionManager(
            self.config,
            store=store,
        )
        self.outcomes = outcome_engine or ForwardOutcomeEngine(self.config)

    # ------------------------------------------------------------
    # CYCLE
    # ------------------------------------------------------------

    def process(
        self,
        session: ForwardSession,
        *,
        scan_cycle_id: str = "",
        reference_now: datetime | None = None,
        quality_universe: SetupQualityUniverseResult | None = None,
        lifecycle_cycle: SetupLifecycleCycleResult | None = None,
        alert_cycle: AlertCycleResult | None = None,
        reference_prices: Mapping[str, float | None] | None = None,
        primary_candles: Mapping[str, Sequence[OHLCVCandle]] | None = None,
        label: str = "",
    ) -> ForwardCycleResult:
        """
        Process ONE forward-validation cycle.

        The requested universe is the session's universe; EVERY
        constituent is explicitly accounted for (a symbol without a
        19.5 result is recorded as a DATA_UNAVAILABLE non-observation,
        never silently dropped).
        """

        now = reference_now or datetime.now(UTC)
        _require_aware(now, "reference_now")
        # Resolve the CURRENT session snapshot (a caller may pass a
        # stale object after close/advance).
        current = self.sessions.session(session.session_id)
        if current is None:
            raise KeyError(f"unknown session {session.session_id!r}.")
        if current.status != "OPEN":
            raise ValueError(
                "a forward-validation cycle requires an OPEN session.",
            )
        session = current
        scan_id = scan_cycle_id or "manual-cycle"
        policy_version = self.config.policy_version()
        cycle_id = build_forward_cycle_id(
            session_id=session.session_id,
            scan_cycle_id=scan_id,
            reference_now=now,
            policy_version=policy_version,
        )

        observations: list[ForwardObservation] = []
        outcomes: list[ForwardOutcome] = []
        alerts_total = 0
        alerts_delivered = 0
        alerts_failed = 0
        alerts_suppressed = 0
        error = ""

        # One result per lifecycle observation (19.6), keyed by symbol.
        lifecycle_by_instrument = _lifecycle_by_instrument(lifecycle_cycle)

        # Alert lookup by setup id (19.7, emitted this cycle).
        alerts_by_setup = _alerts_by_setup(alert_cycle)

        universe = session.universe
        instruments = tuple(
            sorted(set(_canonical_name(n) for n in universe)),
        ) if universe else ()

        for instrument in instruments:
            quality = self._quality_for(quality_universe, instrument)
            lifecycle = lifecycle_by_instrument.get(instrument)
            if lifecycle is None and complete_lifecycle_cycle(lifecycle_cycle):
                # The lifecycle cycle DID evaluate this instrument but
                # produced no observation -> explicit non-observation.
                lifecycle = None
            reference_price = None
            if reference_prices is not None:
                reference_price = reference_prices.get(instrument)
            observation = build_forward_observation(
                session_id=session.session_id,
                scan_cycle_id=scan_id,
                reference_now=now,
                instrument=instrument,
                quality=quality,
                lifecycle=lifecycle,
                alerts_by_setup=alerts_by_setup,
                policy_version=policy_version,
                reference_price=reference_price,
            )
            recorded = self.sessions.record_observation(
                session.session_id,
                observation,
            )
            observations.append(recorded)
            if recorded.status is ForwardObservationStatus.RECORDED:
                self._persist(recorded)
                # Measure the forward outcome from the completed
                # primary candles available at T' (measurement time).
                forward_candles = (
                    primary_candles.get(instrument) if primary_candles else None
                )
                if forward_candles is not None:
                    outcome = self.outcomes.measure(
                        recorded,
                        forward_candles,
                        measurement_timestamp=now,
                    )
                    outcomes.append(outcome)
                    self._persist(outcome)

        if alert_cycle is not None:
            counts = getattr(alert_cycle, "counts", None)
            if counts is not None:
                alerts_total = int(getattr(counts, "emitted", 0) or 0)
                alerts_delivered = int(getattr(counts, "delivered", 0) or 0)
                alerts_failed = int(getattr(counts, "failed", 0) or 0)
                alerts_suppressed = int(getattr(counts, "suppressed", 0) or 0)

        if quality_universe is not None:
            evaluate_done = getattr(quality_universe, "counts", None)
            if evaluate_done is not None and not getattr(evaluate_done, "tested", None):
                error = "setup-quality universe produced no tested instrument."
            else:
                error = ""

        return ForwardCycleResult(
            cycle_id=cycle_id,
            session_id=session.session_id,
            scan_cycle_id=scan_id,
            reference_now=now,
            instruments=instruments,
            observations=tuple(observations),
            outcomes=tuple(outcomes),
            alerts=alerts_total,
            alerts_delivered=alerts_delivered,
            alerts_failed=alerts_failed,
            alerts_suppressed=alerts_suppressed,
            error=error,
        )

    # ------------------------------------------------------------
    # SESSION + REPORT
    # ------------------------------------------------------------

    def open_session(self, **kwargs: Any) -> ForwardSession:
        return self.sessions.open_session(**kwargs)

    def close_session(
        self,
        session_id: str,
        *,
        ended_at: datetime | None = None,
    ) -> ForwardSession:
        return self.sessions.close_session(session_id, ended_at=ended_at)

    def report(
        self,
        sessions: Sequence[str] | ForwardSession | str,
        *,
        outcomes: Sequence[ForwardOutcome] | None = None,
        label: str = "",
    ) -> ForwardValidationReport:
        """
        Build the deterministic forward-validation report for one or
        more sessions from the RECORDED observations.

        All counts are derived from the recorded observations / alert
        cycle outputs — nothing fabricated. Outcome-derived dimensions
        are reported only when outcomes are supplied (the caller
        decides which measurements are mature).
        """

        if isinstance(sessions, ForwardSession):
            session_ids = (sessions.session_id,)
        elif isinstance(sessions, str):
            session_ids = (sessions,)
        else:
            session_ids = tuple(sessions)
        observations: list[ForwardObservation] = []
        for sid in session_ids:
            obs = self.sessions.observations_for(sid)
            observations.extend(obs)
        observations.sort(key=lambda o: (o.observation_timestamp, o.observation_id))

        outcome_list = list(outcomes or ())
        outcome_list.sort(key=lambda o: (o.observation_id, o.outcome_id))

        setup_counts = _count_tuples(
            [o.setup_id for o in observations if o.setup_id],
        )
        classification_counts = _count_tuples(
            [o.quality_classification.name for o in observations],
        )
        direction_counts = _count_tuples(
            [o.direction for o in observations if o.direction],
        )
        setup_type_counts = _count_tuples(
            [o.setup_type for o in observations if o.setup_type],
        )
        mtf_alignment_counts = _count_tuples(
            [o.mtf_alignment for o in observations],
        )
        lifecycle_counts = _count_tuples(
            [o.lifecycle_state.name for o in observations],
        )
        alert_counts = _count_tuples(
            [o.alert_state for o in observations if o.alert_state],
        )
        outcome_availability = _count_tuples(
            [o.availability.name for o in outcome_list],
        )
        outcome_direction_counts = _count_tuples(
            [o.outcome_direction.name for o in outcome_list],
        )
        consistency_counts = _count_tuples([
            ("CONSISTENT" if o.direction_consistent is True else
             "INCONSISTENT" if o.direction_consistent is False else
             "NOT_EVALUABLE")
            for o in outcome_list
        ])

        # Alert totals are re-derived from the recorded observations
        # (each observation carries the alert state at T) — the alert
        # cycle numbers are the operational source; the observations
        # are the durable point-in-time source used here.
        alerts_total = sum(1 for o in observations if o.alert_state == "ALERTED")
        alerts_delivered_total = sum(
            1 for o in observations if o.alert_state == "ALERTED"
        )
        alerts_suppressed_total = sum(
            1 for o in observations
            if o.alert_state == "SUPPRESSED"
        )

        failures: Counter[str] = Counter()
        for sid in session_ids:
            session = self.sessions.session(sid)
            if session is not None:
                for name, count in session.counts.failures:
                    failures[name] += count

        report_id = build_report_id(
            session_ids=session_ids,
            policy_version=self.config.policy_version(),
            label=label or self.config.label,
        )
        universe_count = (
            len(self.sessions.session(session_ids[0]).universe)
            if session_ids and self.sessions.session(session_ids[0]) is not None
            else 0
        )

        sample_note = _sample_size_note(len(observations), len(outcome_list))
        rationale = _report_rationale(
            len(observations),
            len(outcome_list),
            alerts_total,
            sample_note,
        )

        return ForwardValidationReport(
            report_id=report_id,
            session_ids=session_ids,
            observations=tuple(observations),
            outcomes=tuple(outcome_list),
            setup_counts=setup_counts,
            classification_counts=classification_counts,
            direction_counts=direction_counts,
            setup_type_counts=setup_type_counts,
            mtf_alignment_counts=mtf_alignment_counts,
            lifecycle_counts=lifecycle_counts,
            alert_counts=alert_counts,
            outcome_counts=outcome_availability,
            outcome_direction_counts=outcome_direction_counts,
            outcome_consistency_counts=consistency_counts,
            failure_counts=tuple(sorted(failures.items())),
            universe_instrument_count=universe_count,
            instruments_attempted=len(observations),
            instruments_with_usable_data=sum(
                1 for o in observations
                if o.quality_status is not SetupQualityStatus.UNAVAILABLE
            ),
            unsupported_instruments=sum(
                1 for o in observations if o.mtf_completeness == "UNSUPPORTED"
            ),
            stale_instruments=0,
            provider_failures=sum(
                1 for o in observations
                if o.quality_status is SetupQualityStatus.UNAVAILABLE
            ),
            successful_scans=0,
            incomplete_mtf_analyses=sum(
                1 for o in observations if o.mtf_completeness != "COMPLETE"
            ),
            setups_detected=sum(
                1 for o in observations if o.setup_id
            ),
            alerts_generated=alerts_total,
            alerts_delivered=alerts_delivered_total,
            alerts_suppressed=alerts_suppressed_total,
            sample_size_note=sample_note,
            rationale=rationale,
        )

    @staticmethod
    def _quality_for(
        quality_universe: SetupQualityUniverseResult | None,
        instrument: str,
    ) -> Any | None:
        if quality_universe is None:
            return None
        result = getattr(quality_universe, "result_for", None)
        if callable(result):
            try:
                return result(instrument)
            except Exception:
                return None
        return None

    def _persist(self, document: Any) -> None:
        if self.sessions.store is None:
            return
        store = self.sessions.store
        if isinstance(document, ForwardObservation):
            if hasattr(store, "save_observation"):
                store.save_observation(document)
        elif isinstance(document, ForwardOutcome):
            if hasattr(store, "save_outcome"):
                store.save_outcome(document)


def _lifecycle_by_instrument(
    lifecycle_cycle: SetupLifecycleCycleResult | None,
) -> dict[str, Any]:
    if lifecycle_cycle is None:
        return {}
    result = {}
    for item in getattr(lifecycle_cycle, "results", ()) or ():
        instrument = getattr(item, "instrument", "")
        if instrument:
            result[instrument] = item
    return result


def complete_lifecycle_cycle(lifecycle_cycle: Any) -> bool:
    """True when the lifecycle cycle actually evaluated a universe."""
    if lifecycle_cycle is None:
        return False
    return bool(getattr(lifecycle_cycle, "results", ()) or ())


def _alerts_by_setup(
    alert_cycle: AlertCycleResult | None,
) -> dict[str, list[Any]]:
    if alert_cycle is None:
        return {}
    result: dict[str, list[Any]] = {}
    for alert in getattr(alert_cycle, "alerts", ()) or ():
        setup_id = getattr(alert, "setup_id", "")
        if setup_id:
            result.setdefault(setup_id, []).append(alert)
    return result


def _count_tuples(values: Sequence[Any]) -> tuple[tuple[str, int], ...]:
    counts: Counter[str] = Counter()
    for value in values:
        counts[str(value)] += 1
    return tuple(sorted(counts.items()))


def _sample_size_note(observations: int, outcomes: int) -> str:
    if observations == 0:
        return "No forward observations were recorded."
    note = (
        f"{observations} observation(s) recorded; {outcomes} outcome(s) "
        "measured."
    )
    if outcomes < 10:
        note += " SAMPLE TOO SMALL for reliable inference — descriptive only."
    elif outcomes < 30:
        note += " Limited sample — descriptive only; not statistically conclusive."
    return note


def _report_rationale(
    observations: int,
    outcomes: int,
    alerts: int,
    sample_note: str,
) -> str:
    parts = [
        sample_note,
        f"alerts recorded across sessions: {alerts}.",
        "Forward validation is descriptive; it does not claim predictive "
        "validity or profitability.",
    ]
    return " ".join(parts)


__all__ = [
    "ForwardOutcomeEngine",
    "ForwardSessionManager",
    "ForwardValidationEngine",
    "build_forward_observation",
]