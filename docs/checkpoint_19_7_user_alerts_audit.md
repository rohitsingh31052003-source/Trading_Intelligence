# CHECKPOINT 19.7 — USER ALERTS (audit document)

- Status: COMPLETE / **PASS**.
- Scope: USER ALERTS layer on top of the FROZEN 19.6 setup-lifecycle boundary. This checkpoint builds the deterministic alert/event layer that answers: *"Which lifecycle events are important enough to notify the user about, what should the notification contain, and how should duplicate / noisy notifications be prevented?"*
- **BROKER EXECUTION REMAINS COMPLETELY DEFERRED THROUGHOUT CHECKPOINT 19.x** (Checkpoints 13–18 frozen; execution gate DISABLED). No 19.8–19.9 work started; nothing committed to git.
- Full audit: 53 sections below.

---

## 1. Objective

Build the USER ALERTS layer on top of the frozen 19.1–19.6 foundations. The layer answers "which lifecycle events are important enough to notify the user about, what should the notification contain, and how should duplicate/noisy notifications be prevented". It is NOT another signal engine: it converts MEANINGFUL lifecycle events into deterministic, user-facing alert events.

## 2. Scope

- Alert event model (deterministic identity, severity, delivery status).
- Alert eligibility policy (explicit, minimal, explainable).
- Identity-based deduplication / noise control (no time-based cooldowns).
- Alert content (reused upstream fields only — nothing fabricated).
- Delivery abstraction (generation separated from delivery; offline default sink).
- Deterministic formatters (text + JSON).
- In-memory alert history (19.8 owns durability/recovery).
- Deterministic offline CLI + demo.
- Comprehensive deterministic tests (60-point requirement + 18 adversarial scenarios).
- Audit document + AGENTS.md append.

## 3. Relationship to 19.1 (NIFTY Top 200 universe)

19.7 CONSUMES the frozen 19.1 universe only as the set of instruments that flow through the lifecycle cycle (the alert engine processes every `SetupLifecycleObservationResult` in a `SetupLifecycleCycleResult`, whose `instruments` derive from the frozen 19.1 `UniverseBuilder.nifty200()` / `NIFTY200_SYMBOLS`). The alert layer does NOT define a universe, does NOT re-validate membership, and does NOT import `UniverseBuilder` / `UniverseDefinition` (verified by source: `dashboard/user_alerts.py` imports no universe code). Universe-wide processing is proven for the full NIFTY Top 200 (see §36).

## 4. Relationship to 19.2 (reliable intraday data coverage)

19.7 consumes the 19.2 data-quality semantics INDIRECTLY: the lifecycle observations it consumes carry `quality_status` / `quality_classification` / `quality_score` that were derived (upstream, in 19.5) from 19.2 data-quality states (STALE / INCOMPLETE / UNAVAILABLE / VALID). The alert layer never reads a provider, never calls `IntradayCoverageEngine`, never inspects candles and never re-classifies freshness. A data-unavailable lifecycle observation (`LifecycleObservationStatus.DATA_UNAVAILABLE`) is a lifecycle OBSERVATION, not an alert EVENT — it produces NO alert (tested).

## 5. Relationship to 19.3 (continuous market scanning)

19.7 consumes the 19.3 scan-cycle identity: every alert carries `scan_cycle_id` (the 19.3 identity that produced the lifecycle event) and every alert-processing cycle embeds the consumed lifecycle cycle's `scan_cycle_id`. The alert layer implements NO scheduling, NO polling loop, NO no-overlap semantics and NO scan-state persistence (19.3 owns those; 19.8 owns reliability). The alert set is a deterministic function of the lifecycle event stream — not of scan frequency.

## 6. Relationship to 19.4 (multi-timeframe analysis)

19.7 consumes the 19.4 MTF state INDIRECTLY: the lifecycle observations carry the 19.5 assessment, which references the 19.4 `PerSymbolMtfResult` (alignment / completeness). The alert copies those values VERBATIM (`mtf_alignment` / `mtf_completeness`) when available; it never recomputes MTF state and never re-derives alignment. An incomplete / unavailable MTF state is carried honestly (`None` / `INCOMPLETE`) and is a data-quality prerequisite, not an alert event.

