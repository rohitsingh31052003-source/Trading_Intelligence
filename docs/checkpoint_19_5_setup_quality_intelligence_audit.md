# Checkpoint 19.5 — Setup-Quality Intelligence

- Status: COMPLETE / PASS
- Audit date: 2026-09-06
- Baseline: Checkpoint 19.4 — 6590 passed / 12 skipped / 2 warnings

This document is the audit + design record for Checkpoint 19.5. It follows
the conventions of all previous Checkpoint audit documents.

---

## 1. Objective

Build the SETUP-QUALITY INTELLIGENCE layer on top of the FROZEN 19.1-19.4
foundations. The layer transforms validated multi-timeframe market-state
information into EXPLICIT, EXPLAINABLE, DETERMINISTIC setup-quality
intelligence that answers:

> "Given the market state currently available for this instrument, does it
> contain a potentially meaningful trading setup, how strong is that setup
> according to the defined quality model, and why?"

The layer performs SETUP DETECTION, SETUP-QUALITY ASSESSMENT, EXPLANATION,
and CANDIDATE RANKING. It does NOT execute trades, place orders, manage
positions, send alerts, or persist a setup lifecycle.

## 2. Scope

IN SCOPE: setup candidate detection, setup classification, setup-quality
assessment and scoring, deterministic quality dimensions, explainable
scoring, evidence/provenance, MTF confluence + market-structure + trend +
support/resistance + price-context inputs, setup-quality thresholds,
candidate qualification, candidate ranking, deterministic tie-breaking,
quality/confidence classification, rejection reasons, setup-quality
summaries, universe-wide evaluation, deterministic offline tests, operator
diagnostics.

NOT IN SCOPE (belong to later checkpoints): automated trade execution,
broker order placement, portfolio/position management, automated
entries/exits, user notifications (Telegram/WhatsApp/email/push), setup
lifecycle/state persistence, setup expiry, setup confirmation/invalidation
lifecycles, forward-testing infrastructure, reliability/recovery/watchdog
framework, persistent execution workers.

BROKER EXECUTION REMAINS COMPLETELY DEFERRED THROUGHOUT CHECKPOINT 19.x.

## 3. Relationship to Checkpoint 19.1

The FROZEN 19.1 canonical NIFTY Top 200 universe (the versioned NSE
manifest + `UniverseBuilder` / `UniverseDefinition` /
`DEFAULT_NIFTY200_UNIVERSE`) is CONSUMED, never duplicated. The 19.5
engine accepts a `UniverseDefinition` (or a canonical name sequence)
directly; the universe-wide `SetupQualityUniverseResult` enforces
exactly-one-result-per-constituent (matching the 19.1/19.3/19.4
invariants). No second universe was created.

## 4. Relationship to Checkpoint 19.2

The FROZEN 19.2 canonical `DashboardDataProvider`, intraday coverage layer,
normalization/validation, freshness, session awareness and explicit
data-status vocabulary are CONSUMED, never duplicated. The 19.5 data gate
reads the reused `IntradayCoverageStatus` vocabulary verbatim: STALE /
VALID / VALID_WITH_GAPS / UNSUPPORTED_* / PROVIDER_ERROR / NO_DATA / EMPTY /
INVALID_RESPONSE are all preserved. Stale data is a HARD data-quality cap;
missing / unsupported / error frames never become a qualified setup.

## 5. Relationship to Checkpoint 19.3

The FROZEN 19.3 `ContinuousScannerEngine` / `ContinuousScanner` /
deterministic scan cycles / per-symbol scan results / statuses / ordering /
no-overlap / injected clock/waiter are untouched. 19.5 does not modify the
19.3 flow; it is an additive consumer of the 19.4 result that the 19.3-era
data path feeds. The per-symbol failure-isolation and
exactly-one-result-per-constituent conventions established in 19.3 are
mirrored by the 19.5 universe result.

## 6. Relationship to Checkpoint 19.4

