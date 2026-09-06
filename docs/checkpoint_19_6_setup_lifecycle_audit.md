# Checkpoint 19.6 — Setup Lifecycle

- Status: COMPLETE / PASS
- Audit date: 2026-09-06
- Baseline: Checkpoint 19.5 — 6673 passed / 12 skipped / 2 warnings

This document is the audit + design record for Checkpoint 19.6. It follows
the conventions of all previous Checkpoint audit documents.

---

## 1. Objective

Build the SETUP LIFECYCLE layer on top of the FROZEN 19.1-19.5
foundations. 19.5 answers:

> "At analysis time T, what setup candidates exist and how strong are
> they?"

19.6 must answer:

> "Is this the SAME setup across subsequent scan cycles, what lifecycle
> state is it currently in, and how has that setup evolved over time?"

The layer adds stable identity, lifecycle state, temporal continuity,
transition semantics, confirmation/invalidation/expiration semantics,
deterministic state transitions, explicit transition reasons, and
auditable lifecycle history. It does NOT alert, execute, size positions,
manage portfolios, recover, or forward-test.

## 2. Scope

IN SCOPE: deterministic setup identity, lifecycle state, state
transitions, transition reasons, lifecycle history, in-memory lifecycle
store, idempotency, deterministic ordering, explicit timestamps,
point-in-time safety, universe-wide processing, failure isolation, an
operator diagnostic CLI, and a deterministic formatter.

NOT IN SCOPE (belong to later checkpoints): user alerts / notifications
(19.7), persistence/recovery/observability/retries/watchdogs (19.8),
forward-testing/backtesting (19.9), and ALL broker/execution semantics
(deferred through Checkpoint 19.x).

## 3. Relationship to 19.1 (universe)

The lifecycle layer never defines its own universe. It consumes the
canonical instrument symbols produced by the frozen 19.1 manifest /
`UniverseBuilder` / `UniverseDefinition` through the 19.5
`SetupQualityUniverseResult.instruments`. The `NIFTY` benchmark stays
separate from the Top-200 stock universe; a setup lifecycle is keyed by
the canonical instrument name.

## 4. Relationship to 19.2 (intraday data coverage)

The lifecycle layer consumes the frozen 19.2 data-quality vocabulary
(`IntradayCoverageStatus`) and the completed-candle boundary indirectly:
19.5 assessments already encode stale/data-gate conditions
(`stale_data`, INCOMPLETE / UNAVAILABLE data-gate states). The lifecycle
layer NEVER fetches or normalizes candles — it only classifies an
assessment as OBSERVED / ABSENT / DATA_UNAVAILABLE using the 19.5/19.2
signals already present on the assessment.

## 5. Relationship to 19.3 (continuous scanning)

The scan-cycle identity (`scan_cycle_id`) is consumed verbatim by the
lifecycle layer: every observation records the cycle that produced it, and
scan-cycle id + setup identity + assessment fingerprint drive idempotent
replays. The lifecycle layer does NOT create another scanner; it is
strictly downstream (Scan Cycle → 19.4 MTF → 19.5 Setup Quality → 19.6
Lifecycle).

## 6. Relationship to 19.4 (MTF)

The lifecycle layer references the MTF instrument/timeframe semantics via
`primary_timeframe` and `mtf_alignment` on the 19.5 result. The frozen
19.4 INCOMPLETE / UNAVAILABLE semantics are never converted into setup
invalidation: an assessment carrying an incomplete/unavailable MTF
resolves to the DATA_UNAVAILABLE observation class (state unchanged), not
to a transition.

## 7. Relationship to 19.5 (setup quality)

The lifecycle layer consumes 19.5 `SetupQualityResult` objects BY
REFERENCE (never mutates them, never re-scores). 19.5 remains the owner
of setup detection, the `SetupType` taxonomy, evidence, score, quality
classification, negative evidence, ranking, and explainability. The
lifecycle layer REUSES 19.5's confirmation-grade evidence (11Q
`POTENTIAL_SETUP` + 19.5 `QUALIFIED` status + not-stale) as its own
confirmation predicate rather than inventing a parallel confirmation
engine.

## 8. Existing lifecycle architecture discovered

