# CHECKPOINT 19.8 — RELIABILITY / RECOVERY / OBSERVABILITY (audit document)

- Status: COMPLETE / **PASS**.
- Scope: OPERATIONAL HARDENING layer that WRAPS the FROZEN 19.1–19.7 continuous
  intelligence pipeline. 19.8 answers: *"Can this system continuously operate,
  detect failures, recover safely, and tell the operator what is happening
  without corrupting analytical state?"*
- **BROKER EXECUTION REMAINS COMPLETELY DEFERRED THROUGHOUT CHECKPOINT
  19.x** (Checkpoints 13–18 frozen; execution gate DISABLED). No 19.9 work
  started; nothing committed to git.
- Full audit: 56 sections below.

---

## 1. Objective

Build an operational reliability / recovery / observability layer around the
existing 19.1–19.7 pipeline: process health, scanner health, cycle health,
provider health, per-symbol failure isolation, alert-delivery health, bounded
retry/backoff/timeouts, graceful shutdown, single-instance protection,
deterministic health classification, structured logging, durable operational
state for restart-safe recovery of alert/lifecycle identities, and an offline
operator diagnostic CLI. 19.8 MUST observe and protect analytical execution —
it must NEVER silently change analytical meaning.

## 2. Scope

- Operational failure taxonomy (explicit; distinct from analytical states).
- Reliability configuration (retry/backoff/timeout/heartbeat/health thresholds).
- Heartbeat (process-alive vs progress-alive; explicit; testable).
- Retry runner (bounded, category-gated, deterministic backoff).
- Timeout boundary (bounded_call).
- Per-symbol failure isolation (200-symbol accounting preserved).
- ReliabilityTracker (cycle/provider/delivery/event observation + health
  derivation + recovery + restart recovery + durable snapshot).
- Single-instance guard (fail-closed, stale-marker recoverable).
- Structured logging (level discipline, stable fields, deterministic ids).
- Alert outbox (delivery reliability; retry preserves the original alert id;
  delivery failure never mutates lifecycle state).
- Durable OperationalStateStore (atomic, schema-versioned, crash-safe,
  corruption fail-closed).
- Health reporting formatter + operator CLI (`scripts/check_system_health.py`).
- Comprehensive deterministic offline tests + demo.
- Audit document + AGENTS.md append.

## 3. Existing reliability architecture discovered (audit)

A full repository/architecture audit was performed BEFORE any implementation:

1. Logging framework: NO project logging framework existed (only `print()`
   in CLIs/demos).
2. Structured logging: NONE existed.
3. Log schemas: NONE existed.
4. Process/supervisor: the only process-management was the external Windows
   Task Scheduler smoke-check script sidecars for the paper-trading CLI
   (`scripts/windows/keep_awake*`) — never the 19.1–19.7 scanner pipeline.
5. Heartbeat: NONE existed.
6. PID files: `keep_awake_manager.py`/`keep_awake.py` (Windows smoke-check
   sidecar) used a marker file; the 19.x scanner had NO pid file / single
   instance mechanism.
7. Keep-awake/watchdog scripts: `scripts/windows/keep_awake*.py` are
   WINDOWS-KEEP-AWAKE utilities for a live session; they are NOT scanner
   reliability infrastructure and were NOT reused for 19.8.
8. Retry utilities: NONE existed (the Yahoo live provider failed gracefully
   but never retried).
9. Backoff utilities: NONE existed.
10. Timeout handling: `bounded_call` did not exist; `urllib` timeouts were
    provider-internal only.
11. Exception boundaries: 19.2/19.3 classify per-symbol failures inside the
    cycle result (`PROVIDER_ERROR`, `UNSUPPORTED_*`, `INVALID_RESPONSE`,
    `EMPTY`, ...) — never raised. Cycle-level exceptions existed as a typed
    per-boundary capture.
12. Persistence/state stores: many schema-versioned atomic JSON stores
    (PaperTradeStore, OperationalStateStore-style) across the repo; the 19.x
    scanner/lifecycle/alert stores were in-memory only.
13. Checkpoint/state recovery: NONE for the scanner/lifecycle/alert path.
14. Startup/shutdown handling: 19.3 `ContinuousScanner.start()/stop()/join()`
    with a cooperative stop; no durable flush.
15. Signal handling: NONE (only the Windows sidecar used OS-specific hooks —
    not reusable off-Windows).
16. Health checks: `GET /health` / `GET /api/health` on the dashboard expose
    data-provider status (not a scanner-operational health model).