The FROZEN 19.4 `PerSymbolMtfResult` / `MtfTimeframeState` /
`MtfUniverseAnalysis` / `MtfAnalysisEngine` / `MtfAnalysisConfig` are the
PRIMARY 19.5 input. The 19.5 engine consumes the already-computed 19.4
per-symbol result BY REFERENCE (never modified) plus the caller-supplied
COMPLETED primary-timeframe candles at the same reference instant. The 19.4
alignment/completeness classifications are preserved verbatim on the 19.5
result (`result.mtf.alignment` / `result.mtf.completeness` /
`result.data_complete`). No MTF engine duplication: the 19.5 engine imports
only the 19.4 MODELS, never the 19.4 engine.

## 7. Existing trading-intelligence architecture (audit result)

The repository already contains substantial deterministic, point-in-time
safe trading intelligence. The audit established the following reusable
components:

| Component | Module | Point-in-time safe | Live-path-proven |
| --------- | ------ | ------------------- | ---------------- |
| Swing detection | `engine.intelligence.swings.SwingEngine` | YES (lookback-confirmed) | YES (Sprint 11P path) |
| Market structure | `engine.intelligence.structure.MarketStructureEngine` | YES | YES |
| Structure analysis | `engine.intelligence.structure_analysis.StructureAnalysisEngine` | YES | YES |
| Range detection | `engine.intelligence.range_detection.RangeDetectionEngine` | YES | YES |
| Support/resistance context | `engine.intelligence.support_resistance_context.SupportResistanceContextEngine` | YES | YES |
| Descriptive trend | `engine.intelligence.market_trend.MarketTrendEngine` | YES | YES |
| Market context bundle | `engine.intelligence.market_context_engine.MarketContextEngine.analyze_at` | YES (prefix-only) | YES (product dashboard) |
| Candle/price-action patterns | `engine.intelligence.candle_patterns.CandlePatternEngine` | YES (candles[T-1..T]) | YES (11Q/12x paths) |
| Setup/confluence assessment | `engine.intelligence.setup_confluence.SetupConfluenceEngine` | YES (reads pre-computed inputs) | YES (6D research path) |
| MTF market-state relation | `dashboard.mtf_analysis.MtfAnalysisEngine` + `engine.models.mtf_analysis` | YES (completed-candle boundary) | YES (19.4) |
| BOS | `engine.intelligence.bos.BOSEngine` | YES | HISTORICAL signal path ONLY |
| CHOCH | `engine.intelligence.choch.CHOCH` | YES | HISTORICAL signal path ONLY |
| Trend (signal-path) | `engine.intelligence.trend.TrendEngine` | YES | HISTORICAL signal path ONLY |
| Liquidity | `engine.intelligence.liquidity.*` | YES | RESEARCH / historical |
| Confluence/decision/signal | `engine.intelligence.confluence.*`, `decision.py`, `signal.py` | YES | HISTORICAL signal pipeline |
| Volume | `engine.intelligence.volume.py` | — | EMPTY (0-byte placeholder; no references) |
| Structural strength | `engine.intelligence.strength.py` / `structural_strength.py` | YES (historical) | HISTORICAL / research |
| Historic setup research | `engine.data.setup_research.py` + 9.x research chain | YES (corpus-bound) | RESEARCH ONLY |
| Trade candidate / decision / opportunity | `engine.intelligence.trade_candidates.py` / `trade_decision.py` / `trade_opportunity.py` | YES | HISTORICAL / 6D research |
| Market scanner (11U) | `engine.intelligence.market_scanner.MarketScanner` | YES | HISTORICAL scanner |

### Historical-only / research-only / paper-trading-only / live-path-compatible

- HISTORICAL SIGNAL PATH (not wired to the 19.x live/intraday path):
  BOS, CHOCH, trend (signal-path), confluence/decision/signal, liquidity,
  structural strength, volume.
- RESEARCH ONLY: Sprint 11H-11Z research engines, 6C/6D corpus + setup
  research, Checkpoints 9.x historical setup research chain.
