# Checkpoint 19.4 — Multi-Timeframe Analysis (MTF) Audit

**Status:** COMPLETE / **PASS** — deterministic, offline-provable
multi-timeframe **market-state** analysis on the frozen 19.1 universe,
19.2 intraday layer and 19.3 continuous-scanner data flow.

**Broker execution remains COMPLETELY DEFERRED throughout Checkpoint
19.x** (nothing execution-related was introduced; Checkpoints 13–18
remain frozen; the execution gate stays DISABLED).

**Live trading is NOT authorized.** This checkpoint does not generate
trade setups, entry signals, buy/sell decisions, or recommendations.

---

## 1. Objective

Build the Multi-Timeframe Analysis (MTF) foundation on top of the frozen
19.1 universe correctness, 19.2 reliable intraday data coverage and 19.3
continuous market scanning. The MTF layer answers:

> "What does the current market state of this instrument look like
> across the configured timeframes, and are those timeframe states
> aligned, mixed, or conflicting?"

MTF analysis is **market-state analysis only**. It explicitly does NOT
decide whether something is a high-quality trade (that is 19.5).

## 2. Scope

**Implemented (per the strict 19.4 scope):**

* multiple configured analysis timeframes (validated, canonicalized,
  ascending-duration only);
* multi-timeframe data acquisition through the canonical 19.2
  `DashboardDataProvider` boundary (one provider request per instrument
  per timeframe; unsupported frames never fetch);
* timeframe-specific normalized market state — reuses Sprint 11P
  `MarketContextEngine.analyze_at` on the completed-candle prefix of
  each frame (deterministic, point-in-time safe, descriptive);
* aggregation of timeframe states into a per-symbol MTF completeness +
  alignment/conflict classification;
* deterministic MTF result models (engine/models), an orchestration
  engine (dashboard/, consuming the canonical 19.2 coverage layer), a
  reporting formatter (engine/reporting), and an operator CLI
  (scripts/analyze_mtf.py);
* per-symbol and universe-wide MTF analysis with explicit handling of
  missing / stale / unsupported / provider-error / invalid frames;
* timestamp alignment WITHOUT requiring identical candle timestamps;
* completed-candle boundaries (reusing the 19.2
  `split_completed_candles` boundary semantics via the canonical
  19.2 per-timeframe coverage classifier);
* deterministic ordering, session awareness (reused 19.2 market-session
  helpers), configurable timeframe sets, and deterministic offline
  testing.

**NOT implemented (belongs to 19.5–19.9):** setup detection, setup
scoring, setup-quality ranking, trade ranking, entry signals, buy/sell
decisions, entry/stop/target prices, risk/reward, trade plans, setup
lifecycle, alerts/notifications, broker execution, automated trading,
portfolio management, the 19.8 reliability/recovery framework, and the
19.9 forward-testing framework.

## 3. Relationship to 19.1 (frozen)

The MTF analyzer consumes the frozen 19.1 NIFTY Top 200 universe
boundary directly:

* `UniverseBuilder.nifty200()` / `DEFAULT_NIFTY200_UNIVERSE` /
  `NIFTY200_SYMBOLS` are the single instrument source;
* the engine accepts a `UniverseDefinition`, a sequence of instrument
  names, or ``None`` (default = validated NIFTY Top 200);
* the universe is canonicalized (strip + upper), de-duplicated and
  deterministically sorted;
* every requested constituent appears exactly once in a universe-wide
  result (regression-tested against `len(NIFTY200_SYMBOLS) == 200`).

No second universe was created; the benchmark `NIFTY` index instrument
stays separate from the Top 200 stock set.

## 4. Relationship to 19.2 (frozen)

The MTF engine **consumes the canonical 19.2 intraday data layer** — it
does NOT call providers directly or build a parallel normalization path:

* every per-timeframe assessment goes through
  `dashboard.intraday_coverage.IntradayCoverageEngine.assess_instrument`
  (capability discovery → `provider.fetch` → canonical `OHLCVCandle`
  construction → completed-candle boundary via
  `split_completed_candles` → `DataValidator` re-validation → session
  aware freshness → intraday gap refinement);
* the per-timeframe availability vocabulary **reuses the 19.2
  `IntradayCoverageStatus` taxonomy verbatim** (SUPPORTED /
  UNSUPPORTED_TIMEFRAME / UNSUPPORTED_INSTRUMENT / VALID /
  VALID_WITH_GAPS / STALE / NO_DATA / EMPTY / INVALID_RESPONSE /
  PROVIDER_ERROR / TEMPORARILY_UNAVAILABLE);