17. Metrics/counters: cycle metadata in 19.3 was analytical cycle bookkeeping;
    NO operational counters existed.
18. Operational diagnostics: NONE. The `scan_market` CLI shows one cycle's
    per-symbol results.
19. Dashboard health/status endpoints: yes for data providers
    (`/api/health`), but no operational-health model.
20. Session monitoring: 19.2 `market_session` (deterministic IST session
    arithmetic) existed and was reused.
21. Paper-trading reliability: the PaperTradingOperations runner + persistence
    existed; NOT an analytics-path reliability layer; not reused.
22. Data-provider retry behavior: NONE (fail-fast per symbol; isolated).
23. Alert-delivery behavior: 19.7 `AlertEngine` -> `AlertDeliveryChannel` with
    terminal DELIVERED/FAILED/SKIPPED/SUPPRESSED statuses; NO retry existed.
24. Scan lifecycle behavior: 19.3 `ContinuousScannerEngine.run_cycle` +
    `ContinuousScanner` start/stop with no-overlap and a state machine.
25. Configuration conventions: frozen+slots dataclasses per checkpoint with
    validation at construction (19.4+ convention).
26. CLI conventions: `scripts/*.py` thin operator CLIs; `--json` pure JSON;
    exit codes 0 (ran) / 1 (runtime) / 2 (bad args); default offline.
27. Test conventions: deterministic; injected clocks/waiters/providers;
    frozen+slots models; full-path imports; AST boundary tests; offline.

## 4. Existing logging architecture

None. The repository never used a logging framework. CLIs print human
reports; engines/formatters return strings. No structured events existed.
19.8 therefore introduces a SMALL StructuredLogger module rather than a
dependency on any external logging package.

## 5. Existing process-management architecture

- 19.3 `ContinuousScanner` owns ONE background worker thread with a state
  machine (STOPPED/RUNNING/SCANNING/WAITING/STOPPING/FAILED), a cooperative
  stop, and a no-overlap policy. No supervisor, no pid file, no single
  instance lock, no heartbeat.
- External Windows Task Scheduler runs the paper-trading CLI on a schedule
  (outside the 19.x pipeline); not reused.

## 6. Existing persistence/state mechanisms

- The repository has established atomic filesystem JSON stores
  (tempfile.mkstemp + fsync + os.replace; safe-id regex; schema versioning;
  typed exceptions). 19.8 follows the SAME pattern for operational state.
- THE 19.1–19.7 scanner/lifecycle/alert stores are IN-MEMORY (deterministic
  repositories). 19.8 is the FIRST checkpoint permitted to introduce durable
  OPERATIONAL state (limited to the identity/dedup snapshot needed for
  restart-safety).

## 7. Existing retry mechanisms

None. Provider/delivery failures were fail-fast per-symbol / per-alert.
19.8 introduces a minimal bounded, category-gated, deterministic retry
runner for transient provider + delivery failures. No infinite loops; no
blind retry of deterministic failures (malformed data, invalid config,
unsupported instrument, validation failures).

## 8. Existing health/diagnostic mechanisms

- Dashboard `/api/health` exposes provider data-source status (49.x/19.2
  era). It does NOT model scanner/cycle/provider/delivery operational health.
- 19.3 cycle metadata carries analytical counts. 19.8 adds the OPERATIONAL
  health model ON TOP (never modifying 19.3 semantics).

## 9. Components reused

- 19.1 `UniverseBuilder`/`NIFTY200_SYMBOLS` — canonical universe for the
  health CLI + scanner cycles.
- 19.2 `IntradayCoverageEngine`/`DashboardDataProvider`/`market_session`
  session arithmetic / `split_completed_candles` / freshness semantics — the
  CLI + tracker consume these; the reliability layer never re-derives
  sessions.
- 19.3 `ContinuousScannerEngine`/`ContinuousScanner` — cycle execution +
  cycle identity + no-overlap + state machine (unchanged, used in tests and
  the demo); `MarketScanStatus`, `ContinuousScanConfig`, `ScanSourceKind`.
- 19.4 `MtfAnalysis` — NOT directly consumed by 19.8 (the reliability layer
  is engine-agnostic); the demo exercises the full 19.2->19.3 flow which
  leads to 19.4/19.5 outputs through the fixtures.
- 19.5 `SetupQuality` — consumed only as carried fields on the lifecycle/
  alert cycle objects (by reference), never recomputed.
- 19.6 `SetupLifecycleEngine`/`SetupLifecycleStore`/`build_setup_id` —
  lifecycle cycle objects consumed for alert/lifecycle observation; setup id
  identity rules reused.