A full repository audit found NO existing setup lifecycle layer of any
kind: no setup identity, no lifecycle state machine, no lifecycle store,
no lifecycle transitions, no lifecycle history. Checkpoint 19.5 audit
document §"not-implemented" list confirms setup lifecycle was explicitly
deferred.

## 9. Existing setup identity mechanisms discovered

None. No setup ID existed before 19.6. The only deterministic-ID
conventions found are the established `"<prefix>-" + sha256[:16]` pattern
used pervasively across the project (experiment `exp-`, suite `suite-`,
selection `sel-`, corpus `corpus-`, scan `cycle-`, MTF `mtf-`, setup
quality `sq-`, paper trade `pt-`, intent `intent-`, authorization
`auth-`, command `cmd-`, submission `submission-`, validation `lval-`).
19.6 follows this exact convention: `setup-<sha256[:16]>`.

## 10. Existing lifecycle mechanisms reused

- The `"<prefix>-" + sha256[:16]` deterministic-ID pattern.
- Frozen 19.5 models consumed verbatim (SetupQualityResult,
  SetupQualityUniverseResult, SetupQualityClassification,
  SetupQualityStatus, MtfAnalysis types, MarketContext types).
- The 19.5 confirmation-grade evidence (11Q POTENTIAL_SETUP) as the
  confirmation predicate.
- 19.5 test helpers in `tests/test_setup_quality.py` (`_evaluate`,
  `_aligned_mtf`, `_bullish_15m`, `_bearish_15m`, `_market_context`,
  `_flat_candles`, `_now`).
- Frozen 19.1 universe semantic (canonical instrument names).
- Frozen 19.2 status vocabulary.
- Frozen 19.3 scan-cycle identity strings.
- Pipeline baseline helpers (`HistoricalEvaluationPipeline`,
  `tests.test_pipeline.trending_dataset`).

## 11. Existing mechanisms intentionally not reused

- The paper-trading lifecycle (Product Phase 5: WAITING_FOR_ENTRY → OPEN
  → CLOSED / INVALIDATED / CANCELLED) is a TRADE-RESULT lifecycle coupled
  to entry/stop/target/planned quantity — unsafe and semantically wrong
  for setup lifecycle. Documented, NOT reused.
- The Sprint 11W OutcomeStatus / 11T OpportunityStatus / 12B
  IntegrationStatus / 11S DecisionClassification state machines are
  trade/outcome/decision state machines coupled to candidates, exits and
  authorization — semantically incompatible with setup identity. NOT
  reused.
- The Checkpoint 17 submission lifecycle (CREATED → SUBMITTED →
  ACCEPTED...) is coupled to execution submission state — never reused
  (checkpoint-19 boundary).
- The heavy `engine.persistence` stores and `PaperTradeStore` file-based
  stores are 19.8/Product-Phase-5 concerns. An in-memory store is used
  (19.8 owns persistence/recovery).

## 12. Setup identity definition

Setup identity = canonical tuple

```
instrument (canonical, strip+upper)
direction  (SetupDirection.BULLISH | BEARISH)
setup_type (19.5 SetupType, e.g. BREAKOUT / TREND_CONTINUATION)
timeframe  (primary_timeframe, canonical)
identity_version (rule version, 1)
```

hashed as `setup-` + sha256(identity.fields)[:16]. Setup identity is
deterministic, reproducible, explainable, point-in-time safe and never
depends on: scan-cycle id, wall-clock time, provider order, object
identity, memory address, random UUIDs, quality score, quality
classification, or transient data-quality fields.

## 13. Identity invariants

1. Same (instrument, direction, setup_type, primary_timeframe) →
   same setup ID everywhere and every run.
2. Direction is an identity component (LONG-scale setups are distinct
   from SHORT-scale setups) — documented in §27.
3. setup_type is an identity component (a BREAKOUT is a different
   analytical setup than a TREND_CONTINUATION).
4. instrument is identity: two instruments with identical-looking
   assessments get distinct setup IDs.
5. Quality score / band / stale flags / MTF alignment / data status are
   NOT identity components — they describe the assessment, not the
   setup.
6. identity_version is part of the identity (a future rule change is
   versioned, never silently rewritten).

## 14. Lifecycle state vocabulary

```
DETECTED    (initial; setup first observed, not yet confirmed)
CONFIRMED   (confirmation evidence observed at least once)
INVALIDATED (terminal; setup superseded by opposing direction)
EXPIRED     (terminal; absent for max_misses clean cycles)
```

