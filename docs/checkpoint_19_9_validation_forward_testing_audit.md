# CHECKPOINT 19.9 — VALIDATION / FORWARD TESTING (audit document)

- Status: COMPLETE / **PASS WITH LIMITATIONS**.
- Scope: the FINAL Checkpoint 19 layer. It builds a deterministic,
  point-in-time-safe forward-validation framework that records what the
  completed 19.1–19.8 pipeline observed at each instant and measures what
  the market subsequently did — WITHOUT ever rewriting an earlier
  observation, WITHOUT optimizing any parameter against observed outcomes,
  and WITHOUT any broker / execution path.
- **BROKER EXECUTION REMAINS COMPLETELY DEFERRED THROUGHOUT CHECKPOINT
  19.x** (Checkpoints 13–18 frozen; execution gate DISABLED). No 20.x work
  started; nothing committed to git.
- Full audit: 50 sections below.

---

## 1. Executive summary

Checkpoint 19.9 delivers a complete, deterministic, offline-first
forward-validation framework on top of the FROZEN 19.1–19.8 pipeline:

- a small **forward observation model** (`ForwardObservation`) that captures
  exactly what the system knew at time T (setup identity, direction, setup
  type, primary timeframe, observation timestamp, scan cycle id, lifecycle
  state, setup-quality score/classification, MTF classification, alert state);
- a **forward outcome model** (`ForwardOutcome`) that measures only what
  happened strictly AFTER T, with an explicit measurement window, completion
  rule, direction handling, missing/partial/ambiguous data behavior;
- a **session model** (`ForwardSession`) that records the configuration /
  policy versions that produced the observations;
- a **session manager** with duplicate / out-of-order rejection and
  deterministic identity;
- a **core engine** (`ForwardValidationEngine`) that processes a universe
  cycle (one observation per requested constituent, never silently dropped),
  aggregates setup-frequency / ranking / lifecycle / alert / reliability
  statistics, and produces a descriptive report;
- **schema-versioned atomic persistence** (observation / outcome / session
  stores) with corruption fail-closed;
- an **operator CLI** (`scripts/forward_validation.py`) with `start` /
  `demo` / `report` sub-commands;
- a **13-check demo** and **142 deterministic offline tests** (68 core +
  35 adversarial + 26 persistence + 13 CLI).

The framework is **point-in-time safe by construction**: the outcome engine
consumes ONLY candles strictly after T within a bounded horizon, and the
observation record is immutable. Adversarial tests prove future candles,
future setup confirmation, future BOS, future trend changes, future
support/resistance, future alerts and future lifecycle transitions can never
alter an earlier observation.

**OFFLINE / DETERMINISTIC VALIDATION ONLY.** No live Top-200 forward
validation was performed in this environment (it is an explicit operator
action requiring the Yahoo OPT-IN provider and market-hours data). **LIVE
VALIDATION NOT PERFORMED.** The framework is implemented and locally
validated; live forward validation is documented as an operator workflow.

## 2. Objective

Validate whether the complete 19.x system works correctly and operationally
under FORWARD MARKET CONDITIONS — with strict point-in-time discipline and
NO profitability claim. The objective is engineering validation, not
strategy tuning.

## 3. Scope

- Forward observation model + identity.
- Configuration / policy-version capture (configuration immutability).
- Point-in-time snapshot + future-data exclusion.
- Outcome separation + measurement framework.
- Session model + management.
- Universe accounting (Top-200, no silent drops).
- Setup-frequency / ranking / lifecycle / alert / reliability aggregation.
- Persistence (schema-versioned, atomic, corruption fail-closed).
- Operator CLI + demo + deterministic tests.
- Audit document + AGENTS.md append.

## 4. Frozen architecture

Checkpoints 10–17 and 18.x are FROZEN and UNCHANGED. Checkpoints 19.1–19.8
are FROZEN and regression-safe (verified below). 19.9 is ADDITIVE ONLY: it
consumes the frozen 19.3 `MarketScanCycleResult`, 19.5 `SetupQualityResult`,
19.6 lifecycle events, 19.7 `AlertEvent`, 19.8 operational state — it does
NOT modify any of them.