- 19.7 `AlertEngine`/`AlertEvent`/`AlertDeliveryChannel`/`ConsoleAlertSink`/
  `AlertDeliveryStatus` — alert generation + delivery abstraction reused
  verbatim; the outbox integrates WITH the 19.7 channel abstraction.
- 19.2/19.3 deterministic fixtures (`_checkpoint19_8_fixtures.py` reuses the
  19.6 fixture helpers for lifecycle/alert inputs).

## 10. Components intentionally not reused (and why)

- `keep_awake*.py` / Windows scheduler sidecars: OS-specific live-session
  utilities, not scanner reliability infrastructure; not reusable, not imported.
- Sprint 11W `OutcomeEvaluator` / Sprint 11F `HistoricalEvaluationPipeline`:
  forward-only outcome evaluation / historical pipeline — the reliability
  layer must never touch candles or forward windows; deliberately not used
  (tests patch them to raise to prove the chain never calls them).
- Checkpoint 13–18 execution/submission/broker layers: frozen + deferred;
  never imported (AST-checked).
- 19.7 `AlertStore` durability: 19.7's in-memory store stays in-memory;
  19.8's store persists only the id-identity subset required for restart
  safety (the full alert/lifecycle state remains regenerable from the
  analytical stream).
- Sprint 11K/11L experiment/registry/query persistence: unrelated domain,
  different schema; not reused.

## 11. Reliability architecture

```
Analytical pipeline (19.1–19.7)
        │  events (cycle result / lifecycle cycle / alert cycle) flow OUT
        ▼
ReliabilityTracker ── observes ──┬─ scan cycles        (cycle health)
                                 ├─ provider results   (provider health)
                                 ├─ lifecycle cycles   (setup identity)
                                 ├─ alert cycles       (alert identity)
                                 ├─ heartbeat          (process/progress)
                                 └─ retries/recovery captions
        │
        ├─ ReliabilityTracker.health() -> OperationalHealthReport
        ├─ startup_recovery()/shutdown()  <->  OperationalStateStore (durable)
        ├─ SingleInstanceGuard (fail-closed single instance)
        ├─ RetryRunner + TimeoutPolicy + HeartbeatPolicy
        └─ StructuredLogger (stable fields, level discipline)

AlertEngine (19.7) -> AlertEvent -> AlertOutbox -> AlertDeliveryChannel
        └─ outbox retries PRESERVE the original alert_id (never a new alert)
```

The tracker keeps OPERATIONAL state strictly separate from ANALYTICAL state:
the lifecycle/alert objects are observed and referenced (id sets, counts),
never mutated; the durable snapshot persists the identity/dedup subset only.

## 12. Operational-state model

- `OperationalStateBundle` — one durable snapshot: snapshot_id, recorded_at,
  heartbeat_at, policy_version, provider_health, cycle summary/counts,
  last_cycle_id/last_cycle_at, alert counts, lifecycle set_ids, alert_ids,
  recovery events. Full model in `engine/models/operational_health.py`.
- `OperationalStateStore` — durable, atomic, schema-versioned
  (`OPERATIONAL_STATE_SCHEMA_VERSION = 1`), idempotent-on-identical,
  conflict-rejecting (unless overwrite), corruption fail-closed.
- `ReliabilityTracker` — in-memory observer aggregating the operational
  facts; `snapshot()`/`startup_recovery()`/`shutdown()`.

## 13. Failure taxonomy

`ReliabilityFailureCategory` (explicit; distinct from analytical vocabulary):

`PROVIDER_TIMEOUT`, `PROVIDER_UNAVAILABLE`, `INVALID_RESPONSE`,
`DELIVERY_FAILURE`, `CYCLE_FAILURE`, `HEARTBEAT_STALE`, `STALLED_SCANNER`,
`RECOVERY_FAILURE`, `CONFIGURATION_FAILURE`, `PROCESS_FAILURE`, `UNKNOWN`.

Analytical states (`DATA_UNAVAILABLE`, `CONFIRMED`, `HIGH`, `ALIGNED`,
`QUALIFIED`, `DELIVERED`, `SUPPRESSED`, `FULL_SUCCESS`, ...) are NOT part of
the failure taxonomy and are NEVER conflated with it (tested). `UNKNOWN`
round-trips and maps to non-retryable (operational hygiene).

## 14. Health model

`OperationalHealthState`: `HEALTHY` / `DEGRADED` / `RECOVERING` / `FAILED`
(+ `UNKNOWN` for a never-instrumented aggregation).