This is the smallest deterministic machine that satisfies the 19.6
requirements. ABSENT / DATA_UNAVAILABLE / SUPERSEDED / LATE_REJECTED are
PER-OBSERVATION statuses (how the setup was observed in one cycle), not
persistent lifecycle states. No execution vocabulary (no ORDER_PLACED,
POSITION_OPEN, FILLED, SUBMITTED).

## 15. State definitions

- DETECTED: a directional setup candidate (identity resolvable) has been
  observed but has never produced a QUALIFIED 19.5 assessment (the reused
  11Q classification is not POTENTIAL_SETUP, or the quality bar was not
  reached yet). The candidate is present and being monitored — it has NOT
  been confirmed. Non-terminal.
- CONFIRMED: the setup produced at least one QUALIFIED 19.5 assessment
  (reused 11Q POTENTIAL_SETUP) per the configured confirmation rule.
  Confirmation is a HISTORICAL, one-way property: once confirmed, later
  quality fluctuations are recorded as assessment changes on
  observations but do NOT revert the state to DETECTED. Non-terminal.
- INVALIDATED: terminal. A deterministic disproof condition was met — an
  OPPOSING-direction setup candidate appeared for the same
  instrument/timeframe (the tracked setup's directional premise no
  longer holds). NEVER triggered by a data failure or a plain absence.
  Terminal.
- EXPIRED: terminal. The setup's identity was not observed for
  `max_consecutive_missing_observations` CONSECUTIVE CLEAN scan cycles.
  Driven by the deterministic scan-cycle stream, never a wall-clock
  timeout, and never by data-unavailable cycles. Terminal.

## 16. Transition table

| From        | To            | Condition (deterministic)                        | Reason                       |
|-------------|---------------|--------------------------------------------------|------------------------------|
| (new)       | DETECTED      | first observation with a directional candidate,  | "new setup lifecycle created — state [DETECTED]." |
|             |               | confirmation predicate not yet satisfied         |                              |
| (new)       | CONFIRMED     | first observation ALREADY satisfies the          | "qualified assessment observed — setup confirmed." |
|             |               | confirmation predicate                           |                              |
| DETECTED    | CONFIRMED     | a later OBSERVED assessment satisfies the        | "qualified assessment observed — setup confirmed." |
|             |               | confirmation predicate                           |                              |
| DETECTED /  | INVALIDATED   | opposing-direction setup observed for same      | "opposing directional setup observed ... superseded." |
| CONFIRMED   |               | instrument/timeframe (SUPERSEDED)                |                              |
| DETECTED /  | EXPIRED       | consecutive-miss counter reaches                | "setup not observed for N consecutive clean scan |
| CONFIRMED   |               | max_consecutive_missing_observations            | cycles — expired."           |
| CONFIRMED   | (no revert)   | sticky; no transition back to DETECTED           | (no un-confirm transition)   |
| CONFIRMED   | CONFIRMED     | (re)confirmation of a still-active lifecycle    | "setup observed and confirmed — state unchanged." |
|             |               | is a no-op (state unchanged)                    |                              |

Invalid transitions (rejected or structurally impossible):

- Terminal (INVALIDATED / EXPIRED) → any non-terminal: rejected; a
  terminal lifecycle can never reopen (see §26).
- ABSENT / DATA_UNAVAILABLE / SUPERSEDED / LATE_REJECTED are
  observation-classes applied per cycle; they are never entered as
  persistent lifecycle states. A lifecycle's own state field changes ONLY
  via the transitions above.

## 17. Transition reasons

Every transition carries an explicit `reason` string produced by the
rule that fired: creation reason, "qualified assessment observed — setup
confirmed.", "opposing directional setup observed ... — lifecycle
invalidated.", "... consecutive clean scan cycles — expired.". No
transition is ever recorded without a reason.

## 18. Initial-state semantics

First observation of a setup:

- If the confirmation predicate is satisfied at the first observation →
  the lifecycle is created directly in CONFIRMED (the setup was
  confirmation-grade the instant it appeared).
- Otherwise → created in DETECTED.
- The initial state is a pure function of the first assessment; it is
  deterministic.

## 19. Repeated-observation semantics