## 7. Relationship to 19.5 (setup-quality intelligence)

19.7 consumes the 19.5 quality snapshot VERBATIM: `quality_status` / `quality_classification` / `quality_score` carried by the lifecycle observation are copied into the alert; the 19.5 `SetupClassification` (11Q) / `confluence_score` / `has_conflict` are copied when available. The alert layer NEVER recalculates quality, NEVER re-scores, NEVER re-ranks and NEVER re-classifies. The optional `min_quality_classification` config gate is a FILTER on the carried classification (never a re-scoring). Tests prove the carried quality snapshot is identical regardless of the alert config (19.5 untouched).

## 8. Relationship to 19.6 (setup lifecycle)

19.7 is DOWNSTREAM of 19.6. It consumes the deterministic lifecycle EVENTS that 19.6 exposes — the per-symbol `SetupLifecycleObservationResult.transition` (the authoritative 19.6 `SetupLifecycleTransition` with `from_state` / `to_state` / `reason`) and the creation events (`result.created`) — and converts the MEANINGFUL ones into alerts. It does NOT hold the 19.6 store, does NOT manage lifecycle state, does NOT create lifecycle identities, does NOT reconstruct lifecycle history and does NOT re-derive transitions. The alert id embeds the 19.6 observation id (the lifecycle-layer idempotency basis), so the exact event is fully traceable.

## 9. Existing alert/notification architecture discovered

The full repository was audited before any change. Findings:

- NO alert system existed anywhere (no `alert`, `notification`, `notify`, `webhook`, `telegram`, `discord`, `slack`, `email`, `sms`, `push` implementation in `src/` or `scripts/`).
- NO notification abstraction existed.
- NO alert configuration existed.
- NO alert deduplication logic existed.
- NO cooldown / debounce logic existed.
- NO event bus / event abstraction existed (the 19.6 lifecycle `transition` records are the closest existing "event" concept, and they are consumed by 19.7).
- NO user-facing notification semantics existed in any prior checkpoint.
- NO alerting mechanism was coupled to broker execution / order placement / trade decisions / paper trading / historical research (because none existed).

## 10. Existing mechanisms reused

- 19.6 `SetupLifecycleObservationResult` / `SetupLifecycleTransition` / `SetupLifecycleCycleResult` — consumed VERBATIM as the lifecycle event source.
- 19.6 deterministic-ID convention (`<prefix>-` + sha256[:16]) — reused for `alert-` / `alert-cycle-`.
- 19.5 `SetupQualityClassification` — reused for the optional quality gate (rank comparison).
- 19.4 MTF values — copied verbatim from the carried assessment.
- 19.1 `NIFTY200_SYMBOLS` — used in tests for Top-200 processing.
- Existing conventions: frozen+slots models, empty `__init__.py` + full-path imports, `reporting/__init__.py` NOT extended (formatter imported via full path), operator CLI, deterministic test style, `docs/checkpoint_*_audit.md` audit documents, AGENTS.md append.

## 11. Existing mechanisms intentionally NOT reused (and why)

- **Sprint 11T `TradeOpportunity` / 11S `TradeDecision` / 11R `TradeCandidate`** — these are trade-decision/opportunity artifacts with execution-adjacent semantics; alerting on them would couple notification to trade decisions. The alert layer consumes LIFECYCLE events only (analytical, non-execution).
- **Sprint 11W `HistoricalOutcome` / 11X `HistoricalPerformanceStatistics` / 11Y `EvidenceStrength` / 11Z strategy interpretation / 12A decision intelligence** — historical/evidence/decision layers, semantically incompatible with "a lifecycle event happened" alerting.
- **Paper-trade lifecycle (WAITING_FOR_ENTRY → OPEN → CLOSED)** — a trade-result lifecycle, not a setup lifecycle; not consumed.
- **19.3 `ContinuousScannerEngine` scheduling loop** — alerting is a pure function of the lifecycle cycle; the scanner loop is a separate concern (19.8 owns reliability around both).
- **Any external notification provider** — none existed; none was introduced (see §25).