Domain health (`HealthStatus`): `HEALTHY` / `DEGRADED` / `FAILED` / `UNKNOWN`
per domain (provider, scanner, delivery), derived SOLELY from observable
operational facts:

- provider: consecutive failures >= degradation_threshold -> DEGRADED;
  >= (2 * degradation_threshold) -> FAILED; consecutive successes >=
  recovery_threshold -> HEALTHY/RECOVERING.
- scanner: stalls/heartbeat-stale/failed cycles count; a live-but-stalled
  process is NEVER healthy (no naive "PID exists = healthy" rule).
- delivery: failed deliveries / retries-exhausted count; FAILED after
  threshold.

Aggregation: `FAILED` wins, then `RECOVERING` (recovery pending), then
`DEGRADED`, else `HEALTHY`. Health is NEVER derived from setup quality.

## 15. Scanner health

Driven by cycle observation (`record_cycle`) + heartbeat + stall detection:
no cycle started yet + fresh heartbeat -> UNKNOWN/HEALTHY-by-absence; a
stale heartbeat or a stuck "no cycle completed for
`max_since_cycle_seconds`" -> STALLED_SCANNER -> FAILED (even when heartbeat
is nominally fresh). A market-closed/no-cycle window alone is NEVER a scanner
failure.

## 16. Cycle health

`CycleHealth`/`CycleFailureSummary`/`MarketScanCycleResult` observation:
started/ended/duration/requested/succeeded/failed/unavailable/skipped counts;
cycle status (FULL/PARTIAL/COMPLETE_FAILURE/SKIPPED preserved from 19.3);
last_cycle_id + duration exposed. A cycle that overruns its timeout budget is
an operational observation (never an analytical signal).

## 17. Provider health

`record_provider_result(success, provider_name, category)` sustains the
consecutive-failure/success streak; the provider domain health + a provider
name + operation counts are surfaced. A healthy-looking provider stream with
a stale heartbeat is still NOT healthy (aggregate downgrade).

## 18. Symbol failure isolation

FROZEN 19.3 semantics preserved and proven: 200 requested -> 1 failure ->
cycle completes with 199 successes + 1 isolated failure + explicit
accounting; 50 failures -> 150 continue; all-unavailable -> COMPLETE_FAILURE
with zero fabricated data. The tracker records per-symbol failures with
symbol/category/timestamp/attempts (bounded). A single failing symbol can
never terminate the Top-200 cycle.

## 19. Heartbeat

`HeartbeatTracker` (injected clock): `refresh()`/`state()`; classification
`NONE` (never refreshed) / `FRESH` / `STALE`; naive timestamps fail closed to
STALE; future-dated heartbeat is STALE; `max_quiet_seconds` documented (4 x
interval). The heartbeat answers "alive AND making progress", never "healthy"
alone — the stall classification uses the last-COMPLETED-cycle anchor.

## 20. Stale / hung detection

- no cycle started yet + stale heartbeat -> STALE -> FAILED
- cycle running too long -> overrun observation (TimeoutPolicy) + hung
  flag (never auto-starts parallel cycles)
- scanner stopped unexpectedly -> FAILED state (`STALLED_SCANNER`)
- heartbeat stale -> FAILED
- repeated provider failures -> provider DEGRADED/FAILED
- repeated delivery failures -> delivery DEGRADED/FAILED

All thresholds live in `ReliabilityConfig` and are documented; none are
arbitrary production guesses. Market-closed periods are distinguished by the
2.2 session helper (never a false alarm).

## 21. Market-session interaction

19.8 reuses `engine.data.market_session` helpers for the CLI banner and for
market-session context; it does NOT gate health solely on session state. The
absence of a cycle during a closed/Weekend period never auto-fails the
scanner; process-level health (heartbeat) remains observable regardless.
The measured "no cycle on weekend" case is HEALTHY (tested).

## 22. Retry policy

`RetryPolicy` (bounded, deterministic, category-gated):

- `max_attempts` (total incl. first; 1 disables retry), `base_delay_seconds`,
  `backoff_factor` (>=1; multiplicative backoff), `retryable_categories`
  (default: PROVIDER_TIMEOUT / PROVIDER_UNAVAILABLE / DELIVERY_FAILURE).
- Retryable: transient provider timeout / temporary network failure /
  delivery failure.
- NOT retried: malformed data, invalid configuration, unsupported
  instrument, deterministic validation failures (immediate fail).
- No infinite loops: `max_attempts` hard-caps; `RetryExhausted` raises with
  the attempt count. Tests prove no blind retries of a non-retryable
  category.

## 23. Backoff policy