## 5. Complete 19.x pipeline

```
NIFTY Top 200 (19.1 manifest)
→ Market Data (19.2 intraday coverage / completed-candle boundary)
→ Continuous Scanner (19.3 scan cycles)
→ MTF Analysis (19.4 timeframe states / alignment)
→ Setup Intelligence (19.5 setup-quality score / classification)
→ Ranking (19.5 deterministic ranking)
→ Setup Lifecycle (19.6 identity + state machine)
→ User Alerts (19.7 alert events)
→ Reliability / Recovery / Observability (19.8)
→ Forward Validation (19.9 — THIS layer)
→ YOU → MANUAL EXECUTION
```

## 6. Existing validation infrastructure (audited)

- 19.8 `OperationalHealthReport` / `ReliabilityTracker` / `AlertOutbox`:
  operational reliability vocabulary (HEALTHY/DEGRADED/RECOVERING/FAILED)
  REUSED as the operational-failure vocabulary in the forward report.
- 19.8 `OperationalStateStore` persistence conventions REUSED (atomic
  writes, schema versioning, corruption fail-closed).
- 19.3 `MarketScanCycleResult` scan-cycle identity REUSED as the
  `scan_cycle_id` reference on observations.
- 19.5 `SetupQualityResult` / `SetupQualityClassification` REUSED as the
  carried quality snapshot.
- 19.6 `SetupLifecycleState` / `LifecycleObservationStatus` REUSED as the
  carried lifecycle snapshot.
- 19.7 `AlertEvent` alert identity REUSED as the carried alert state.

## 7. Existing historical / research infrastructure (audited)

- Sprint 11W `OutcomeEvaluator` (trade-level outcome: target/stop/expired/
  both-touched): **INTENTIONALLY NOT REUSED** — it measures trade-level
  geometry outcomes (entry/stop/target) which the 19.x live setup pipeline
  does not produce (no trade geometry in 19.5), and its semantics are
  trade-result semantics, not forward-market-movement semantics.
- Sprint 11X `HistoricalPerformanceStatistics` / 11Y `EvidenceStrength`:
  **INTENTIONALLY NOT REUSED** — they aggregate trade-outcome evidence
  (win rate / realized R / profit factor) over historical corpora; 19.9
  measures forward price movement after a setup observation, a different
  concern, and the 19.9 layer must not inherit trade-profit semantics.
- Checkpoint 9.x `ForwardReturnObservation` / `PriceExcursionObservation`:
  direction-neutral fixed-horizon forward return + max up/down excursion.
  **INTENTIONALLY NOT REUSED** — the Checkpoint 9.x chain is a separate
  historical research pipeline over a different corpus; 19.9 defines its
  own minimal, justified forward-movement metrics (forward return + max
  favorable/adverse movement) with the same spirit but its own vocabulary
  and measurement window, keeping the layer self-contained and
  point-in-time auditable.
- Product Phase 5 `PaperTrade` / `PaperTradingEngine`: **INTENTIONALLY NOT
  REUSED** — paper trading is a trade-lifecycle simulation (entry/stop/
  target/exit); 19.9 is a forward-observation measurement layer and must
  remain semantically independent (the spec explicitly forbids reusing
  paper-trading semantics merely to produce outcome statistics).

## 8. Reused components

| Component | How reused |
| --- | --- |
| 19.1 `UniverseBuilder.nifty200()` / `NIFTY200_SYMBOLS` | canonical universe input |
| 19.2 intraday coverage / completed-candle boundary | data-availability semantics |
| 19.3 `MarketScanCycleResult` | `scan_cycle_id` reference |
| 19.4 `PerSymbolMtfResult` (alignment/completeness) | carried MTF snapshot |
| 19.5 `SetupQualityResult` (score/classification/status) | carried quality snapshot |
| 19.6 lifecycle state + observation status | carried lifecycle snapshot |
| 19.7 `AlertEvent` identity | carried alert state |
| 19.8 operational health vocabulary + persistence conventions | report vocabulary + store discipline |
| Sprint 11K / 19.8 safe-id + atomic-write pattern | persistence implementation |