## 12. Alert event model

`src/engine/models/user_alerts.py` (frozen+slots):

- `AlertEventKind` — the SMALLEST alert vocabulary justified by the 19.6 semantics: `SETUP_DETECTED` / `SETUP_CONFIRMED` / `SETUP_INVALIDATED` / `SETUP_EXPIRED` (one alert kind per distinguishable lifecycle state-arrival event; no "score change", "still confirmed", "data unavailable" or "observed" kinds).
- `AlertSeverity` — small fixed vocabulary `INFO` / `IMPORTANT` / `CRITICAL`, bound DETERMINISTICALLY to the event kind via `severity_for_kind` (NOT configurable, NOT a probability/confidence/recommendation).
- `AlertDeliveryStatus` — `DELIVERED` / `FAILED` / `SKIPPED` / `SUPPRESSED` (the smallest justified delivery vocabulary).
- `AlertEvent` — the immutable user-facing alert (identity + lifecycle identity + carried analytical payload).
- `AlertDeliveryResult` — deterministic delivery outcome (delivery timestamp kept SEPARATE from the analytical timestamp).
- `SuppressedAlert` — explicit auditable record of a suppressed event (never a silent drop).
- `AlertCounts` — deterministic aggregate counts (invariants: `emitted == delivered + failed + skipped`; `eligible == emitted + suppressed`).
- `AlertCycleResult` — one universe-wide alert-processing cycle.
- Helpers: `build_alert_id`, `kind_for_state`, `severity_for_kind`.

## 13. Lifecycle-event-to-alert boundary

```
19.6 lifecycle event (transition / creation)
        ↓
19.7 AlertEngine._event_for_result  (consumes the 19.6 event VERBATIM)
        ↓
alert kind = kind_for_state(arrived state)
        ↓
eligibility policy (config)
        ↓
identity-based dedup (alert id in store)
        ↓
AlertEvent (frozen) -> delivery channel -> user
```

The alert layer consumes lifecycle events rather than reconstructing them: a `result.transition` is used directly (its `from_state`, `to_state`, `reason`); a `result.created` lifecycle is the detection / first-observation-confirmation event. Results carrying NO state change (repeated observations, DATA_UNAVAILABLE, LATE_REJECTED, duplicates) produce NO event and therefore NO alert.

## 14. Alert eligibility policy

Explicit, deterministic, minimal:

1. The lifecycle events examined are the state-arrival events 19.6 produced per symbol per cycle (transitions + creations).
2. Only OBSERVED / SUPERSEDED / ABSENT observations can carry a state-arrival event; DATA_UNAVAILABLE / LATE_REJECTED / duplicate observations NEVER produce an alert.
3. The alert kind is a deterministic function of the arrived-at lifecycle state.
4. Kind eligibility is governed by the config `eligible_kinds` — default `SETUP_CONFIRMED` / `SETUP_INVALIDATED` / `SETUP_EXPIRED` ONLY (a detection is informational and would be spam on the Top 200).
5. `enabled=False` suppresses every eligible event explicitly.
6. An optional `min_quality_classification` gate filters on the CARRIED 19.5 classification (never recalculates quality).
7. Deterministic identity-based deduplication: the same logical event (same alert id) recorded before is suppressed explicitly.

## 15. Alert identity

`build_alert_id(policy_version, kind, lifecycle_id, observation_id)` → `"alert-" + sha256[:16]` over the canonical string `policy|<policy_version>|kind|<kind>|lifecycle|<lifecycle_id>|observation|<observation_id>`.

The identity is a function of:
- the alert-policy version (config-derived, deterministic),
- the event kind,
- the 19.6 lifecycle id,
- the 19.6 observation id (the lifecycle-layer idempotency basis).

It NEVER depends on random UUIDs, wall-clock generation, provider response order or object memory addresses.