* session/freshness logic is reused unchanged (the 19.2 documented
  holiday limitation remains — no calendar redesign);
* future-dated candles are counted and never used; forming candles are
  carried separately and never treated as completed.

No second provider abstraction was created. The engine takes a
`DashboardDataProvider` and builds a 19.2 `IntradayCoverageEngine` on
demand (dependency-injectable for tests).

## 5. Relationship to 19.3 (frozen)

The 19.3 `ContinuousScannerEngine.run_cycle()` is a per-universe
single-timeframe cycle. 19.4 reuses its **data-flow and per-symbol
failure-isolation conventions** rather than its single-frame loop:

* per-symbol fetch/analysis is isolated by a
  `try/except Exception` boundary (one frame error + one symbol error
  cumulative — never abort the universe result);
* deterministic universe ordering (sorted canonical symbols);
* reference-time-driven determinism (caller-supplied aware
  `reference_now`; the engine never calls wall-clock);
* scan-status vocabulary style is mirrored (per-symbol results always
  present; per-frame statuses always present).

The 19.3 scanner object itself is NOT modified. The MTF engine is an
additive consumer of the same canonical data layer the scanner consumes;
the operator CLI `scripts/analyze_mtf.py` demonstrates the data flow a
future 19.8+ consumer can call alongside the 19.3 scanner.

## 6. Existing analytical architecture (audit)

Repository-wide audit of reusable analytical components (conclusions):

* **Sprint 11P `MarketContextEngine`** (`engine/intelligence/
  market_context_engine.py`) — deterministic, descriptive
  market-state snapshot (swing structure → structure state →
  BULLISH/BEARISH/RANGE/UNKNOWN market trend + range detection +
  support/resistance + price location). **Reused.** It is
  point-in-time safe (`analyze_at(candles, index)` uses only
  `candles[:index+1]`), deterministic, and carries NO setup/trade
  semantics. It is used by the live Product Phase literally for the
  current dashboard/scanner path, so it is live-path-proven.
* **Sprint 11U `MTFAlignmentEngine`** — higher-context ↔ lower-direction
  alignment used by the 11U market scanner. Inspected but NOT reused:
  it maps ONE higher-timeframe context to ONE lower-timeframe decision
  direction (not a multi-frame aggregation of same-kind states), and
  its vocabulary/mapping is tailored to the 11U opportunity flow. 19.4
  needs a same-kind multi-frame aggregation; the 11U `MarketContext`
  constructs are still reused as the per-frame state payload.
