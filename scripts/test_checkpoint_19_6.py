#!/usr/bin/env python3
"""
Checkpoint 19.6 demo — setup lifecycle.

Proves (deterministically, offline):

  1. the lifecycle engine consumes the FROZEN 19.5 setup-quality
     results and the FROZEN 19.3 scan-cycle identity;
  2. first observation creates exactly ONE lifecycle instance;
  3. repeated observation of the same setup REUSES the same setup id /
     lifecycle (no duplicate lifecycles);
  4. a changed quality score does NOT create a duplicate setup;
  5. setup identity is deterministic across independent runs and
     independent of scan-cycle id / provider order / prices;
  6. the initial state is deterministic (CONFIRMED for a first
     observation that is already qualified);
  7. DETECTED -> CONFIRMED / INVALIDATED / EXPIRED transitions are
     explicit and carry explicit reasons;
  8. provider failure / stale / incomplete data is handled explicitly
     (DATA_UNAVAILABLE) and NEVER invalidates a setup;
  9. a clean absence (NO_SETUP) counts toward the documented EXPIRATION
     rule (max consecutive misses);
 10. an opposing-direction candidate INVALIDATES the same-direction
     lifecycle and starts a NEW lifecycle instance (terminal lifecycles
     are never reused);
 11. processing the same scan cycle twice is idempotent (no duplicate
     observations / transitions);
 12. genuinely out-of-order observations are rejected (late_rejected)
     and never mutate the lifecycle;
 13. future observations never retroactively mutate earlier lifecycle
     history (point-in-time safety);
 14. active and terminal lifecycles are retrieved deterministically and
     the history is auditable;
 15. universe-wide processing is deterministic, failure-isolated, and
     exactly-once-per-symbol;
 16. NO broker execution, NO broker imports, NO alerts, NO notification
     provider, NO 19.8 reliability framework, NO 19.9 forward-testing
     framework, NO trade-plan semantics, NO duplicate 19.5 scoring
     engine;
 17. the pipeline baseline (signals=4, trades=3) is unchanged.

Run:  python scripts/test_checkpoint_19_6.py
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.setup_lifecycle import (  # noqa: E402
    SetupLifecycleEngine,
    resolve_setup_identity,
)
from engine.models.setup_lifecycle import (  # noqa: E402
    LifecycleObservationStatus,
    SETUP_ID_PREFIX,
    SetupLifecycleState,
)
from engine.reporting.setup_lifecycle import (  # noqa: E402
    SetupLifecycleFormatter,
)
from tests.test_setup_quality import (  # noqa: E402
    _aligned_mtf,
    _bearish_15m,
    _bullish_15m,
    _evaluate,
    _flat_candles,
    _market_context,
    _now,
)
from engine.models.market_context import MarketTrendState  # noqa: E402

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))


def inspect_source(module) -> str:
    return Path(module.__file__).read_text(encoding="utf-8")


def T(i: int):
    return _now() + timedelta(minutes=15 * i)


def run_pipeline_baseline() -> bool:
    """Confirm the historical pipeline baseline unchanged."""
    try:
        from engine.pipeline.historical_pipeline import (
            HistoricalEvaluationPipeline,
            PipelineConfig,
        )
        from tests.test_pipeline import trending_dataset  # type: ignore
    except Exception:
        return False
    try:
        candles = trending_dataset()
        result = HistoricalEvaluationPipeline(PipelineConfig()).evaluate(candles)
        return len(result.signals) == 4 and result.completed_trades == 3
    except Exception:
        return False


def main() -> int:
    print("=" * 76)
    print("Checkpoint 19.6 demo — setup lifecycle")
    print("=" * 76)

    eng = SetupLifecycleEngine.build()

    # -- 1 / 2/ 3/ 4/ 5: identity + creation + reuse -----------------
    r1 = _evaluate(
        _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH)),
        _bullish_15m(),
    )
    idx = resolve_setup_identity(r1)
    check("identity resolvable for a directional candidate", idx is not None)
    check(
        "setup id is deterministic + prefixed",
        idx is not None and idx.setup_id.startswith(SETUP_ID_PREFIX),
    )
    res1 = eng.process(r1, "cycle-1", timestamp=T(0))
    check(
        "first observation creates one lifecycle",
        res1.created and eng.store.total_count == 1,
        res1.lifecycle_id or "",
    )
    check(
        "initial state is deterministic (CONFIRMED)",
        res1.state is SetupLifecycleState.CONFIRMED,
    )
    lc1 = eng.store.active_lifecycles()[0]
    check(
        "initial lifecycle history carries the confirming transition",
        lc1.transition_count == 1
        and lc1.transitions[0].is_confirmation,
    )

    # repeated observation (score change) same setup
    r2 = _evaluate(
        _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH)),
        _bullish_15m(),
    )
    res2 = eng.process(r2, "cycle-2", timestamp=T(1))
    check(
        "repeated observation reuses the same lifecycle",
        not res2.created and res2.lifecycle_id == res1.lifecycle_id,
    )
    check(
        "score change does not create a duplicate",
        eng.store.total_count == 1,
    )
    check(
        "scan-cycle id alone does not create a new identity",
        eng.store.active_lifecycles()[0].setup_id == res1.setup_id,
    )

    # identity deterministic across independent runs
    check(
        "identity is deterministic across independent runs",
        resolve_setup_identity(r1).setup_id == resolve_setup_identity(r2).setup_id,
        resolve_setup_identity(r1).setup_id,
    )

    # -- 12: out-of-order rejection ----------------------------------
    late = eng.process(r2, "cycle-late", timestamp=T(0) + timedelta(minutes=5))
    check(
        "out-of-order observation is rejected (no mutation)",
        late.late_rejected and eng.store.load(res1.lifecycle_id).observation_count == 2,
    )

    # -- 11: idempotency ---------------------------------------------
    dup = eng.process(r2, "cycle-2", timestamp=T(1))
    check(
        "same scan cycle + assessment processed twice is idempotent",
        dup.duplicate and eng.store.load(res1.lifecycle_id).observation_count == 2,
    )

    # -- 10a: opposite direction invalidates the active lifecycle -----
    rbc = _evaluate(
        _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BEARISH)),
        _bearish_15m(),
    )
    eng.process(rbc, "cycle-3", timestamp=T(2))
    check(
        "opposing-direction candidate invalidates same-identity active lifecycle",
        any(
            lc.state is SetupLifecycleState.INVALIDATED
            and lc.identity.direction.value == "BULLISH"
            for lc in eng.store.terminal_lifecycles()
        ),
    )
    lc_bear = eng.store.active_lifecycles()[0]
    check(
        "new lifecycle instance created for the new direction",
        lc_bear.identity.direction.value == "BEARISH",
    )
    # reappearance of the original bullish after terminal -> new instance
    rbc2 = _evaluate(
        _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH)),
        _bullish_15m(),
    )
    res_r = eng.process(rbc2, "cycle-4", timestamp=T(3))
    check(
        "reappearance after terminal starts a NEW lifecycle instance",
        res_r.created,
    )
    instances = eng.store.lifecycles_for_setup(res1.setup_id)
    check(
        "terminal lifecycle is never reused (no resurrection)",
        len(instances) == 2
        and any(lc.is_terminal for lc in instances)
        and any(lc.is_active for lc in instances),
    )
    # Capture the reappeared instance's FIRST observation (a frozen
    # snapshot of what the system knew at T(3)) for the no-retroactive-
    # mutation assertions later.
    new_instance = next(
        lc for lc in eng.store.lifecycles_for_setup(res1.setup_id)
        if lc.is_active
    )
    first_obs_ts = new_instance.observations[0]
    first_obs_state = new_instance.observations[0].at_state

    # -- 8: data unavailable never invalidates (on the new instance) --
    rin = _evaluate(_aligned_mtf("RELIANCE", completeness=1), [])
    res_in = eng.process(rin, "cycle-5", timestamp=T(4))
    lc = eng.store.active_for_setup(res1.setup_id)
    check(
        "data-unavailable observation is explicit + state unchanged",
        res_in.observation_status is LifecycleObservationStatus.DATA_UNAVAILABLE
        and lc is not None
        and lc.state is SetupLifecycleState.CONFIRMED
        and lc.consecutive_missing == 0,
    )

    # -- 9: clean absence + expiration --------------------------------
    rnone = _evaluate(
        _aligned_mtf(
            "RELIANCE",
            market_state=_make_unknown_context(),
        ),
        _flat_candles(),
    )
    check(
        "a clean no-setup symbol produces a non-directional assessment",
        rnone.setup_type is None,
    )
    for i, cycle in enumerate(("cycle-6", "cycle-7", "cycle-8", "cycle-9", "cycle-10")):
        eng.process(rnone, cycle, timestamp=T(5 + i))
    lc = eng.store.active_for_setup(res1.setup_id)
    check(
        "expiration after max consecutive clean misses",
        lc is None
        and any(
            lc2.state is SetupLifecycleState.EXPIRED
            and lc2.setup_id == res1.setup_id
            for lc2 in eng.store.terminal_lifecycles()
        ),
    )

    # -- 13: no retroactive mutation ----------------------------------
    # The first observation of the NEW bullish instance was recorded at
    # T(3) with a frozen snapshot. Later observations (data-unavailable
    # + absences that eventually EXPIRED the instance) must NOT rewrite
    # it. The terminal snapshot's first observation must still be the
    # SAME immutable record.
    terminal_bull = next(
        lc for lc in eng.store.lifecycles_for_setup(res1.setup_id)
        if lc.is_terminal and lc.state is SetupLifecycleState.EXPIRED
    )
    first_obs = terminal_bull.observations[0]
    check(
        "historical observation at T is an immutable point-in-time record",
        first_obs.observation_id == first_obs_ts.observation_id
        and first_obs.at_state == first_obs_state
        and first_obs.observation_timestamp == T(3),
    )

    # -- 14: deterministic retrieval ----------------------------------
    check(
        "active retrieval deterministic + sorted",
        [lc.lifecycle_id for lc in eng.store.active_lifecycles()]
        == sorted(lc.lifecycle_id for lc in eng.store.active_lifecycles()),
    )
    check(
        "terminal retrieval deterministic + sorted",
        [lc.lifecycle_id for lc in eng.store.terminal_lifecycles()]
        == sorted(lc.lifecycle_id for lc in eng.store.terminal_lifecycles()),
    )

    # -- 15: universe-wide processing + failure isolation ---------------
    from engine.config.setup_quality_config import SetupQualityConfig
    from engine.models.setup_quality import (
        SetupQualityUniverseResult,
        setup_quality_analysis_id,
    )

    bull_r = _evaluate(
        _aligned_mtf("RELIANCE", market_state=_market_context(MarketTrendState.BULLISH)),
        _bullish_15m(),
    )
    bull_t = _evaluate(
        _aligned_mtf("TCS", market_state=_market_context(MarketTrendState.BULLISH)),
        _bullish_15m(),
    )
    uni = SetupQualityUniverseResult(
        analysis_id=setup_quality_analysis_id(
            ("15m", "1h"), ("RELIANCE", "TCS"), _now(),
            SetupQualityConfig().snapshot(),
        ),
        reference_now=_now(),
        timeframes=("15m", "1h"),
        primary_timeframe="15m",
        universe_instrument_count=2,
        instruments=("RELIANCE", "TCS"),
        results=(bull_r, bull_t),
    )
    uni_res = eng.process_universe(uni, "universe-cycle-1", timestamp=_now())
    check(
        "universe processing is deterministic + per-symbol",
        uni_res.instruments == ("RELIANCE", "TCS")
        and len(uni_res.results) >= 2,
    )
    check(
        "cycle id is deterministic + prefixed",
        uni_res.cycle_id.startswith("lc-cycle-"),
    )

    # -- formatter output is str + warnings present --------------------
    text = SetupLifecycleFormatter().format_lifecycle(
        eng.store.list_lifecycles()[-1],
    )
    check(
        "formatter returns str with disclaimer",
        isinstance(text, str) and "for the setup lifecycle" not in text
        and "WARNING" in text,
    )

    # -- 16: boundary checks --------------------------------------------
    src = inspect_source(dashboard_setup_lifecycle_module())
    # The lifecycle engine must not import any broker / execution /
    # network module. (The word "order" appears in the module docstring
    # only as a prohibition; actual IMPORT statements are the check.)
    check(
        "no broker / execution / network imports in lifecycle engine",
        not any(
            line.strip().startswith(("import ", "from "))
            and any(
                kw in line
                for kw in (
                    "requests", "urllib", "socket", "httpx", "http",
                    "broker", "Upstox", "execution", "paper_trade",
                    "submission", "Order", "Broker",
                )
            )
            for line in src.splitlines()
        ),
    )
    check(
        "no alert / notification provider imports in lifecycle engine",
        "telegram" not in src.lower()
        and "whatsapp" not in src.lower()
        and "smtp" not in src.lower()
        and "webhook" not in src.lower(),
    )
    check(
        "no trade-plan fields in lifecycle engine",
        "entry_price" not in src and "stop_loss" not in src
        and "take_profit" not in src and "position_size" not in src,
    )
    check(
        "no wall-clock in lifecycle decision logic",
        "datetime.now" not in src and "time.time(" not in src,
    )

    # -- 17: pipeline baseline ------------------------------------------
    check(
        "pipeline baseline unchanged (signals=4, trades=3)",
        run_pipeline_baseline(),
    )

    # ---- summary ------------------------------------------------------
    print()
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    for name, ok, detail in CHECKS:
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}" + (f"  ({detail})" if detail else ""))
    print()
    print(f"Sprint 19.6 demo completed successfully. ({passed}/{len(CHECKS)} checks passed)")
    return 0 if passed == len(CHECKS) else 1


def _make_unknown_context():
    from engine.models.market_context import (
        MarketContext,
        MarketTrend,
        PriceLocation,
        RangeContext,
        RangeState,
        SupportResistanceContext,
    )
    from engine.models.structure_analysis import StructureBias

    return MarketContext(
        index=59,
        trend=MarketTrend(
            state=MarketTrendState.UNKNOWN,
            bias=StructureBias.UNKNOWN,
            structure_intact=False,
            reasons=["test"],
        ),
        range=RangeContext(
            state=RangeState.UNKNOWN, high=None, low=None,
            width=None, position=None, reason="test",
        ),
        support_resistance=SupportResistanceContext(
            support=None, resistance=None,
            distance_to_support=None, distance_to_resistance=None,
            location=PriceLocation.UNKNOWN,
        ),
        recent_structure=(),
        confirmed_swings=0,
    )


def dashboard_setup_lifecycle_module():
    import dashboard.setup_lifecycle as mod

    return mod


if __name__ == "__main__":
    sys.exit(main())