## 16. Alert ID invariants

- The same logical lifecycle event (same lifecycle + same observation + same kind + same policy) ALWAYS yields the same alert id.
- A DIFFERENT lifecycle transition of the SAME setup (different observation / different kind) yields a DIFFERENT alert id — one setup may legitimately produce several distinct alerts (CONFIRMED then INVALIDATED).
- A policy-rule change (different policy version) yields a different alert id for the same event (deterministic policy versioning).
- The alert id does not depend on delivery time (proven: injecting different delivery instants never changes the identity).
- The alert id does not depend on randomness (proven: same event → same id across runs).

## 17. Deduplication

Deterministic identity-based deduplication (NOT time-based cooldowns):

- The same lifecycle event processed twice (same store) → the second is `SUPPRESSED` with reason `"deduplicated (this lifecycle event already produced an alert)"`.
- The same scan cycle processed twice → identical alert set (no duplicate).
- The same setup remains in the same lifecycle state over many scans → NO new lifecycle transitions → NO new alerts (the core noise-control property).
- Reordered input (provider / universe order reversed) → identical alert set.
- Deduplication never suppresses legitimate distinct lifecycle events: CONFIRMED then INVALIDATED remain independently alertable (proven).

## 18. Idempotency

Alert processing is idempotent per event: given the same lifecycle event + policy + config, processing twice produces one logical alert (the second is explicitly suppressed). No wall-clock dependence is used to determine duplication. A "restart simulation" (reconstructing the store from the first alert and re-processing) yields the same dedup result (proven).

## 19. Noise-control semantics

- The same setup appearing in many scan cycles CANNOT produce repeated alerts because unchanged lifecycles produce NO new lifecycle transitions in 19.6, and every event has a distinct, stable alert id.
- The alert set is a deterministic function of the lifecycle event stream — not of scan frequency, clock time, provider order or universe order.
- 20 consecutive confirmed cycles → exactly ONE confirmation alert (proven).
- Repeated unchanged observations produce NO lifecycle event and therefore NO alert (proven).
- Suppression is explicit and auditable (every suppressed event appears in `AlertCycleResult.suppressed` with a reason).

## 20. Alert severity / priority

A small, fixed, explainable vocabulary: `INFO` / `IMPORTANT` / `CRITICAL`, bound deterministically to the event kind:

- `SETUP_DETECTED` → INFO
- `SETUP_CONFIRMED` → IMPORTANT
- `SETUP_INVALIDATED` → CRITICAL
- `SETUP_EXPIRED` → CRITICAL

Severity is NOT configurable (it is a semantic property of the event kind) and is NEVER a disguised trade recommendation — it is not a probability, not a confidence and not a profitability claim.

## 21. Alert content

Every alert carries (copied from the upstream models, never fabricated):