## 9. Intentionally non-reused components

| Component | Why not reused |
| --- | --- |
| Sprint 11W `OutcomeEvaluator` | trade-geometry outcome semantics, incompatible with forward price-movement measurement |
| Sprint 11X/11Y performance/evidence | trade-outcome evidence aggregation, different concern |
| Checkpoint 9.x forward-return/excursion | separate historical research pipeline; 19.9 keeps its own minimal vocabulary |
| Product Phase 5 paper trading | trade-lifecycle simulation; 19.9 must stay semantically independent |
| Sprint 11U `MarketScanner` | trade-opportunity intelligence, not forward validation |

## 10. Forward-testing definition

Forward validation = OBSERVE → RECORD → WAIT → OBSERVE OUTCOME → MEASURE.
It is NOT backtesting: no historical return optimization, no threshold /
weight tuning against outcomes, no hindsight-based labels.

## 11. Observation model

`ForwardObservation` (frozen+slots) captures at minimum: `observation_id`
(`fobs-<sha256[:16]>`), `setup_id`, `lifecycle_id`, `instrument`, `direction`,
`setup_type`, `primary_timeframe`, `observation_timestamp`, `scan_cycle_id`,
`lifecycle_state`, `lifecycle_observation_status`, `quality_status`,
`quality_classification`, `quality_score`, `mtf_alignment`,
`mtf_completeness`, `alert_state`, `alert_id`, `reference_price`,
`policy_version`, `status` (RECORDED/DUPLICATE/OUT_OF_ORDER), `reason`.

## 12. Outcome model

`ForwardOutcome` (frozen+slots) measures what happened AFTER T:
`outcome_id` (`fout-<sha256[:16]>`), `observation_id`, `instrument`,
`direction`, `availability` (OUTCOME_AVAILABLE / OUTCOME_PARTIAL /
OUTCOME_UNAVAILABLE / WINDOW_INCOMPLETE), `outcome_direction`
(FAVORABLE / UNFAVORABLE / NEUTRAL / NOT_EVALUABLE), `reference_price`,
`reference_timestamp`, `measurement_timestamp`, `forward_return`,
`max_favorable_movement`, `max_adverse_movement`, `bars_used`,
`bars_available`, `horizon_bars`, `window_complete`, `reason`.

## 13. Point-in-time semantics