- PAPER-TRADING ONLY: Product Phases 4/5 paper trading + operations.
- LIVE-PATH-COMPATIBLE (point-in-time safe, consumption path proven by the
  Product dashboard + 19.4): SwingEngine, MarketStructureEngine,
  StructureAnalysisEngine, RangeDetectionEngine,
  SupportResistanceContextEngine, MarketTrendEngine,
  MarketContextEngine, CandlePatternEngine, SetupConfluenceEngine.

## 8. Existing setup / signal / trade-plan components

- Setup: Sprint 11Q `SetupConfluenceEngine.assess(...)` produces
  `SetupAssessment` (`NO_SETUP` / `WATCH` / `POTENTIAL_SETUP`,
  `confluence_score` in [0,5], structured `SetupEvidence` with supporting /
  conflicting views). Sprint 11R `TradeCandidateEngine` produces
  `TradeCandidate` with a `SetupType` taxonomy (TREND_CONTINUATION /
  BREAKOUT / STRUCTURE_CONTINUATION / RANGE_REJECTION / SETUP_CANDIDATE).
- Signal: Sprint 11C `SignalEngine` (historical signal pipeline only).
- Trade-plan: Product Phase 4 `TradePlan` (+ `TradePlanningEngine`),
  Sprint 11S `TradeDecision`, Sprint 11T `TradeOpportunity` —
  downstream concepts NOT used by 19.5.
- Scoring: Sprint 11S `DecisionScore` (mapped, defect-scored); Sprint 11X
  `HistoricalPerformanceStatistics`; Checkpoint 9.11 historical setup
  quality (research-only, forward-return consistency — semantically
  incompatible with live/intraday setup-quality scoring).

## 9. Reuse assessment

REUSED VERBATIM (proven, point-in-time safe, live-path-compatible):

1. `engine.intelligence.candle_patterns.CandlePatternEngine` (11O) — for
   per-timeframe pattern detection at the triggering index.
2. `engine.intelligence.setup_confluence.SetupConfluenceEngine` (11Q) —
   for single-frame setup candidate detection + classification + evidence
   + confluence score. NO new setup-detection heuristic is invented.
3. The FROZEN 19.4 `PerSymbolMtfResult` — the primary input, by
   reference.
4. The FROZEN 19.1 universe (`UniverseBuilder` / `UniverseDefinition`).
5. The FROZEN 19.2 `IntradayCoverageStatus` vocabulary for data gates.
6. The Sprint 11R `SetupType` TAXONOMY + documented derivation rules
   (BREAKOUT / TREND_CONTINUATION / STRUCTURE_CONTINUATION /
   SETUP_CANDIDATE) — WITHOUT computing any geometry.
7. The Sprint 11P `MarketContext` (structure / trend / range / S-R)
   carried by the 19.4 timeframe states.

INTENTIONALLY NOT REUSED (with rationale):

1. `engine.intelligence.trade_candidates.TradeCandidateEngine` (11R) —
   computes entry/stop/target/risk-reward geometry, which the 19.5 spec
   says NOT to compute. Its setup-type rules are mirrored (documented)
   without the geometry.
2. `engine.intelligence.trade_decision.TradeDecisionEngine` (11S) — a
   decision layer whose score includes geometry + risk-reward components
   (trade-plan concepts) and whose classification (REJECTED/WATCH/
   QUALIFIED/PREFERRED) describes candidate DECISION, not setup QUALITY.
   19.5 defines a NEW, non-overlapping quality classification.
3. Checkpoint 9.x historical setup quality / evidence — research-only,
   forward-return based, semantically incompatible with live intraday
   setup-quality scoring.
4. BOS/CHOCH/signal-path trend/liquidity/volume/strength — historical /
   research / signal path only; not live-intraday-path compatible at
   19.5, and the quality model does not need them as inputs.