Deterministic multiplicative backoff `delay_for(n) = base_delay * factor**n`
with NO random jitter (deterministic CI behavior). Delay sweep is exact and
tested. `sweep()` returns the exact per-attempt delay tuple.

## 24. Timeout policy

`TimeoutPolicy`: `provider_operation_seconds` (15s default),
`cycle_operation_seconds` (300s), `delivery_operation_seconds` (5s). All
optional; `bounded_call` runs an operation with a deadline; a hung operation
becomes `OperationTimedOut` (an OPERATIONAL failure classification, NEVER an
analytical signal). One hung provider operation is bounded and isolated;
the pipeline continues or recovers per policy (tested).

## 25. Recovery policy

Explicit recovery, observable:

- provider failure -> DEGRADED -> retry -> success streak >= recovery
  threshold -> RECOVERING -> HEALTHY (all states recorded via
  `mark_recovery_pending` + `record_recovery`).
- recovery is never hidden: `record_recovery(message, success)` creates a
  `RecoveryEvent` surfaced in the report + failure counts.
- a recovery failure (e.g. corrupt persisted state on restart) FAILS CLOSED
  (never silently invents analytical state).

## 26. Process restart behavior

`startup_recovery(scanner_state, at)`:

- no durable state + stopped scanner -> "fresh start" (no fabricated ids);
- valid durable state -> RESTORES the identity/dedup snapshot; setup/alert
  ids survive; the analytical stream (lifecycles/alerts) is NOT re-created
  (no duplicate analytical identities — unchanged identity is reused);
- corrupt durable state -> recorded as a RECOVERY_FAILURE -> health FAILED.

`shutdown()` flushes the snapshot (when a store is configured) and returns
it. Persistence is OPT-IN (a tracker without a store operates fully
in-memory; restart-safety then relies on regenerating identity from the
deterministic analytical stream).

## 27. Persistent-state design (implemented)

- OWNERSHIP: `ReliabilityTracker` (producer) -> `OperationalStateStore`
  (durable, atomic) -> snapshot schema `OPERATIONAL_STATE_SCHEMA_VERSION=1`.
- WRITE: atomic same-dir temp + flush + fsync(best-effort) + os.replace;
  idempotent identical writes; conflicting content -> OperationalStateIntegrityError
  unless `overwrite=True`.
- READ: schema checked BEFORE reconstruction; future/missing schema rejected;
  corrupted payload -> OperationalStoreError (fail closed).
- VERSIONING: schema_version + policy_version (default
  `25e4d9befd844b11` embedded); deterministic snapshot/serialized bytes.
- RESTART: load-on-start-up (opt-in), idempotent; no automatic analytical
  state creation.
- ATOMICITY/IDEMPOTENCY/RECOVERY: tested (see §27 crash-consistency).

## 28. Crash consistency

Crash-like scenarios tested deterministically:

- crash before first write: `load_snapshot()` -> None -> fresh start.
- duplicate identical write: idempotent no-op.
- conflicting write: rejected (typed error) unless overwrite.
- partial/corrupted write: fail closed (never a fabricated bundle).
- restart with valid state: identity/alert ids restored exactly.
- restart with missing state: fresh start with no invented identity.
- future schema: rejected before reconstruction.

No claim of crash safety is made beyond what these tests prove (documented
honest scope).

## 29. Alert-delivery reliability

`AlertOutbox` integrates with the frozen 19.7 `AlertDeliveryChannel`
abstraction. On `process(now)`:

- pending entries are DUE checks (deterministic, injected clock);
- DUE entries are delivered through the provided channels;
- DELIVERY FAILURE does NOT mutate the alert or lifecycle (no new analytical
  setup; alert_id unchanged);
- failures are retried according to the RetryPolicy but NEVER become new
  alerts;
- budget exhaustion -> terminal FAILED entries (explicit, auditable);
- no channels -> SKIPPED (explicit, never silent);
- delivered/failed/pending counts + delivered/failed entry lists exposed.

## 30. Alert retry semantics

The outbox stores the ORIGINAL `AlertEvent` (identical alert_id,
observation_id, setup_id, payload) and retries THAT object. `RetryRecord`
carries `alert_id` so retry/attempt accounting is bound to the original
alert. Repeatedly retrying Alert A 20 times produces at most one alert
record (budget-exhausted FAILED entry with the same alert_id) — never
Alert B, never duplicate analytics.

## 31. Duplicate-alert protection

19.7's identity-based dedup is preserved: the outbox enqueues by the fixed
alert_id emitted by the frozen 19.7 engine; re-processing the same lifecycle
cycle dedups (no duplicate alert); retries never create new ids; the outbox
dedups its own queue by outbox_id/alert_id.