The same setup observed again: resolve identity → retrieve lifecycle →
evaluate the new assessment → append an observation record (with
scan_cycle_id, timestamp, quality score/band/status, alignment, data
status) → apply the transition table. No duplicate lifecycle is ever
created; the lifecycle is reused; assessment changes live on the
observation records, never on the identity.

## 20. Quality-change semantics

Score / classification / MTF alignment changes are assessment changes.
They never create a new identity, never un-confirm a CONFIRMED setup, and
do not by themselves change lifecycle state. A classification change is
represented by new observation records retaining the new score/band. The
quality classification (19.5) and the lifecycle state (19.6) are two
separate concerns and are never conflated.

## 21. Missing-observation semantics

A tracked setup missing from a CLEAN cycle (no data failure for that
symbol) advances `consecutive_missing` by 1. While below the threshold
the lifecycle state is unchanged and the observation is recorded with
observation_status ABSENT. Reaching the threshold triggers EXPIRED. A
single missing cycle is NOT invalidation — "setup absent" is always
distinguished from "setup disproven".

## 22. Data-failure semantics

A provider/data failure (stale primary data, INCOMPLETE/
UNAVAILABLE data-gate state) for a tracked setup resolves to observation
status DATA_UNAVAILABLE: the lifecycle state is UNCHANGED and the miss
counter is NOT advanced (a failed cycle must not count toward expiry).
Data failure is never invalidation.

## 23. Incomplete-MTF semantics

When the 19.5 result carries an INCOMPLETE / UNAVAILABLE MTF
completeness (or the primary frame is unavailable), the assessment
resolves to DATA_UNAVAILABLE (§22). MTF incompleteness is explicitly NOT
converted into invalidation and does not advance the miss counter. This
is tested.

## 24. Expiration semantics

Expiration is the only time-based rule and it is justified: a setup that
is not observed for `max_consecutive_missing_observations` (default 5)
consecutive CLEAN scan cycles is considered gone from the observable
universe. The rule is a deterministic counter over explicit scan cycles
— NO wall-clock timeout, NO arbitrary elapsed-time rule. The 19.6
implementation deliberately does NOT introduce wall-clock expiry; the
counter is the justified rule.

## 25. Terminal-state semantics

INVALIDATED and EXPIRED are terminal. Terminal lifecycles are excluded
from `active_lifecycles()` and exposed via `terminal_lifecycles()`.
Terminal lifecycles cannot be advanced by `process()` (returns the
existing snapshot with a "terminal" reason and no new
observation/transition), and cannot reopen.

## 26. Reopening / new-setup semantics

After a lifecycle becomes terminal, a later similar setup for the same
symbol receives a NEW lifecycle delimited by an `instance_discriminator`
(a nonce derived from the first observation timestamp). Identity = same
setup ID (identity fields) BUT a different lifecycle ID
(`lifecycle-<sha256[:16]>` over setup identity + instance
discriminator). This is the simplest deterministic rule: the old
terminal lifecycle is never resurrected; the new instance is a fresh
lifecycle with the same setup ID and a distinct lifecycle ID.

## 27. Direction and setup-type as identity components

Direction IS part of setup identity: the repository's existing setup
semantics (11Q/19.5) define setups directionally (LONG-scale vs
SHORT-scale candidates are different analytical objects), and mixing
them into one lifecycle would corrupt confirmation/evidence semantics.
When the opposing direction appears for the same instrument/timeframe,
the tracked lifecycle is INVALIDATED (SUPERSEDED) and the new direction
gets its own lifecycle. SetupType is also an identity component: a
BREAKOUT is analytically distinct from a TREND_CONTINUATION. A setup
that would change type is, by construction, a different setup (the old
type context no longer holds); the rule is documented in the engine and
tested.

## 28. Lifecycle history

`SetupLifecycle.observations` (tuple of frozen
`SetupLifecycleObservation` with observation_id, timestamp, scan_cycle_id,
previous/current state, reason, observation_status, quality score/band/
status, MTF alignment, assessment fingerprint) and
`SetupLifecycle.transitions` (tuple of frozen `SetupLifecycleTransition`
with transition_id, from/to state, reason, scan_cycle_id, timestamp).
History is append-only and immutable; the lifecycle snapshot is replaced
by a NEW snapshot (never mutated in place).

## 29. Lifecycle store / repository