5. 11U `MarketScanner` / 11T `TradeOpportunity` — opportunity / scanner
   semantics; 19.5 has its own deterministic ranking for setup-quality
   candidates (a setup-quality view, not an opportunity feed).

## 10. Setup taxonomy

The existing project ALREADY defines a conservative setup taxonomy
(Sprint 11R `SetupType`). 19.5 reuses it VERBATIM (via the enum member
names; the new model layer avoids a trade-candidate import by carrying
`setup_type` as a string):

- `TREND_CONTINUATION` — directional trend aligned with the structure and
  price at a constructive pullback location (e.g. near support in an
  uptrend).
- `BREAKOUT` — price has moved beyond a structural level in the candidate
  direction.
- `STRUCTURE_CONTINUATION` — structure aligned but descriptive trend not
  aligned, price at a constructive location.
- `SETUP_CANDIDATE` — conservative generic fallback when the evidence
  cannot reliably distinguish a specific type.
- (`RANGE_REJECTION` exists in the enum but is not populated by default —
  range setups are disabled; matches 11R.)

Rationale: the spec requires "Use existing project semantics where
possible." No new taxonomy was invented.

## 11. Setup detection model

Setup detection REUSES the Sprint 11Q `SetupConfluenceEngine.assess`
verbatim on the PRIMARY (lowest-duration) timeframe:

```
SetupQualityEngine.evaluate(mtf_result, primary_candles)
    -> CandlePatternEngine.detect(primary_candles)      (reused 11O)
    -> patterns at the last index of the supplied completed candles
    -> SetupConfluenceEngine.assess(patterns_at_T,
                                    primary_market_state,
                                    index, timestamp)  (reused 11Q)
    -> SetupAssessment (classification NO_SETUP/WATCH/POTENTIAL_SETUP,
                        confluence_score, evidence, has_conflict,
                        direction)
```

A setup candidate is therefore detected ONLY when the 11Q engine reaches
a directional, evidence-based classification — never merely because price
is moving, a timeframe is bullish, or MTF is ALIGNED. The 11Q
`POTENTIAL_SETUP` definition is the project's established legitimate setup
condition (>= 3 aligned independent evidence sources, clear directional
bias, no disqualifying conflict by default).

## 12. Setup evidence model

Each `SetupQualityResult` carries EXPLICIT, input-derived evidence:

- `positive_factors` — e.g. `MTF_ALIGNED`, `SETUP_POTENTIAL`,
  `CONFLUENCE_3/5`, `STRUCTURE_INTACT_7_SWINGS` (derived from the actual
  19.4/11Q/11P inputs).
- `negative_factors` — e.g. `MTF_CONFLICTING`, `MTF_INCOMPLETE`,
  `CONFLICTING_EVIDENCE`, `STALE_PRIMARY_DATA`,
  `SETUP_BELOW_QUALIFICATION_BAR`, `NO_SETUP_DETECTED`.
- `score_components` — the four named components with points /
  max_points / reason (auditable breakdown).
- `setup_classification` (reused 11Q), `setup_direction`,
  `confluence_score`, `has_conflict`, `setup_type`.

## 13. Quality model

The quality model is the smallest robust model supported by the existing
project's analytical capabilities. It has FOUR documented, non-overlapping
dimensions (no double counting — trend/location enter only through the 11Q
confluence/confirmation components, which already incorporate them):

| Component | Weight | Input | Contribution rule |
| --------- | ------ | ----- | ------------------- |
| `mtf_alignment` | 30 | 19.4 `MtfAlignmentState` | ALIGNED=30; MIXED=15; CONFLICTING=0; INCOMPLETE=0; UNAVAILABLE=0 |
| `setup_confirmation` | 30 | 11Q `SetupClassification` | POTENTIAL_SETUP=30; WATCH=15; NO_SETUP=0 |
| `setup_confluence` | 25 | 11Q `confluence_score` [0,5] | `round(confluence/5*25)` -> 0,5,10,15,20,25 |
| `structure_quality` | 15 | 11P `MarketContext` structure_intact + confirmed_swings | intact & >=2 swings=15; >=1 swing=8; none=0 |