* **Sprint 11O candle patterns / 11Q setup confluence / 11R trade
  candidate / 11S trade decision** — explicitly NOT imported (they are
  setup/decision layers; 19.5's domain).
* **Sprint 11W outcome evaluator / 11X analytics / 11Y evidence / 11Z
  strategy / 12x decision intelligence** — research / evidence layers,
  NOT imported.
* **Sprint 11F–11N / checkpoints 12–18** — trading-pipeline and
  execution layers, NOT imported.

**Design decision:** 19.4 does NOT rewrite swing/structure analysis.
The per-timeframe "market state" is the reusable Sprint 11P
`MarketContext` (trend + range + structure), computed on the completed
prefix of each timeframe with the canonical engine.

## 7. Existing timeframe architecture (audit)

* Canonical intraday timeframes exist in `engine/data/historical_times.py`:
  `HISTORICAL_TIMEFRAME_SECONDS` (1m/2m/3m/5m/15m/30m/1h/90m/4h/1D),
  `canonical_timeframe(label)` (aliases 15M→15m, 1D→1d, 60m→1h; unknown
  → None, never guessed), `timeframe_seconds`, `supported_timeframes`.
* `MarketScanConfig` (Sprint 11U) uses `context_timeframe="1D"` +
  `setup_timeframe="15M"`.
* `ResearchCorpusConfig` (Phase 6C) uses `setup_timeframe="15m"` +
  `context_timeframe="1D"`.
* 19.2 `INTRADAY_TIMEFRAMES` (canonical set minus 1D) + `market_session`
  helpers + `IntradayCoverageEngine` per-frame classifier.
* 19.1 provider symbol maps for Yahoo (all 200) + Upstox (verified 5).

## 8. Existing market-state analysis

The Sprint 11P `MarketContext` is the de-facto canonical market-state
snapshot (it already powers the Product dashboard). `TrendEngine`
(BOS/CHOCH-driven) exists in the historical signal pipeline only and is
NOT live-path-proven; 19.4 deliberately reuses the live-path-proven
Sprint 11P `MarketTrendState` for the per-frame descriptive market-state
token.

## 9. Provider timeframe capabilities

Audited via the frozen 19.2 `supports_instrument` /
`is_timeframe_supported` / `fetch` contract and capability-discovery
(`IntradayCoverageEngine.provider_capabilities`):

| Provider | Intraday intervals | Notes |
| --- | --- | --- |
| Fixture (default, offline) | 15m ONLY | deterministic; 4 fixture instruments (RELIANCE/TCS/HDFCBANK/ICICIBANK) |
| Yahoo (OPT-IN live) | 1m/2m/5m/15m/30m/60m/1h/90m/1D native | requires `yfinance`; symbol map covers all 200 |
| Upstox (historical-only) | 15m, 1D | verified instrument-key map (5) |

## 10. Selected MTF timeframe set

**Primary set: `("15m", "1h")`** — the smallest set supported by the
existing architecture for live multi-timeframe intraday analysis:

* `15m` — the canonical 19.2/19.3 layout intraday frame (provider
  native on fixture + Yahoo + Upstox historical).
* `1h` — a distinct higher intraday frame, **provider-native** on the
  OPT-IN Yahoo live provider (no resampling needed; point-in-time risk
  minimised). It is honestly UNSUPPORTED by the offline fixture, so the
  default fixture path explicitly reports INCOMPLETE — never fabricates
  alignment.

**Configuration is future-proof:** `MtfAnalysisConfig(timeframes=...)`
accepts any non-empty, strictly ascending-duration subset of the
canonical intraday set (e.g. `("5m","15m","1h")`); every configured
frame yields an explicit per-frame result. Unknown / duplicate /
unordered / 1D / non-canonical-labels are rejected at construction.

## 11. Rationale for selected timeframes

* 19.2/19.3 and the Product dashboard already use 15m intraday data
  (the real-time chart works on 15m), so 15m is the natural "setup /
  layout" frame.
* `1h` gives a strictly higher intraday observation frame while still
  forming fast enough to be useful in an intraday session and is native
  to the only live provider (Yahoo), so no resampling is required.
* Starting with two frames keeps the system honest and simple; adding
  `5m` later is purely configuration (validated unit tests cover 3+
  frame configs).
* Fixture-only runs (the default, fully offline) must show 1h as
  UNSUPPORTED → INCOMPLETE so the tool never "looks complete" without
  live data.

## 12. Data acquisition design

* Per analysis at reference time `T`: for each symbol (sorted) and each
  configured timeframe (ascending duration), ONE provider request
  200 × N frames logical requests per full-universe cycle (doc:
  `symbol_count × frame_count`).
* The engine first checks `is_timeframe_supported`; a frame the provider
  does not support is recorded `UNSUPPORTED_TIMEFRAME` WITHOUT calling
  `fetch` (efficiency + honest capability discovery).
* `supports_instrument` is likewise consulted; unsupported instruments
  are recorded `UNSUPPORTED_INSTRUMENT` without fetching.
* Otherwise `provider.fetch(...)` is invoked with the canonical 19.2
  signature and the raw response is normalized by the reused 19.2
  `IntradayCoverageEngine` (completed-candle boundary → validation →
  freshness → gaps).

No batching/caching layer was built (19.8's domain). Duplicate requests
are avoided: exactly one fetch per (symbol, timeframe) per analysis.

## 13. Timeframe normalization

Each configured label is canonicalized via the reused
`engine.data.historical_times.canonical_timeframe` (the models layer
uses a lazy import helper so the model module never pulls `engine.data`
at import time — repo convention for model modules). The config stores
the canonical, ordered, de-duplicated tuple.

## 14. Completed-candle policy

Relying on the reused 19.2 boundary (`split_completed_candles` inside
`IntradayCoverageEngine.assess_instrument`):

* a candle is COMPLETED when `timestamp + duration <= T` (aware-UTC);
* the currently-FORMING candle (`open <= T < close`) is carried
  separately (`forming_setup_candle`) and is never supplied to the
  market-state engine;
* future-dated candles are excluded and counted (honest reporting).
* Per-frame the MTF layer uses `IntradayCoverageStatus`:
  `STALE` (completed but old) vs `VALID` (completed + fresh) are both
  "completed, usable" for market-state; the frame is still marked stale
  so downstream consumers know the data is old.

## 15. Point-in-time safety

* Every analysis is anchored to an explicit timezone-aware
  `reference_now`; the engine never reads wall-clock.
* Each frame's market state is computed by
  `MarketContextEngine.analyze_at(completed_prefix, index=-1)` where the
  prefix is exactly the completed candles ≤ T for that frame.
* No future candle, future higher-timeframe close, or forming candle is
  ever supplied.
* Proven by tests (Sections 26): appending future candles to ANY frame
  does not change the market-state, completeness, alignment, reason, or
  analysis id at an earlier fixed T (per-symbol AND universe-wide); a
  "future" 1h candle opening exactly at T (closing after T) is excluded;
  the 15m boundary candle closing exactly at T IS included.

Documented in `docs/checkpoint_19_4_...` + tests.

## 16. Timestamp alignment

**Alignment rule (documented + tested):** each frame contributes its OWN
latest completed candle at T. States are aligned point-in-time by
reference instant `T`, NOT by requiring identical candle timestamps. A
5m frame whose latest completed candle is `05:15 UTC` aligns with a 1h
frame whose latest completed candle is `04:00 UTC` because both are "the
latest completed candle at T" for that frame (T = 05:30 UTC). The rule
never uses future information; it only asks "what did this frame's most
recent completed state look like at T?".

## 17. Missing/stale/unsupported timeframe handling

Every requested timeframe produces an explicit `MtfTimeframeState`:

* `availability` reuses the 19.2 `IntradayCoverageStatus` (never a
  parallel vocabulary);
* `market_state_available=False` + `market_state=None` for
  unsupported / error / insufficient / absent frames — a frame is never
  silently dropped;
* per-symbol `timeframe_count == len(configured frames)` always;
* completeness: `COMPLETE` (all usable) / `INCOMPLETE` (some frame has
  no usable market-state) / `UNSUPPORTED` (all frames unsupported) /
  `UNAVAILABLE` (no frame usable for another reason).

## 18. Per-symbol MTF result design

`PerSymbolMtfResult` (frozen dataclass):

```
instrument
timeframes          (configured, canonical order)
reference_now
market_session      (reused 19.2 session label)
timeframe_states    (one per configured frame, canonical order)
completeness
alignment
alignment_reason
is_complete / alignment_is_aligned / ...
```

Per-frame `MtfTimeframeState`:

```
timeframe | availability (19.2 status) | coverage (19.2 detail)
completed_candle_count | latest_completed_timestamp |
latest_completed_candle | forming_present | data_age_seconds
market_state (Sprint 11P MarketContext | None)
market_state_available | direction (MTFDirection) | reason
```

## 19. Universe-wide MTF design

`MtfUniverseAnalysis` (frozen): `analysis_id` (
`"mtf-"+sha256[:16]` of canonical identity) + instruments/timeframes +
per-symbol results (sorted, exactly once) + counts:
`tested / complete / incomplete / unsupported / unavailable` and
`aligned / mixed / conflicting / alignment_incomplete /
alignment_unavailable` + `completeness_ratio`. Partial coverage is never
reported as full; the fixture universe reports `complete == 0` honestly.

## 20. Alignment / conflict classification

`classify_alignment(states) -> (MtfAlignmentState, reason)` — pure,
deterministic, direction-symmetric:

* per-frame directional token: `MTFDirection` derived ONLY from the
  Sprint 11P descriptive trend (BULLISH/BEARISH → directional;
  RANGE/NEUTRAL → NEUTRAL frame; no structure/data → UNKNOWN);
* usable = frame has a market state;
* **ALIGNED** — all usable frames have the same directional token;
* **CONFLICTING** — ≥2 usable frames observe opposite directional
  tokens (bull vs bear);
* **MIXED** — ≥2 usable frames, not all same, no opposite pair
  (e.g. directional + NEUTRAL/RANGE, or NEUTRAL + NEUTRAL);
* **INCOMPLETE** — <2 usable frames but ≥1 usable (full relationship
  unassertable);
* **UNAVAILABLE** — no usable frame with any market state.

This is a TIMEFRAME-RELATIONSHIP classification. It never means
BUY/SELL/TRADE/NO-TRADE (tests assert those strings never appear).

## 21. Failure isolation

* per-frame: `try/except` around `assess_timeframe` → `PROVIDER_ERROR`
  state; symbol result still contains all frames;
* per-symbol: `try/except` around `MtfAnalysisEngine.analyze_symbol` →
  keep-all-frames result with an INCOMPLETE/cumulative error;
* universe: each symbol independent; one raise never aborts others;
* the 19.2 provider fetch itself isolates + normalizes malformed rows
  before the MTF layer ever sees them.

## 22. Determinism

* instrument ordering: canonical sorted;
* timeframe ordering: config order (ascending duration);
* classification / reason strings: pure functions of reused states;
* serialization: `MtfAnalysisViewBuilder.analysis_to_jsonable` sorted
  keys; formatter deterministic;
* analysis id: deterministic hash of canonical inputs (reference now
  included) — same instant + data ⇒ same id;
* tests: repeated runs, shuffled input order, shuffled-frames none
  (config enforces order).

## 23. Data-request / efficiency assessment

200 × 2 frames = 400 logical provider requests per full-universe MTF
cycle (documented in the CLI/doc). No premature caching built (19.8
domain). Efficiency measures already present: `is_timeframe_supported`
short-circuits (no fetch for unsupported frames); one fetch per
(symbol, frame) per analysis; provider response ordering never affects
results (canonical rebuild inside 19.2 layer). The fixture default
(`DASHBOARD_PROVIDER=fixture`) is fully offline and deterministic — the
live Yahoo intake is an explicit operator opt-in.

## 24. Changes implemented (files)

| File | Type | Purpose |
| --- | --- | --- |
| `src/engine/models/mtf_analysis.py` | NEW | MTF result/enum/alignment models + pure helpers (`classify_alignment`, `compute_completeness`, `mtf_analysis_id`); lazy `engine.data.historical_times` import inside `_canonical_timeframe` |
| `src/engine/config/mtf_analysis_config.py` | NEW | `MtfAnalysisConfig` + `canonicalize_timeframes` (validated, canonical, ascending) |
| `src/dashboard/mtf_analysis.py` | NEW | `MtfAnalysisEngine` orchestration (consumes canonical 19.2 layer + Sprint 11P `MarketContextEngine`); `MtfAnalysisViewBuilder` |
| `src/engine/reporting/mtf_analysis.py` | NEW | `MtfAnalysisFormatter` (str, deterministic, DISCLAIMER) |
| `scripts/analyze_mtf.py` | NEW | operator CLI (fixture default/offline; `--json`; deterministic ref time) |
| `tests/test_mtf_analysis.py` | NEW | 71 focused tests |
| `tests/test_mtf_analysis_cli.py` | NEW | 13 CLI tests |
| `scripts/test_checkpoint_19_4.py` | NEW | demo (21 PASS) |
| `docs/checkpoint_19_4_multi_timeframe_analysis_audit.md` | NEW | this document |
| `AGENTS.md` | APPEND | this checkpoint section |

No existing engine/model modified. No frozen 19.x file modified.

## 25. Tests added

* `tests/test_mtf_analysis.py` (71) — universe acceptance (200), config
  validation (duplicates / unknown / unordered / aliases), explicit
  per-timeframe states (valid/stale/provider-error/unsupported/
  insufficient/invalid), completed-candle + forming + future rejection,
  anti-lookahead (per-symbol + universe), timestamp alignment without
  identical closes, ALIGNED/MIXED/CONFLICTING/INCOMPLETE/UNAVAILABLE
  classification, failure isolation, determinism, id stability,
  efficiency (single fetch per frame), no forbidden semantics, no
  broker/network imports, pipeline baseline guard.
* `tests/test_mtf_analysis_cli.py` (13) — fixture run, default universe
  (200), JSON purity, deterministic JSON, unsupported-frame explicit,
  bad args exit 2, manual timeframe set, no BUY/SELL/setup language.

## 26. Tests executed and results

| Suite | Result |
| --- | --- |
| `tests/test_mtf_analysis.py` (new) | **71 passed** |
| `tests/test_mtf_analysis_cli.py` (new) | **13 passed** |
| 19.1 `tests/test_nifty200_universe.py` | **48 passed** |
| 19.2 `tests/test_market_session.py test_intraday_coverage.py test_intraday_coverage_yahoo.py test_intraday_coverage_cli.py` | **200 passed** |
| 19.3 `tests/test_continuous_scanner.py test_scan_market_cli.py` | **53 passed** |
| market-data/provider/dashboard subset (dashboard, live_intraday, watchlist_scanner, workstation, yahoo_range_fix, upstox_historical, historical_data_foundation/availability/consumer, corpus_preparation/ingestion/audit, research_corpus) | **905 passed** |
| `scripts/test_checkpoint_19_4.py` demo | **21 PASS / 0 FAIL** |
| **FULL SUITE** | **see §27** |

All deterministic and network-free (fixture + scripted providers).

## 27. Full suite result (vs 19.3 baseline)

* 19.3 baseline (documented): **6506 passed / 12 skipped / 2 warnings**
  (12 skips = opt-in real-broker/sandbox tests; 2 third-party
  deprecation warnings).
* Full suite collected: **6602 tests** (6518 baseline + 84 new).
* Full suite result after 19.4: **6590 passed / 12 skipped / 2 warnings
  / 0 failed** (`python -m pytest tests/ -q`, ~123 s). This is exactly
  the 19.3 baseline **6506 passed** + **84 new tests** (71 engine + 13
  CLI), with the same 12 opt-in skips and the same 2 pre-existing
  third-party deprecation warnings (StarletteDeprecationWarning +
  anyio BlockingPortal). **0 failures; zero regression.**

19.1–19.3 suite numbers unchanged; pipeline baseline signals=4/trades=3
verified in the demo.

## 28. Live validation, if any

None performed. Live Yahoo MTF analysis is an explicit operator opt-in
(`--provider yahoo`) and is NOT claimed to have been demonstrated for
the full NIFTY Top 200. The fixture/offline path is fully deterministic
and is the default.

## 29. Limitations

* Fixture provider supports only 15m → the default 1h frame is honestly
  UNSUPPORTED → default MTF results are INCOMPLETE (never fabricated).
* 19.2 documented exchange-holiday limitation preserved (session
  arithmetic, no calendar).
* No caching/batching/persistence of MTF runs (deferred to 19.8).
* No live validation (opt-in only; not claimed).
* Sequential per-frame fetches (rate-limit respectful but no 19.8
  retry/backoff framework).
* `NIFTY` index is not a Top-200 constituent (a benchmark; treated as a
  separate instrument when explicitly requested).

## 30. Confirmation: setup intelligence was NOT implemented

Confirmed. No setup detection/scoring/ranking, no opportunity quality,
no trade direction, no entry/stop/target, no risk/reward anywhere in the
new modules (`src/engine/models/mtf_analysis.py`,
`src/dashboard/mtf_analysis.py`, `src/engine/reporting/mtf_analysis.py`,
`scripts/analyze_mtf.py`). The only "setup" strings are docstrings
stating the layer is NOT setup intelligence.

## 31. Confirmation: 19.5–19.9 functionality was NOT implemented

Confirmed. Nothing for 19.5 (setup-quality), 19.6 (setup lifecycle),
19.7 (alerts/notifications), 19.8 (reliability/recovery/observability
framework beyond normal error handling), or 19.9 (forward-testing) was
added. No alerts, lifecycle, ranking, persistence framework, watchdog or
forward-test harness exists.

## 32. Broker-execution boundary confirmation

Confirmed. No order placement, broker SDK, credentials, network path to
any broker, position or portfolio code was added. Checkpoints 13–18 stay
frozen; the execution gate stays disabled. The MTF layer imports none of
those modules (AST-tested: no broker/execution/order/position imports).

## 33. Final verdict

**PASS** — the MTF market-state layer:

1. accepts the frozen NIFTY Top 200 universe and consumes the canonical
   19.2 data layer (capability discovery, validation, freshness);
2. integrates cleanly with the 19.3 data flow (same provider boundary,
   same failure-isolation conventions, no scanner modification);
3. configures + validates timeframes explicitly and deterministic
   (ascending order, no duplicates/unknowns);
4. keeps every requested timeframe and every symbol explicit;
5. respects completed-candle boundaries and is point-in-time safe
   (proven by anti-lookahead tests at symbol + universe level);
6. aligns timestamps deterministically WITHOUT identical closes;
7. classifies ALIGNED / MIXED / CONFLICTING / INCOMPLETE / UNAVAILABLE
   as pure timeframe relationships (never trade verdicts);
8. contains per-symbol + universe-wide aggregation with explicit
   coverage counts;
9. isolates failures (one frame / one symbol never destroys the rest);
10. is fully deterministically testable offline (84 new tests + 21
    demo checks, all green) with no regression in 19.1/19.2/19.3;
11. introduces NO setup intelligence / lifecycle / alerts / broker
    execution and leaves 19.5–19.9 untouched.

**Live trading is NOT authorized and broker execution remains
deferred.**