## 32. Outbox design (implemented — offline deterministic abstraction)

```
Lifecycle Event -> AlertEvent (19.7) -> Outbox (pending, durable-in-memory)
-> Delivery (channel) -> Delivery Status
```

The outbox is inline (in-memory) and OFFLINE (default console sink). It
proves reliability semantics WITHOUT any external notification provider. If
a delivery transport is added later it plugs in behind the frozen 19.7
channel abstraction. The outbox NEVER alters lifecycle semantics.

## 33. Graceful shutdown

- 19.3 `ContinuousScanner.stop()` is a cooperative request: no new work
  after shutdown begins; the in-flight cycle finishes; the worker exits.
- Tests: start -> stop -> start -> stop leaves no orphan worker (worker
  thread joined, state STOPPED), duplicate stop idempotent, restart after
  stop keeps cycle counting from the new schedule.
- `ReliabilityTracker.shutdown()` flushes durable state (when configured).

## 34. Signal handling

No project convention for in-process OS signals; 19.8 keeps signal
integration thin (documented). Core logic is directly testable without real
OS signals; tests never depend on OS-specific behavior. OS signal wiring
remains an operator-level integration (outer CLI wrapper), not part of the
deterministic core.

## 35. Single-instance protection

`SingleInstanceGuard(instance_label, marker_store, is_pid_alive)`:

- explicit + observable; a second live acquire raises (fail closed) even
  with `force=True`;
- a STALE marker (pid not alive) is recoverable (replace) — never relied on
  alone; the pid-liveness checker makes stale detection deterministic and
  testable;
- release() clears the marker; `is_held`, `lock_info()` for observation;
- a shared marker store is REQUIRED for multi-instance tests (the default
  is a per-process in-memory store; a live second process can only be
  detected when it shares durability — documented limitation, tested).

## 36. No-overlap preservation

The frozen 19.3 no-overlap policy is untouched. 19.8 never launches
parallel cycles and never a second scanner; if a cycle exceeds its expected
duration the reliability layer records overrun/stall information instead of
launching unbounded parallel cycles (tested: distinct reference times for
back-to-back cycles; scanner.stop during a cycle completes the cycle).

## 37. Observability

`OperationalHealthReport` surfaces: health_state, scanner_state, per-domain
health, heartbeat time/state, cycle counts, last_cycle_id + time + duration,
provider name/status/counts, delivery counts, per-symbol failure count,
retry counts, recovery events, failure counts, policy_version, snapshot
identity, rationale. The formatter renders a deterministic text report +
JSON. No full observability platform is built (repo convention: formatter +
CLI).

## 38. Structured logging

`StructuredLogger(component, sink, clock)` + `StructuredEvent`:

- stable fields: timestamp, component, event, level, cycle_id, setup_id,
  alert_id, symbol, status, duration, error_category, retry_count,
  recovery_state (+ detail);
- deterministic event ids (`log-` + sha256 prefix);
- levels DEBUG<INFO<WARNING<ERROR<CRITICAL with rank ordering and
  `level_at_least` filtering;
- sinks: `ListLogSink` (deterministic in-memory; test seam) and a
  `PrintLogSink` (default human output);
- NO unstable object representations (no `repr()` object dumps in lines);
- expected per-symbol partial failures are logged at WARNING, not CRITICAL.

## 39. Error handling

No broad `except Exception: pass`. Every swallowed exception has a documented
reason + observable handling (per-symbol isolation, cycle-boundary capture,
recovery-failure capture, flush-failure capture). Unexpected infrastructure
failures remain visible (operator CLI + report + structured events).

## 40. Configuration validation

`RetryPolicy` / `TimeoutPolicy` / `HeartbeatPolicy` / `HealthThresholds` /
`ReliabilityConfig` validate at construction (types + bounds + ordering);
unknown category names and duplicates are rejected; invalid scan
interval / timeout / retry count / backoff / persistence options fail
closed. No unsafe default substitution.

## 41. Fail-open / fail-closed decisions

- Invalid configuration: FAIL CLOSED (construction raises).
- Unsupported instrument/timeframe: explicit unsupported, NOT retried.
- Temporary provider timeout: retried per policy.
- Alert delivery failure: preserve + retry + recover; never a new alert.
- Corrupt persisted state: FAIL CLOSED (never silent fabrication).
- Single-instance acquire while live: FAIL CLOSED (even with force).
- Naive/future-dated heartbeat: FAIL CLOSED to STALE.
- Any recovery failure: FAIL CLOSED (health FAILED).