Sum = 100. The score is a bounded integer [0, 100]. Data quality is a
PREREQUISITE gate (not a score component).

## 14. Scoring methodology

- Score range: `[0, 100]` (bounded integer; `max_score == 100`).
- Each component has a documented meaning + contribution (section 13).
- Weighting is explicit and configurable with validation (weights must
  sum to 100).
- Normalization: confluence is normalized to its weight via
  `round(confluence/5*weight)`; partial credit uses
  `partial_fraction = 0.5`.
- Rounding: `round(...)` on each component; the total is the integer sum
  of the (already rounded) components, clamped to `[0, max_score]`.
- Tie behavior: deterministic (stable sort by ranking key).
- Threshold categories: EXCELLENT >= 80, HIGH >= 65, MEDIUM >= 50,
  LOW >= 35, REJECTED < 35.
- Determinism: identical inputs ALWAYS produce identical scores
  (pure function; no randomness, no wall-clock, no model calls, no
  network).
- Precision: the score is an integer 0-100 — never a meaningless
  87.43-style figure.

## 15. Quality classification

`SetupQualityClassification` (by rank): EXCELLENT > HIGH > MEDIUM > LOW >
REJECTED (quality bands), plus the data-gate states INCOMPLETE (data
insufficient) and UNAVAILABLE (no data). The band is derived SOLELY from
the score + the documented caps (section 16). No subjective runtime
judgment.

## 16. Negative evidence

Negative evidence is handled EXPLICITLY — the model never simply adds
positive points while ignoring contradictions:

- MTF CONFLICTING -> `mtf_alignment` = 0 + `MTF_CONFLICTING` negative
  factor.
- MTF INCOMPLETE -> `mtf_alignment` = 0 + `MTF_INCOMPLETE` negative
  factor + classification capped at LOW.
- 11Q WATCH -> setup is never qualified (status WATCH) + classification
  capped at LOW + `SETUP_BELOW_QUALIFICATION_BAR` negative factor.
- 11Q conflict (`has_conflict`) -> classification capped at MEDIUM +
  `CONFLICTING_EVIDENCE` negative factor.
- STALE primary data -> classification capped at LOW +
  `STALE_PRIMARY_DATA` negative factor (never a high-quality setup).
- NO_SETUP detection -> status NO_SETUP + classification REJECTED (never
  a qualified setup).

Caps are applied in order, each can only REDUCE the classification
(mirrors the 11S cap discipline).

## 17. Data-quality prerequisites

The 19.5 engine applies a deterministic DATA GATE before any setup
detection / scoring (node fabricated values):

- 19.4 completeness UNAVAILABLE -> `status=UNAVAILABLE`, `score=None`.
- 19.4 completeness UNSUPPORTED -> `status=INCOMPLETE`, `score=None`.
- Primary timeframe state missing / not usable / no derived market state
  -> `status=INCOMPLETE`, `score=None`.
- STALE primary data -> data usable but classified with a STALE flag +
  LOW cap.
- INCOMPLETE MTF -> data usable on the primary frame but the relation is
  incomplete (zero MTF credit + LOW cap).

These rules satisfy: "5m valid / 15m valid / 1h stale must not silently
become a fully confident setup" and "MTF = INCOMPLETE must not be treated
as MTF = ALIGNED."

## 18. MTF integration

The 19.4 alignment (ALIGNED / MIXED / CONFLICTING / INCOMPLETE /
UNAVAILABLE) feeds the `mtf_alignment` component; the 19.4 completeness
feeds `data_complete` + the classification caps. ALIGNED does not mean
BUY; CONFLICTING does not mean SELL — the MTF classification is a
timeframe relationship that contributes to setup QUALITY only.

## 19. Point-in-time safety

The engine is a pure function of:
- the 19.4 `PerSymbolMtfResult` (already derived from completed candles
  <= T by the frozen 19.4/19.2 boundary), and