`SetupLifecycleStore` — an in-memory, deterministic repository isolated
behind a clear boundary (save / load / delete / list_lifecycles /
active_lifecycles / terminal_lifecycles / active_for_instrument /
active_for_setup / lifecycles_for_instrument / lifecycles_for_timeframe /
lifecycles_for_setup / active_count / terminal_count / total_count /
reset). No file I/O, no database, no global mutable state; injectable and
resettable for tests. 19.8 owns persistence/recovery.

## 30. Idempotency

Processing the same (scan cycle, setup identity, assessment fingerprint)
twice is detected via `_last_processed_fingerprint` per lifecycle and
returns a duplicate result with `observation_status=OBSERVED,
duplicate=True`, recording NO new observation and NO new transition.
`process_universe` on the same universe result twice produces identical
cycle results and lifecycle history. Idempotency is driven by the
deterministic observation ID (lifecycle_id + scan_cycle_id + timestamp +
status + fingerprint) — the SAME input ALWAYS produces the SAME
observation ID.

## 31. Ordering guarantees

Lifecycle processing is deterministic: universe instruments are processed
in canonical order (the 19.5 universe tuple, itself sorted); within a
symbol only one assessment is processed per cycle; dict/set iteration is
never relied on for state; repeated identical runs produce identical
results. Provider/scan/parallel arrival order cannot determine lifecycle
state.

## 32. Out-of-order observation handling

A genuinely out-of-chronological-order observation (an observation
timestamp STRICTLY older than the lifecycle's `last_observation_timestamp`
and not already recorded from a prior cycle) is REJECTED: the processing
result returns `late_rejected=True` with
`observation_status=LATE_REJECTED`, and NOTHING is persisted — no
observation record, no transition, no state change. This is the smallest
architecture appropriate for 19.6 (reject-late rather than event-sourcing
replay); the behavior is explicit and tested. A repeated observation of
the SAME cycle is handled by idempotency (§30) before the late check, so
replaying an earlier cycle never corrupts state.

## 33. Point-in-time safety

Lifecycle decisions at time T use only: (a) lifecycle history with
observation_timestamp <= T, (b) the 19.5 assessment at T, (c)
information available at T. No future assessment can alter an earlier
state; no retroactive mutation of past observations; history is an
accurate record of what was known at the time.

## 34. Look-ahead protection

The engine never calls wall-clock time, never reads candles, never
fetches data, and never consumes a 19.5 result produced at a later
timestamp. Tests prove: appending T2/T3 observations leaves the T1 state
and history byte-identical; future confirmation can never retroactively
confirm an earlier DETECTED state; future invalidation cannot alter
historical state.

## 35. Explainability

Every lifecycle carries `explanation` and `reason` strings; every
observation and transition carries an explicit reason; the config
documents every threshold; the formatter renders per-observation and
per-transition reasons. No STATE_CHANGED-without-why ever occurs.

## 36. Determinism

Identical (prior history, scan cycle, assessment, timestamp, config)
always produces identical state. No randomness, no implicit wall-clock,
no provider-order dependence, no network dependence. All IDs are
sha256-based. Repeated runs and shuffled-equivalent inputs produce
identical outputs.

## 37. Universe-wide processing

`process_universe(SetupQualityUniverseResult, scan_cycle_id, timestamp)`
processes every per-symbol result in the 19.5 universe, resolves
identities, creates/updates lifecycles, and produces a
`SetupLifecycleCycleResult` with deterministic counts (created, advanced,
duplicate, late_rejected, observed, absent, data_unavailable, superseded,
detected, confirmed, invalidated, expired, active, terminal) and a
deterministic `lc-cycle-<sha256[:16]>` cycle ID.

## 38. Failure isolation

Per-symbol and per-assessment failures are isolated: a malformed or
exception-raising assessment result is caught and recorded as an error
entry (cycle continues); one symbol's failure never terminates the rest
of the universe. Tested.

## 39. Relationship to alerts

19.6 exposes deterministic transition events (SetupLifecycleTransition)
that a future 19.7 alert layer can consume. 19.6 itself NEVER sends
Telegram/WhatsApp/email/push/SMS — tested by keyword + no-transport
source audit.

## 40. Relationship to trade planning

No entry/stop/target/position sizing/risk-reward/execution fields exist
in the lifecycle models. The lifecycle concerns the setup's analytical
existence, not order execution. No relationship to Product Phase 4/5.