## 42. Analytical vs operational state

- ANALYTICAL: setup quality, lifecycle state/history, alert payloads —
  immutable, frozen+slots, NEVER rewritten by reliability code.
- OPERATIONAL: cycle/provider/delivery counts, heartbeat, retries, recovery
  events, health — mutable operational facts, persisted separately.
The two vocabularies are never merged (tests assert the taxonomy does not
overlap analytical enums; health is never derived from setup quality).

## 43. Time-source semantics

1. Analytical/event time: from upstream event/data timestamps (lifecycle
   observation timestamps, alert observation timestamps, scan-cycle
   reference times) — NEVER wall-clock.
2. Market/session time: from `market_session` helpers (deterministic IST).
3. Operational wall-clock time: only for operational metadata (heartbeat
   refresh instants, retry delays, durations) — injected clock always
   (default `datetime.now(UTC)` fallback in the TRACKER only; engines and
   models never call wall-clock; setup/alert ids and analytical scores are
   NEVER derived from wall-clock; tested).

## 44. Point-in-time protection

- The reliability layer never feeds future information into analytical
  state: lifecycle/alert objects are consumed by reference, never mutated;
  prior results are immutable.
- Synthetic "future" observations/cycle/alert/MTF input cannot change
  earlier state (tested).
- Setup/alert identity uses NO wall-clock (deterministic; tested).

## 45. Determinism

- All models frozen+slots; injected clocks/waiters/providers; scripted
  failures; deterministic retry/backoff; deterministic stores.
- Same inputs -> same health ids/versions/serialized bytes; shuffle-invariant
  aggregation; no wall-clock in analytical identity.
- The 19.8 default `policy_version` = `25e4d9befd844b11` (deterministic
  sha256-prefix of the canonical config snapshot).

## 46. NIFTY Top-200 behavior

- CLI default universe = `UniverseBuilder.nifty200()` (exactly the 200
  official NIFTY constituents, frozen 19.1 manifest).
- Per-symbol failure isolation proven for N large-N cases (200-style
  accounting); provider/alert retry budgets bounded; no unbounded
  concurrency; no hidden slow cycles; reliability overhead does not silently
  drop symbols or alerts (cycle requested == sum of the per-symbol result
  buckets, tested).

## 47. Operator diagnostics

`scripts/check_system_health.py` (offline/deterministic default):

- default provider `fixture`, deterministic reference time; `--json` pure
  machine JSON; `--instruments` (default Top 200); `--timeframes`
  (default 15m); `--state-dir`; `--load-state`; `--reference-now` (must be
  timezone-aware, else exit 2); `--provider`.
- Exit 0 = run completed with honest health findings; 1 = runtime failure;
  2 = bad args.
- Report: health state, provider/fixture availability, freshness, universe,
  cycle/retry/recovery/alert counts, rationale + the standard disclaimer
  (descriptive, no prediction, no broker execution).
- No credentials required.

## 48. Tests added

- `tests/test_reliability.py` — 106 deterministic offline tests.
- `tests/test_check_system_health_cli.py` — 11 CLI subprocess tests.
- `scripts/test_checkpoint_19_8.py` — demo (87 checks).
- Shared deterministic fixtures: `tests/_checkpoint19_8_fixtures.py`
  (lifecycle/alert cycle builders reusing the 19.6 fixture helpers).

## 49. Focused reliability tests

106 tests across 15 areas: A config; B failure taxonomy; C heartbeat;
D retry runner; E timeout boundary; F single-instance guard; G
market-session interaction; H per-symbol failure isolation; I health
derivation; J structured logging; K alert outbox; L persistence/crash
consistency; M scanner graceful restart/no-overlap; N point-in-time; O AST
broker/execution boundary. 106 passed / 0 failed.

## 50. Adversarial tests

- 200 symbols with 1 failure; 50 failures; all-unavailable (engine-level =>
  COMPLETE_FAILURE); one provider op hangs (bounded_call -> OperationTimedOut);
  repeated provider timeout; repeated delivery failure; same alert retried 20
  times stays alert A; same cycle processed twice (dedup); scanner stopped
  during a cycle; scanner restarted immediately; stale heartbeat; stale
  lock; corrupt state; missing state; invalid config; market closed;
  weekend; long-running cycle; back-to-back cycles; multiple restart cycles;
  two instances attempted simultaneously; unexpected exception in one symbol;
  unexpected exception in alert delivery; recovery after multiple failures;
  future analytical data cannot mutate previous state. All pass.