- the caller-supplied COMPLETED primary-timeframe candles at T.

The 19.5 engine itself:
- never reads a provider, store, or future candle;
- attributes candle patterns only to the LAST index of the supplied
  candles;
- consumes only the already-computed market state carried by the 19.4
  timeframe state.

## 20. Look-ahead protection

Structural + regression-tested: appending arbitrary future candles
(including a future breakout, a future BOS/break-of-structure, a future
higher-timeframe candle, a future trend change, and a future
support/resistance event) leaves an already-computed setup-quality result
at T unchanged (score, status, classification, components, factors). The
tests evaluate the same `mtf` result + the same prefix, then re-evaluate
with appended future candles and assert identity (see section 35).

## 21. Explainability

Every non-gate result carries:
- `explanation` — derived from the ACTUAL inputs (`MTF <alignment>
  (<completeness>); setup <classification> with confluence <n>/5; score
  <s>/100 -> <band>` + negative evidence list);
- `reason` — the evaluation-state summary;
- `score_components[].reason` — per-component reasons;
- `positive_factors` / `negative_factors` — input-derived labels.

No generic/disconnected explanations are generated.

## 22. Provenance

Each result preserves:
- `reference_now` (the analysis timestamp); the 19.4 result preserves the
  per-timeframe source timestamps (`latest_completed_timestamp`) and the
  latest completed candle;
- `mtf` by reference (with `analysis_id` when analyzed universe-wide);
- the config snapshot embedded in the deterministic `analysis_id`
  (`sq-<sha256[:16]>` over timeframes + universe + reference instant +
  scoring-rule/config snapshot), serving as the scoring-rule/model
  version identifier.

No persistence infrastructure was introduced (19.6/19.8 own lifecycle and
reliability).

## 23. Determinism

Identical (universe, candles, MTF state, analysis timestamp, config)
produce identical results for: setup detection, quality score, quality
classification, evidence, ranking, tie-breaking, and JSON serialization.
No randomness, no system-time dependence, no provider-response-ordering
dependence (universe results are canonically ordered; ranking sorts
ignores input order).

## 24. Universe-wide evaluation

`SetupQualityEngine.evaluate_universe(analysis, primary_candles_by_instrument)`
evaluates the full universe (default NIFTY Top 200 from the 19.1
`UniverseDefinition`). Every requested constituent gets EXACTLY ONE
explicit `SetupQualityResult` (never silently dropped). The
`SetupQualityUniverseResult` carries status/classification distributions,
the ranked qualified candidates, and the top-N view.

## 25. Candidate ranking

Qualified candidates are ranked strongest-first by a deterministic key:
1. classification rank desc (EXCELLENT > HIGH > MEDIUM > LOW),
2. score desc,
3. confluence desc (absent last),
4. data completeness (COMPLETE first),
5. fewer negative factors,
6. instrument name ascending (final deterministic tie-break).

Direction (LONG/SHORT/BULLISH/BEARISH) is deliberately NOT a ranking key
(matching the 19.4 convention). Rejected/incomplete/unavailable symbols
are never ranked as candidates — they appear in the universe result with
their explicit status.

## 26. Tie-breaking

Deterministic: identical scores break by confluence, then completeness,
then negative-factor count, then canonical instrument name ascending.
No hash randomization, dictionary order, arrival order, network response
order, or random numbers.

## 27. Failure isolation

One symbol's data failure never terminates the universe run (the
`evaluate_universe` loop is per-symbol and the data gate returns explicit
UNAVAILABLE/INCOMPLETE results). One timeframe's failure is carried by the
19.4 completeness (INCOMPLETE) and capped at LOW — never a fabricated
qualified setup. Missing data stays visible in counts and per-symbol
statuses.

## 28. Setup vs lifecycle boundary