## 41. Broker-execution boundary

Zero broker/execution imports in all new modules (AST-audited, tested).
No order placement, no broker APIs, no portfolio/position management, no
automated entries/exits. Checkpoints 13-18 remain frozen; the execution
gate remains DISABLED.

## 42. Reliability boundary

No persistence, no recovery, no restart handling, no retries, no
watchdogs, no health checks, no metrics, no crash recovery — 19.8 owns
these. The in-memory store is the only storage.

## 43. Forward-testing boundary

No forward-testing/backtesting framework was created — 19.9 owns that.

## 44. Files created

- src/engine/config/setup_lifecycle_config.py
- src/engine/models/setup_lifecycle.py
- src/dashboard/setup_lifecycle.py
- src/engine/reporting/setup_lifecycle.py
- scripts/analyze_setup_lifecycle.py
- scripts/test_checkpoint_19_6.py
- tests/test_setup_lifecycle.py
- tests/test_setup_lifecycle_cli.py
- docs/checkpoint_19_6_setup_lifecycle_audit.md (this document)

## 45. Files modified

None (AGENTS.md appended only; no existing source/test modified).

## 46. Tests added

- tests/test_setup_lifecycle.py — 106 deterministic, offline tests
  covering identity, initial state, transitions, quality changes, data
  failures, missing observations, disappearance/expiration, idempotency,
  ordering, time, point-in-time/look-ahead, universe-wide, lifecycle
  retrieval, boundaries (no broker / no alert / no 19.8 / no 19.9 / no
  trade-plan), and 12 adversarial scenarios.
- tests/test_setup_lifecycle_cli.py — 11 CLI tests (exit codes, JSON
  purity/determinism, timestamp validation, no buy/sell/alert language,
  disclaimer).

## 47. Focused test results

- tests/test_setup_lifecycle.py — 106 passed
- tests/test_setup_lifecycle_cli.py — 11 passed
- Demo scripts/test_checkpoint_19_6.py — 29/29 checks passed
- Lint (pyflakes) CLEAN for all new modules + scripts + tests.

## 48. Regression results

- 19.x combined (19.1-19.6 focused suites): 484 passed (was 367 at 19.5;
  +117 net new = 106 lifecycle + 11 CLI).
- Trading-intelligence (11O-11U reuse): 522 passed.
- Provider/dashboard/data: 480 passed.
- Dashboard: 98 passed.
- Full suite: 6790 passed / 12 skipped / 2 warnings (compared with the
  19.5 baseline of 6673 passed / 12 skipped / 2 warnings; +117 net new,
  exact match). Pipeline baseline unchanged (signals=4, trades=3).

## 49. Adversarial lifecycle test results

All 12 adversarial scenarios pass: same setup across multiple cycles;
significant score changes; temporary disappearance via provider failure;
temporary disappearance via MTF incompleteness; future confirmation
after an earlier observation; future invalidation after an earlier
observation; similar setup after terminal; out-of-order observations;
same cycle processed twice; two instruments with identical assessments;
provider result ordering changes; universe ordering changes.

## 50. Known limitations

- The in-memory store is not persistent (19.8 owns persistence/recovery).
- Late observations are recorded without retroactive recomputation (the
  smallest 19.6-appropriate choice; documented in §32).
- Identity quirks: an opposing direction invalidates + creates a new
  lifecycle; setup_type changes create a new identity (documented in
  §27).
- Expiration is a clean-cycle counter, not a wall-clock rule (justified
  in §24).
- The lifecycle layer does not yet expose a persistence/observability
  surface (19.8).

## 51. Confirmation that 19.7-19.9 were NOT implemented

Confirmed. No alerting/notification code, no reliability/recovery code,
no forward-testing code was created anywhere in Checkpoint 19.6.

## 52. Final architectural assessment

The 19.6 layer is a clean downstream consumer of the frozen 19.1-19.5
chain, adds deterministic setup identity + lifecycle semantics, reuses
existing evidence and conventions, preserves point-in-time safety and
failure isolation, exposes auditable history and transition events for
19.7, and introduces zero execution semantics.

## 53. Final verdict

PASS — Checkpoint 19.6 is complete. The lifecycle boundary is clean and
safe for 19.7 (user alerts) to consume.