## 51. Regression results

- 19.1–19.7 combined frozen suites: **590 passed** (12.70s).
- Trading-intelligence (11O–11U): **522 passed**.
- Provider/dashboard/data: **905 passed** (2 pre-existing third-party
  deprecation warnings).
- Full pytest suite: see below. Baseline 19.7 = 6896 passed / 12 skipped /
  2 warnings. 19.8 adds 106 + 11 = 117.
- No unrelated failures; no weakened assertions; no test was modified solely
  to make failures disappear; no analytical/lifecycle/alert regression.

## 52. Known limitations

- The OperationalStateStore is OPT-IN; without a configured store the
  level of durability is analyst-stream regeneration (identity rules are
  deterministic so restart does not duplicate identity).
- Single-instance live-detection requires a SHARED marker store; the default
  is in-memory (documented as a limitation for multi-process deployments —
  an operator deploying multiple OS processes must share a store).
- The retry runner uses Wall-Clock-free delays (injected clock + waiter);
  the real default waiter is `time.sleep` (CLI/operator mode only).
- No external telemetry shipping; observability is the formatter + CLI
  (repo convention; a full observability platform is out of scope).
- In-process signal wiring is left to the operator wrapper; the core is
  signal-free.
- The heartbeat/stall thresholds are conservative defaults; operators may
  configure them via `ReliabilityConfig`.

## 53. What remains deferred to 19.9

21. 19.9 owns validation, forward testing, live observation, performance
    measurement, setup outcome evaluation and operational effectiveness
    under forward conditions. 19.8 does NOT implement any forward-testing
    framework and does NOT introduce historical-return-based setup
    validation (verified: none of the 19.8 modules import `OutcomeEvaluator`,
    `HistoricalEvaluationPipeline`, or any forward/historical-outcome code).

## 54. Broker-execution boundary

ABSOLUTELY NO order placement / modification / cancellation, position
management, broker execution, automated entry/exit, or broker-triggered
alert execution. The user remains the manual execution boundary.
AST checks in `tests/test_reliability.py` prove the 19.8 modules import
ZERO broker/execution/address/submission modules and ZERO network packages
(requests/httpx/urllib/socket/websocket) and contain no credential literals
(`UPSTOX_EXECUTION_ACCESS_TOKEN` / `UPSTOX_ANALYTICS_TOKEN` / `Authorization:`
/ token env reads).

## 55. Final architectural assessment

- The 19.1–19.7 analytical pipeline remains semantically unchanged
  (identical frozen suites pass; the demo baseline `signals=4/trades=3`
  unchanged).
- Operational failures are explicitly observable (taxonomy + health +
  structured events + CLI).
- Per-symbol failures stay isolated; one failed symbol cannot terminate the
  Top-200 cycle.
- Scanner/cycle/provider/delivery health are each observable and derived
  from observable facts (never from setup quality).
- Retries bounded + deterministic; timeouts bounded where needed; recovery
  explicit and observable; alert retry preserves the original alert id;
  delivery failure never mutates lifecycle state.
- Restart behavior deterministic; required identity survives restart when
  a store is configured; corrupt state fails closed.
- Graceful shutdown + no-overlap preserved; single-instance protection
  fail-closed with stale-marker recovery.
- No wall-clock in analytical identity; no future information can mutate
  prior analytical state.
- NIFTY Top 200 remains the canonical universe; reliability overhead never
  silently drops symbols or alerts; every suppression/recovery decision is
  auditable.
- Broker execution: none. 19.9 forward-testing: none.

## 56. Final verdict

**PASS** — the existing 19.1–19.7 analytical pipeline is semantically
unchanged and regression-safe; operational failures are explicitly
observable and isolated; heartbeat/stale/hung detection is explicit and
testable; market-closed periods never create false scanner alarms; retries
and timeouts are bounded and deterministic; recovery is explicit and
observable; alert retry preserves alert identity and never creates
duplicate analytical alerts; durable operational state is atomic/versioned/
corruption-fail-closed and restart-safe where configured; graceful shutdown,
single instance and no-overlap are preserved; structured logging and the
operator diagnostic CLI give actionable operational information; the failure
taxonomy and health model are explicit; 117 new deterministic offline tests
+ an 87-check demo pass with ZERO regressions across 19.1–19.7 and the
trading-intelligence/provider/dashboard/data suites; the broker-execution
boundary is intact (AST-verified); and the result is architecturally ready
for 19.9 forward validation. **STOP after Checkpoint 19.8; 19.9 is NOT
implemented; broker execution remains deferred.**