19.5 produces a POINT-IN-TIME assessment only. `SetupQualityStatus`
(QUALIFIED / WATCH / NO_SETUP / INCOMPLETE / UNAVAILABLE) is an evaluation
state, NOT a lifecycle state machine (no DETECTED -> CONFIRMED ->
TRIGGERED -> INVALIDATED -> EXPIRED). 19.6 owns lifecycle.

## 29. Setup vs alert boundary

19.5 produces ranked setup-quality information only. No alert conditions,
routing, dedup, or user notification (Telegram/WhatsApp/email/push) is
implemented. 19.7 owns alerts.

## 30. Setup vs trade-plan boundary

19.5 does NOT compute entry price, stop loss, target, position size, or
risk/reward. It derives the setup TYPE (reusing the 11R taxonomy + rules)
without any geometry. Trade-plan concepts remain downstream (Product
Phase 4 / future 19.x layers).

## 31. Broker-execution boundary

No trade execution, order placement, automated entry/exit, position
management, or execution API calls. The new 19.5 production modules
contain zero broker/execution/upstox imports (AST-checked in tests and the
demo). Checkpoints 13-18 remain frozen; execution gate DISABLED;
broker execution remains completely deferred throughout 19.x.

## 32. Changes implemented

Files created:

| File | Purpose |
| ---- | ------- |
| `src/engine/config/setup_quality_config.py` | `SetupQualityConfig` (weights, thresholds, caps, gates, ranking cap). |
| `src/engine/models/setup_quality.py` | `SetupQualityStatus`, `SetupQualityClassification`, `SetupQualityScoreComponent`, `SetupQualityResult`, `SetupQualityCounts`, `SetupQualityUniverseResult`, `setup_quality_analysis_id`. |
| `src/dashboard/setup_quality.py` | `SetupQualityEngine` orchestration (data gate, detection reuse, scoring, classification, ranking) + `SetupQualityViewBuilder` JSON projection. |
| `src/engine/reporting/setup_quality.py` | `SetupQualityFormatter` (deterministic text reports). |
| `scripts/analyze_setup_quality.py` | Operator CLI (offline/deterministic default; Yahoo opt-in). |
| `scripts/test_checkpoint_19_5.py` | Demo (15 checks). |
| `tests/test_setup_quality.py` | 72 focused tests. |
| `tests/test_setup_quality_cli.py` | 11 CLI tests. |
| `docs/checkpoint_19_5_setup_quality_intelligence_audit.md` | This document. |

Files modified: none (additive-only; `AGENTS.md` is the repository's
persistent-memory file and this section will be appended there by the
repository convention).

## 33. Tests added

- `tests/test_setup_quality.py` — 72 tests covering the 52-point 19.5
  requirement list: architecture (10), setup detection (6), scoring (9),
  MTF handling (6), point-in-time (6), ranking (6), failure isolation (4),
  explainability/provenance (5), boundaries (10), config validation (10),
  model validation (6), integration (5), no-look-ahead through
  aggregation (1) — some overlap across the numbered groups.
- `tests/test_setup_quality_cli.py` — 11 CLI tests.

Total focused: 83.

## 34. Tests executed and results

- 19.5 focused: 83 passed.
- Checkpoint 19.1 `tests/test_nifty200_universe.py`: 48 passed.
- Checkpoint 19.2 `tests/test_market_session.py`, `test_intraday_coverage*.py`
  (3 files): 200 passed.
- Checkpoint 19.3 `tests/test_continuous_scanner.py`, `test_scan_market_cli.py`:
  53 passed.
- Checkpoint 19.4 `tests/test_mtf_analysis.py`, `test_mtf_analysis_cli.py`:
  84 passed.
- Trading-intelligence / provider / dashboard regression subset: see the
  full-suite result below.
- FULL SUITE: 6673 passed / 12 skipped / 2 warnings (19.4 baseline 6590
  + 83 new net; skips = opt-in real-broker/sandbox; warnings =
  pre-existing third-party deprecations). No test modified/deleted/
  weakened; no unrelated regression.

## 35. Adversarial look-ahead tests