- `alert_id`, `kind`, `severity`
- `lifecycle_id`, `setup_id`, `instrument` (canonical)
- `lifecycle_state` (arrived), `previous_lifecycle_state` (from the 19.6 transition's `from_state`; `None` for a creation)
- `transition_reason` (the exact 19.6 reason)
- `setup_type` (reused 11R taxonomy), `direction` (BULLISH/BEARISH — analytical, never an execution command), `primary_timeframe`
- `observation_id`, `observation_timestamp` (the analytical event timestamp), `scan_cycle_id`, `observation_status`
- `policy_version`
- carried 19.5 quality (`quality_status` / `quality_classification` / `quality_score`)
- carried 19.4 MTF (`mtf_alignment` / `mtf_completeness`)
- carried 11Q (`setup_classification` / `confluence_score` / `has_conflict`)
- `reason` (human-readable, derived from the actual inputs)

## 22. Alert formatting

`src/engine/reporting/user_alerts.py` — `AlertFormatter` (stateless, deterministic, returns str, no print()):

- `format_alert(alert)` — human-readable text (SETUP CONFIRMED header, symbol, direction, setup type, timeframe, state, previous state, quality, MTF, 11Q, observed, reason, ids, severity, policy + WARNING).
- `format_cycle(result)` — universe-wide view (counts, alerts, suppressed audit + WARNING).
- `alert_to_json(alert)` / `cycle_to_json(result)` — deterministic machine-readable JSON (sorted keys).
- Every report ends with the explicit warning that alert events are descriptive lifecycle notifications, NOT trade signals, NOT predictions and NOT trading recommendations.

## 23. Delivery abstraction

ALERT GENERATION is fully separate from ALERT DELIVERY:

- `AlertDeliveryChannel` (Protocol) — `name` + `deliver(alert) -> str`. Deliberately trivial so any future external integration (console / file / http / Slack / Discord / email ...) can be layered without contaminating core logic.
- `ConsoleAlertSink` — DEFAULT deterministic offline sink (records delivered alerts, returns the text formatter output). No network, no credentials.
- `FailingAlertSink` — deterministic raising channel (test-only).
- The engine accepts zero or more channels; with NO channels every emitted alert is recorded as `SKIPPED` (explicit, auditable), never silently dropped.

## 24. Delivery status

`AlertDeliveryStatus` — `DELIVERED` / `FAILED` / `SKIPPED` / `SUPPRESSED`:

- DELIVERED: a channel accepted the alert (requires a delivery timestamp + channel name).
- FAILED: all channels raised (requires a reason; never a silent success, never an engine crash).
- SKIPPED: no channel configured (explicit, auditable).
- SUPPRESSED: identity-dedup / policy-ineligible / cap (explicit, auditable).
- A delivery failure NEVER invalidates a setup, NEVER mutates lifecycle state and NEVER alters the alert event (proven: the alert engine holds no lifecycle store).

## 25. External-channel boundary

NO external notification integration was introduced: no Telegram / WhatsApp / email / SMS / push / Discord / Slack / webhook credentials, APIs or SDKs. The alert layer establishes the boundary; actual production integrations can be layered behind `AlertDeliveryChannel` without contaminating core alert logic. No credentials are required for deterministic tests.

## 26. Alert configuration

`src/engine/config/user_alerts_config.py` — `UserAlertConfig` (frozen+slots, validated):

- `enabled` (master switch; False suppresses every eligible event explicitly).
- `eligible_kinds` (the ONLY alert-eligibility knob; default CONFIRMED/INVALIDATED/EXPIRED).
- `min_quality_classification` (optional filter on the carried 19.5 classification).
- `max_per_cycle` (optional cap on emitted alerts per cycle; remaining eligible events are SUPPRESSED with reason — explicit, auditable).
- `max_qualified_top_n` (optional cap on qualified candidates considered — a consumption of the EXISTING 19.5 ranking, never a second ranking algorithm).
- `severity_policy` (always `"fixed"` — severity is not configurable).
- `label` / `metadata`.
- `snapshot()` (deterministic auditable config snapshot) + `alert_policy_version(config)` (deterministic policy version).

Configuration cannot silently alter 19.5 quality semantics or 19.6 lifecycle semantics (proven).

## 27. Multiple-alert ordering

Multiple alerts in one cycle order deterministically:

- The lifecycle events are enumerated in canonical order (sorted by `instrument`, then `observation_timestamp`, then `lifecycle_id`).
- The emitted alerts are ordered by that same canonical event order (which is instrument-ascending for same-timestamp events).
- Provider ordering cannot affect alert ordering (proven: reversed input → identical order).
- Universe ordering cannot affect alert ordering (proven: explicit `instruments` ordering is canonicalized).

## 28. Suppression semantics

Every suppression is explicit and auditable:

- `AlertCycleResult.suppressed` carries a `SuppressedAlert` per non-emitted event with `alert_id`, `kind`, `setup_id`, `lifecycle_id`, `instrument`, `observation_id`, `observation_timestamp`, `policy_version` and an explicit `reason` (`"deduplicated (...)"` / `"alerting disabled by configuration."` / `"alert kind X is not in the eligible set."` / `"carried quality classification ... below ..."` / `"maximum alerts per cycle reached (...)"`).
- No silent dropping: every eligible-but-not-emitted event appears in the suppression audit.
- `AlertCounts` invariants guarantee the accounting is consistent.

## 29. Alert history

The in-memory `AlertStore` keeps, per run:

- the emitted `AlertEvent` records (immutable),
- the `AlertDeliveryResult` per alert,
- the `SuppressedAlert` audit records.

Historical alerts are NEVER mutated when future lifecycle events occur (frozen records; proven by point-in-time tests). 19.8 owns durable persistence / recovery / restart.

## 30. Timestamp semantics

- The analytical event timestamp is the upstream 19.6 observation instant (explicit, timezone-aware, never wall-clock).
- An optional delivery timestamp (for operational purposes) is carried SEPARATELY on `AlertDeliveryResult` and is NEVER used for analytical identity.
- No hidden wall-clock dependency exists in the alert engine (source-verified: no `datetime.now()` / `time.time()`).
- Injected delivery instants (`at=`) work deterministically in tests.

## 31. Point-in-time safety

- Alerts are generated using ONLY information available at the lifecycle event's observation time.
- A future lifecycle event can never alter an earlier alert (frozen records).
- T1 DETECTED → T2 CONFIRMED → T3 INVALIDATED: the T2 confirmation alert remains exactly the same after T3 (proven).
- Future quality changes cannot rewrite historical alert payloads (proven).
- Future invalidation cannot alter earlier confirmation alerts (proven).
- Future MTF information cannot alter historical alerts (proven).

## 32. Look-ahead protection

The alert engine is a pure function of the consumed lifecycle cycle result + delivery channels + alert store. It never reads candles / providers, never calls wall-clock and never inspects future lifecycle events. A future lifecycle event (invalidation / confirmation / MTF change) cannot alter an earlier alert. The point-in-time correctness established in 19.4–19.6 is preserved unchanged.

## 33. Explainability

- Every alert carries a human-readable `reason` derived from the actual inputs.
- The score is NOT recomputed; the carried quality / MTF / 11Q values are copied verbatim.
- The `AlertFormatter` renders an auditable per-alert report (ids, states, reasons, carried values).
- Every report ends with the descriptive warning (analytical, non-directive).

## 34. Determinism

- Identical inputs + identical store/channel state → identical outputs.
- Deterministic alert ids, cycle ids, ordering, counts, formatter output.
- No wall-clock, no randomness, no unordered iteration.
- Repeated + shuffled-input evaluation produce identical alert sets (proven).

## 35. Universe-wide processing

- The alert engine processes every `SetupLifecycleObservationResult` in a 19.6 cycle (which covers the full NIFTY Top 200).
- A 200-symbol cycle with qualified candidates produces 200 confirmation alerts, deterministically ordered (proven).
- All eligible alerts are accounted for (counts invariants; proven for 200 symbols).

## 36. Failure isolation

- One symbol's DELIVERY failure is isolated: 199 alerts delivered, 1 failed, none dropped, no universe abort (proven for the Top 200).
- A delivery failure NEVER invalidates a setup / mutates lifecycle state (proven).
- The event enumeration defensively skips non-result entries (a corrupt lifecycle result cannot abort the universe).
- A raising delivery channel is caught per-alert and recorded as FAILED (never raising into the engine).

## 37. Relationship to setup lifecycle

19.7 is the downstream consumer of the 19.6 setup lifecycle. It consumes the lifecycle EVENTS (transitions / creations) and does NOT manage lifecycle state, does NOT create lifecycle identities and does NOT reconstruct history. The 19.6 lifecycle semantics are preserved unchanged (a repeated unchanged observation produces no event; a data-unavailable observation produces no event; a terminal state is a real event).

## 38. Relationship to setup quality

19.7 copies the 19.5 quality snapshot verbatim (status / classification / score) and never recalculates quality. The optional quality gate filters on the carried classification. The 19.5 quality semantics are preserved unchanged (proven: the carried quality is identical regardless of the alert config).

## 39. Relationship to ranking

19.5 owns ranking. 19.7 does NOT create a second ranking algorithm: it consumes lifecycle events (which carry the quality snapshot but not a re-ranking), and the optional `max_qualified_top_n` cap consumes the EXISTING 19.5 `ranked_qualified` / `top_n` ranking. No ranking logic exists in the alert layer (source-verified).

## 40. Relationship to trade plans

19.7 introduces NO trade-plan semantics: no entry price, no stop loss, no target, no quantity, no position size, no risk/reward recommendation, no order type, no order placement, no automated execution. Alerts expose only analytical information already produced by the system (direction is analytical, never an execution command). The alert models contain zero trade-plan fields (source-verified + tested).

## 41. Broker-execution boundary

Absolutely no broker execution: no order placement / modification / cancellation, no broker position management, no automated entry / exit. The alert modules import no broker / execution / paper-trading / submission code (AST-verified + tested). Broker execution remains completely deferred throughout Checkpoint 19.x.

## 42. Reliability boundary

19.8 will own durable persistence / crash recovery / retries / watchdogs / health checks / metrics / operational observability / restart recovery. 19.7 exposes deterministic delivery results and an in-memory alert history but implements NO reliability framework (source-verified + tested: no retry / watchdog / threading / time imports).

## 43. Forward-testing boundary

19.9 will own forward validation. 19.7 creates NO forward-testing framework (source-verified + tested).

## 44. Files created

- `src/engine/models/user_alerts.py` (alert event model)
- `src/engine/config/user_alerts_config.py` (alert configuration)
- `src/dashboard/user_alerts.py` (alert engine + store + delivery channels)
- `src/engine/reporting/user_alerts.py` (alert formatters)
- `scripts/emit_alerts.py` (operator CLI)
- `scripts/test_checkpoint_19_7.py` (demo, 17 checks)
- `tests/test_user_alerts.py` (95 tests)
- `tests/test_user_alerts_cli.py` (11 tests)
- `docs/checkpoint_19_7_user_alerts_audit.md` (this document)

## 45. Files modified

- `AGENTS.md` (this checkpoint appended — the only modification).

## 46. Tests added

- `tests/test_user_alerts.py` — 95 deterministic tests across the 60-point requirement list (architecture, eligibility, identity, deduplication, content, ordering, delivery, point-in-time, timestamps, configuration, universe, boundaries, adversarial, models, formatter, immutability).
- `tests/test_user_alerts_cli.py` — 11 deterministic CLI tests.
- `scripts/test_checkpoint_19_7.py` — demo (17 checks).

## 47. Focused test results

- `tests/test_user_alerts.py`: **95 passed**.
- `tests/test_user_alerts_cli.py`: **11 passed**.
- Demo `scripts/test_checkpoint_19_7.py`: **17 checks passed / 0 failed** (exits 0).
- 19.x focused suite (19.1–19.7): **590 passed** in 12.49s.

## 48. Regression results

- 19.1 `test_nifty200_universe.py`: 48 passed.
- 19.2 `test_market_session` + `test_intraday_coverage*`: 99 passed.
- 19.3 `test_continuous_scanner` + `test_scan_market_cli`: 53 passed.
- 19.4 `test_mtf_analysis` + `test_mtf_analysis_cli`: 84 passed.
- 19.5 `test_setup_quality` + `test_setup_quality_cli`: 83 passed.
- 19.6 `test_setup_lifecycle` + `test_setup_lifecycle_cli`: 117 passed.
- Trading-intelligence regression (11O–11U): 522 passed.
- Provider/dashboard/data regression: 617 passed.
- **FULL SUITE: 6896 passed / 12 skipped / 2 warnings** (baseline 6790 → +106 net new = 95 + 11; exact match; 12 skips = opt-in real-broker/sandbox tests; 2 warnings = pre-existing third-party deprecations). Pipeline baseline unchanged (signals=4, trades=3).

## 49. Adversarial alert test results

All 18 mandatory adversarial scenarios pass (deterministic):

1. Same setup confirmed across 20 consecutive scan cycles → exactly ONE confirmation alert.
2. Same lifecycle event processed twice → one logical alert (second suppressed).
3. Same scan cycle processed twice → identical alert set.
4. Provider ordering reversed → identical alert set/order.
5. Universe ordering reversed → identical alert set/order.
6. Two different setups for the same symbol → one event at a time, distinct alerts.
7. Two different lifecycle transitions for the same setup → distinct alert ids.
8. Confirmation followed by invalidation → both independently alertable.
9. Confirmation followed by expiration → both independently alertable.
10. Data unavailable after confirmation → no alert, no lifecycle mutation.
11. Incomplete MTF after confirmation → no alert (data-gate observation).
12. Future invalidation after an earlier confirmation alert → confirmation alert unchanged.
13. Future confirmation after an earlier detection → confirmation alert only (detection not in default eligible set).
14. Multiple symbols generating alerts in one cycle → all accounted, deterministic order.
15. One symbol failing while 199 others continue → 199 delivered, 1 failed, none dropped.
16. Alert delivery failure → FAILED status, never invalidates setup.
17. Alert generation repeated after process restart simulation → same dedup result.
18. Same alert payload generated independently in two runs → byte-identical.

## 50. Known limitations

- The alert store is in-memory (19.8 owns durable persistence / recovery / restart).
- The default delivery channel is the offline console sink (external integrations are layered behind `AlertDeliveryChannel` in a future increment; no external channel is wired).
- `max_qualified_top_n` is implemented as a config knob but the demo/tests exercise the lifecycle-event path (the Top-200 qualified-candidate cap is a documented consumption of the 19.5 ranking for a future alert-burst scenario).
- The primary timeframe is derived from the carried assessment's `primary_timeframe` (fallback "15m") — the 19.6 identity does not carry a timeframe field, so the alert uses the assessment's carried value when available.
- The `_previous_state_for` derivation for CREATED lifecycles is `None` (the lifecycle began there); for transitions it is the 19.6 transition's `from_state` (authoritative).
- No severity-policy knob beyond the fixed mapping (by design).

## 51. Confirmation that 19.8–19.9 were NOT implemented

No 19.8 (reliability / recovery / observability) and no 19.9 (validation / forward-testing) work was started. The alert layer exposes delivery results + in-memory history only; it implements no durable persistence, no retries, no watchdogs, no health checks, no metrics, no restart recovery and no forward-testing framework.

## 52. Final architectural assessment

The 19.7 user-alerts layer is a clean, deterministic, honest boundary that:

- consumes the FROZEN 19.6 lifecycle events (transitions / creations) and does NOT reconstruct lifecycle state, quality or ranking;
- uses the SMALLEST alert vocabulary justified by the 19.6 semantics (DETECTED / CONFIRMED / INVALIDATED / EXPIRED);
- enforces explicit, minimal alert eligibility (confirmation / invalidation / expiration by default; detection opt-in);
- implements identity-based deduplication (no time-based cooldowns) so repeated unchanged lifecycle observations never spam;
- keeps alert identity deterministic and independent of randomness / delivery time / provider / universe ordering;
- keeps alert content analytical and non-directive (no trade-plan semantics, no execution commands);
- separates alert generation from alert delivery (offline console sink default; external channels layered later);
- is point-in-time safe (future lifecycle events cannot mutate earlier alerts);
- supports NIFTY Top 200 processing with per-symbol failure isolation and explicit, auditable suppression;
- is fully deterministically testable offline (106 new tests + 17-check demo, zero regressions);
- introduces NO broker execution, NO reliability framework and NO forward-testing framework.

The architecture is clean enough for 19.8 to add durable persistence / recovery / observability around the alert store + delivery results, and for real-world user alerts to be layered without coupling analytical logic to a specific notification provider.

## 53. Final verdict

**PASS** — the 19.7 user-alerts layer is complete, deterministic, offline-testable, regression-safe and broker-execution-deferred. **STOP after Checkpoint 19.7; 19.8–19.9 are NOT implemented; broker execution remains deferred.**