- Observation at T uses ONLY the 19.5/19.6/19.7 state available at T.
- The observation record is immutable; never rewritten.
- Outcome measurement consumes ONLY candles with `timestamp > T`.
- `reference_price` = the close of the last candle completed at or before T
  (the observation's own reference), never a future price.

- `ForwardOutcomeEngine.measure(observation, forward_candles, measurement_timestamp)`:
  1. strictly-after filter (`timestamp > T`) — structural;
  2. completion filter at the measurement anchor (`timestamp + duration
     <= measured_at`, inclusive close) — structural;
  3. horizon truncation (`completed[:horizon]`) — structural;
  4. `window_complete = completed_count >= horizon`;
  5. `bars_available` = all strictly-after candles supplied;
  6. `bars_used` = completed ∩ horizon count.

## 14. Future-data exclusion

- No forward candle is ever fed to the observation.
- The outcome engine is a pure predicate over the supplied candle list; it
  never reads a provider / store / future candle.
- Adversarial tests 1–7 prove future candles / setup confirmation / BOS /
  trend change / S-R change / alerts / lifecycle transitions cannot alter an
  earlier observation.

## 15. Configuration versioning

- `ForwardValidationConfig` (frozen+slots) captures `universe`, `timeframes`,
  `primary_timeframe`, `provider`, `max_holding_bars`, `record_duplicates`,
  `reject_out_of_order`, `label`, `metadata`.
- `ForwardValidationConfig.policy_version()` = `fv-<sha256[:16]>` of the
  canonical config snapshot.
- `ForwardSession` records `policy_versions` (validation policy version +
  universe version) so a later config change can never silently reinterpret
  earlier observations.

## 16. Session model

`ForwardSession` (frozen+slots): `session_id` (`fsess-<sha256[:16]>`),
`provider`, `timeframes`, `primary_timeframe`, `universe`, `universe_version`,
`policy_versions`, `started_at`, `status` (OPEN/CLOSED), `label`, `metadata`.
`ForwardSessionManager` opens / closes / advances sessions with deterministic
identity and duplicate / out-of-order rejection.

## 17. Universe coverage

- The canonical 19.1 universe is the input; no second universe is created.
- `ForwardValidationEngine.process(...)` records ONE observation per
  requested constituent (sorted canonical order); a missing/unsupported/
  failed symbol becomes an explicit non-observation (`UNAVAILABLE` /
  `DATA_UNAVAILABLE`), never a silent drop.
- Adversarial tests 15–17, 31 prove 1 / 50 / 200-symbol accounting.

## 18. Data coverage

- Reuses 19.2 semantics: `IntradayCoverageStatus` vocabulary for
  data-availability; completed-candle boundary; session-aware freshness.
- The forward layer never fetches data itself; it consumes the frozen
  19.2/19.3/19.4/19.5 outputs.

## 19. Data quality

- Missing / duplicate / out-of-order / malformed / future candles, stale
  data, unsupported instruments, provider failures and session gaps are
  tracked using the 19.2 vocabulary (no second data-quality vocabulary).
- The outcome engine tolerates duplicate / out-of-order forward candles as
  pure predicates (adversarial tests 24–26).

## 20. Scan continuity

- Each observation carries its `scan_cycle_id`; a scanner restart is modeled
  as a new cycle with identical deterministic identity (adversarial test 18).

## 21. MTF behavior

- MTF alignment / completeness are carried verbatim from 19.4 at T.
- A future MTF change cannot alter an earlier observation (adversarial
  test 3/4).

## 22. Setup-quality behavior

- Quality status / classification / score are carried verbatim from 19.5 at T.
- Deterministic; a score change never creates a duplicate observation
  (identity is setup-based, not score-based).

## 23. Ranking behavior

- The report aggregates setup counts by quality classification / setup type /
  direction / MTF alignment; ranking itself is a 19.5 concern and is not
  re-derived.

## 24. Setup frequency

- The report measures setups per scan cycle / per instrument / per day
  (session), by quality classification, by setup type, by direction, by MTF
  alignment, confirmed vs rejected, lifecycle resolution — reporting what
  actually occurs, never optimizing for a desired frequency.

## 25. Setup lifecycle behavior

- Lifecycle state at T is carried verbatim from 19.6.
- The report aggregates DETECTED / CONFIRMED / INVALIDATED / EXPIRED /
  SUPERSEDED / DATA_UNAVAILABLE / LATE_REJECTED counts.
- No illegal transitions, no reopening of terminal lifecycles, no false
  expiry on data failure, idempotent repeated observations, out-of-order
  rejection (all covered by 19.6 tests + 19.9 adversarial tests).

## 26. Alert behavior

- Alert state / id at T are carried verbatim from 19.7.
- The report measures alerts generated / delivered / suppressed / failed /
  retried, duplicate attempts, missed alerts, latency where timestamps exist.
- The same alert retried 20 times keeps the same alert identity (adversarial
  test 10).

## 27. Alert delivery

- Delivery is a 19.7/19.8 concern; 19.9 only carries the alert state at T and
  reports delivery counts from the upstream cycle. Delivery failure cannot
  change analytical state (adversarial test 19).

## 28. Reliability behavior

- The report surfaces operational-failure counts using the 19.8 vocabulary
  (provider / scanner / delivery / persistence failures) — never conflating
  HEALTHY/DEGRADED/RECOVERING/FAILED with the 19.5 quality bands
  EXCELLENT/HIGH/MEDIUM/LOW/REJECTED.

## 29. Recovery behavior

- Restart recovery is a 19.8 concern; the forward layer preserves
  observation identity across restarts (adversarial test 9, persistence
  restart tests).

## 30. Latency

- Latency is measured ONLY where upstream timestamps exist: observation
  timestamp → measurement timestamp. The report surfaces the measurement
  timestamp delta when available; unavailable latency is reported honestly
  (never manufactured).

## 31. Outcome measurement

- Reference timestamp: the observation timestamp T.
- Measurement window: `[T, T + horizon × duration]` (completed candles
  strictly after T, truncated to `max_holding_bars`).
- Price source: the supplied forward candle series (caller-provided; the
  operator supplies the completed candles from the frozen 19.2 layer).
- Candle timeframe: the session primary timeframe.
- Completion rule: `timestamp + duration <= measured_at` (inclusive close).
- Direction handling: LONG/BULLISH favorable = close ≥ reference; SHORT/
  BEARISH favorable = close ≤ reference; NEUTRAL / non-directional =
  NOT_EVALUABLE.
- Missing-data behavior: no forward candles → OUTCOME_UNAVAILABLE
  (`bars_used == 0`, no fabricated values).
- Ambiguous-data behavior: no reference price → OUTCOME_UNAVAILABLE;
  same-bar both-touch is not applicable (no trade geometry) — the outcome
  is computed from closes only.
- Session handling: closed-market / weekend references simply yield no
  forward data (OUTCOME_UNAVAILABLE), never a system failure.
- End-of-window behavior: `window_complete` reflects whether the full
  horizon was completed at the measurement anchor; partial windows are
  OUTCOME_PARTIAL / WINDOW_INCOMPLETE, never a fabricated full outcome.

## 32. Missing-data treatment

`OutcomeAvailability` = OUTCOME_AVAILABLE / OUTCOME_PARTIAL /
OUTCOME_UNAVAILABLE / WINDOW_INCOMPLETE. Missing outcome data is never
classified as success or failure.

## 33. Statistical methodology

- Descriptive statistics only: counts, forward-return average, favorable /
  unfavorable / neutral counts, max favorable / adverse movement averages.
- No hypothesis tests, no confidence intervals, no predictive claims.

## 34. Sample-size limitations

- The report carries a `sample_size_note` that states "SAMPLE TOO SMALL for
  reliable inference — descriptive only" when the observation / outcome
  counts are below a documented threshold (default 30 observations / 10
  outcomes). No strong conclusions are drawn from tiny samples.

## 35. No-optimization policy

- No parameter (score weights, thresholds, MTF config, lifecycle expiry,
  alert policy, ranking, measurement windows) is optimized against forward
  outcomes in this checkpoint. Weaknesses are DOCUMENTED, not tuned.

## 36. No-cherry-picking policy

- The report consumes the FULL recorded population (all observations, all
  outcomes). No subset filtering exists in the layer (adversarial test 33).

## 37. Persistence

- `ForwardObservationStore` (schema-versioned, atomic same-dir temp +
  os.replace, safe-id regex, corruption fail-closed, idempotent identical
  saves, conflicting-content rejection unless overwrite, default directory
  `./data/forward_validation`).
- Serialization is deterministic sorted-key JSON with type tags
  (`__enum__`/`__dataclass__`/`__datetime__`/`__tuple__`), lossless round
  trip, schema checked BEFORE reconstruction, future schema rejected.
- Exceptions (additive in `src/engine/persistence/exceptions.py`):
  `ForwardStoreError` / `ForwardSessionNotFoundError` /
  `ForwardObservationNotFoundError` / `ForwardStoreIntegrityError` /
  `UnsupportedForwardSchemaVersionError`.

## 38. Restart / recovery

- Persistence survives restart (tests save via store #1, load via store #2).
- Deterministic session identity survives restart.
- Corrupt persisted state fails closed (never a valid-looking record).

## 39. Operator workflow

```
python scripts/forward_validation.py --timeframes 15m \
    --instruments RELIANCE TCS --reference-now <aware-UTC> start
python scripts/forward_validation.py --provider fixture demo
python scripts/forward_validation.py --json report
```

- `start` opens a session (records config + policy versions).
- `demo` runs the deterministic offline demo.
- `report` prints the FORWARD-VALIDATION REPORT (aggregated over sessions).
- Flags are given BEFORE the sub-command (argparse layout).

## 40. Offline validation

- Default test mode is deterministic / offline (fixture provider, scripted
  candles, injected timestamps). All 142 tests + 13-check demo are offline.

## 41. Live validation

- A live mode is an explicit OPT-IN operator action (the frozen 19.2 Yahoo
  provider path). It requires no credentials in unit tests, never places
  orders, and clearly reports whether live validation actually occurred.
- **LIVE VALIDATION NOT PERFORMED** in this environment. Full Top-200 live
  coverage is NOT claimed (19.2 documented this as an operator action).

## 42. What was actually validated

- Observation creation + identity + configuration/version capture.
- Point-in-time snapshot + future-data exclusion (adversarial).
- Outcome separation + calculation + direction handling + window handling.
- Missing / partial / ambiguous data handling.
- Session handling + Top-200 accounting (1/50/200 symbols).
- Setup-frequency / ranking / lifecycle / alert / reliability aggregation.
- Alert retry identity preservation.
- Persistence round-trip / restart / corruption / idempotency.
- Deterministic reporting + configuration immutability.
- No-optimization + no-cherry-picking policies.
- Broker / execution boundary (AST).
- Historical-data leakage boundary (adversarial).

## 43. What could not be validated

- Live Top-200 forward validation (no live provider / market-hours data in
  this environment; documented operator action).
- Real-world alert delivery latency (no external delivery channel; 19.7
  console sink only).
- Long-horizon forward outcomes (requires a live forward session run over
  days/weeks).

## 44. Results

- 142 new deterministic tests pass (68 core + 35 adversarial + 26
  persistence + 13 CLI).
- 13-check demo passes.
- Full suite: **7155 passed / 12 skipped / 2 warnings** (7013 baseline +
  142 net new; 12 opt-in real-broker/sandbox skips; 2 pre-existing
  third-party deprecation warnings).
- Pipeline baseline unchanged (signals=4, trades=3).

## 45. Known limitations

- OFFline / deterministic validation only; live validation not performed.
- Outcome measurement requires the operator to supply the completed forward
  candles (the frozen 19.2 layer) — the framework does not fetch data.
- Latency is only measurable where upstream timestamps exist.
- Sample size is tiny in the demo (descriptive only).

## 46. Architectural weaknesses discovered

- None material. The framework is additive and does not reopen any frozen
  checkpoint. The only noted limitation is the inherent dependency on the
  operator-supplied forward candle series for outcome measurement (by
  design, to keep the layer provider-independent and point-in-time safe).

## 47. Recommended future work

- Run a live forward-validation session over the NIFTY Top 200 with the
  Yahoo OPT-IN provider (operator action).
- Wire the 19.9 observation/outcome stores into a long-running forward
  session and produce the aggregate report after outcomes mature.
- (Out of scope for 19.9; documented for the operator.)

## 48. Broker boundary

- AST-checked: no broker / execution imports in any new 19.9 module
  (`broker_adapter`, `submission_lifecycle`, `execution_command`,
  `execution_authorization`, `upstox_broker`, `fake_broker`, `paper_trading`,
  `trade_planning`).
- No network libraries in the engine/config/models/persistence.
- No credentials read for execution.
- The final output remains ALERT → USER → MANUAL EXECUTION.

## 49. Final 19.x assessment

The 19.x roadmap is complete (19.1–19.9). The forward-validation layer is
correct, deterministic, point-in-time safe, and regression-safe. **PASS
WITH LIMITATIONS** — the framework is implemented and locally validated; live
forward validation requires an operator action and was not performed here.

## 50. Final verdict

**PASS WITH LIMITATIONS.** The tested behavior satisfies its documented
requirements with no material correctness, integrity, point-in-time,
reliability, or boundary violation. Live coverage and sample size are the
explicitly documented limitations preventing stronger conclusions.
</｜DSML｜>