`tests/test_setup_quality.py::TestPointInTime` (6 tests) + engine-level
proofs construct a market at T, evaluate it, then append:
- a future breakout candle (price jumps 200+),
- a future BOS (break-of-structure, huge adverse move),
- a future higher-timeframe confirmation (a 1h candle closing after T),
- a future trend-change / support-resistance event (price collapses to
  10-15),
- arbitrary future candles (4 appended candles).

In every case the earlier setup-quality result at T (status, score,
classification, components, factors, explanation) is byte-identical.
`TestNoLookAheadThroughAggregation` additionally proves the engine is a
pure function of the supplied prefix. The frozen 19.4/19.2 completed-
candle boundary is consumed, not re-tested: the 19.5 engine reads only the
already-computed `PerSymbolMtfResult` + supplied completed candles.

## 36. CLI/operator behavior

`scripts/analyze_setup_quality.py`:
- `--provider fixture|yahoo` (fixture = default, deterministic, offline;
  yahoo = OPT-IN live),
- `--timeframes 15m,1h` (canonicalized + validated),
- `--instruments` (default = NIFTY Top 200 from the 19.1 manifest),
- `--reference-now` (deterministic fixture sentinel; naive rejected),
- `--top-n N` (qualified top-N view),
- `--json` (pure, deterministic machine-readable projection).

Exit codes: 0 ran (per-symbol findings reported honestly), 1 runtime
failure, 2 bad args. Banner: "SETUP-QUALITY INTELLIGENCE ONLY — no trade
execution, no alerts, no setup lifecycle, no broker execution." No
credential/network requirement on the default path; no alerts; no trade
execution.

## 37. Limitations

- The fixture provider serves only 4 instruments with 15m data (1h
  honestly UNSUPPORTED) — the default offline path evaluates real
  setups only where data exists; all other constituents are explicit
  UNSUPPORTED/INCOMPLETE.
- 19.5 scoring is a technical-evidence-strength model, NOT calibrated to
  any market; it makes no profitability/predictive claim (19.9 owns
  forward-testing/validation).
- MTF is a fixed 2-frame relation (15m+1h default); the 19.5 config
  accepts any ascending intraday multi-frame set the 19.4 layer
  supports.
- The 19.5 engine requires the caller to supply the completed
  primary-timeframe candles (the 19.4 state does not carry the full
  series); the operator CLI wires this through the canonical 19.2 path.
- No setup lifecycle, no alerting, no persistence, no reliability/
  recovery framework (19.6/19.7/19.8 own those).

## 38. Confirmation that 19.6-19.9 were NOT implemented

Confirmed. No setup lifecycle, no alerts, no forward-testing framework,
no reliability/recovery framework, no persistent workers were introduced.
The 19.5 layer is a clean point-in-time assessment boundary that 19.6 can
build a lifecycle on top of.

## 39. Final architectural assessment

- Setup-quality intelligence consumes the frozen 19.4 MTF layer (by
  reference) and the frozen 19.1 universe.
- Setup detection reuses the proven 11O + 11Q engines verbatim.
- The quality score is deterministic, bounded, component-transparent, and
  reproducible; every component has a documented meaning.
- Negative evidence and data-quality prerequisites are explicit.
- Point-in-time safety is structural + regression-tested (adversarial).
- Ranking is deterministic with deterministic tie-breaking; universe-wide
  evaluation supports the NIFTY Top 200 with per-symbol failure
  isolation.
- No existing module was modified; no duplicate analytical engine or
  abstraction was created.

## 40. Final verdict

PASS. Checkpoint 19.5 is complete: the setup-quality intelligence layer
meets all 25 success criteria, adds 83 deterministic offline tests and a
15-check demo, preserves the 19.1-19.4 regression baseline (full suite
6673 passed vs 6590, +83 net, 0 unrelated failures), and introduces NO
trade execution, alerts, lifecycle, forward-testing, reliability, or
broker artifacts. Broker execution remains completely deferred.