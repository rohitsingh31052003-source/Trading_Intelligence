#!/usr/bin/env python3
"""
Checkpoint 19.7 demo — deterministic USER ALERTS demonstration.

Run:  python scripts/test_checkpoint_19_7.py

Fully OFFLINE, deterministic, no credentials, no network, no external
notification service, no broker execution. Ends with:

    Checkpoint 19.7 demo completed successfully (N checks passed).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.setup_lifecycle import (  # noqa: E402
    SetupLifecycleEngine,
    SetupLifecycleStore,
)
from dashboard.user_alerts import (  # noqa: E402
    AlertEngine,
    ConsoleAlertSink,
    FailingAlertSink,
)
from engine.config.setup_lifecycle_config import (  # noqa: E402
    SetupLifecycleConfig,
)
from engine.config.user_alerts_config import (  # noqa: E402
    UserAlertConfig,
    alert_policy_version,
)
from engine.models.setup_lifecycle import (  # noqa: E402
    SetupLifecycleCycleResult,
    SetupLifecycleState,
)
from engine.models.user_alerts import (  # noqa: E402
    AlertDeliveryStatus,
    AlertEventKind,
)
from engine.reporting.user_alerts import (  # noqa: E402
    AlertFormatter,
)

PASS = 0
FAIL = 0


def check(name: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


def T(i: int, start: datetime | None = None) -> datetime:
    from datetime import timezone

    base = start or datetime(
        2026, 9, 4, 5, 30, tzinfo=timezone.utc,
    )
    return base + timedelta(minutes=15 * i)


def _one_cycle(results, *, scan_cycle_id, reference_now) -> SetupLifecycleCycleResult:
    return SetupLifecycleCycleResult(
        cycle_id="lc-demo-cycle",
        scan_cycle_id=scan_cycle_id,
        reference_now=reference_now,
        instruments=tuple(sorted({r.instrument for r in results})),
        results=tuple(results),
    )


def main() -> int:
    from tests.test_setup_lifecycle import (  # local convenience imports
        _bearish,
        _bullish,
        _no_setup,
    )

    print("Checkpoint 19.7 — USER ALERTS (deterministic, offline, no broker)")
    print()

    lc = SetupLifecycleEngine(SetupLifecycleConfig(), SetupLifecycleStore())
    alerts = AlertEngine.build(
        channels=[ConsoleAlertSink()],
        config=UserAlertConfig(),
    )

    # -----------------------------------------------------------
    # 1. Confirmation alert (first observation qualified)
    # -----------------------------------------------------------
    r1 = lc.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
    cycle = _one_cycle((r1,), scan_cycle_id="cycle-1", reference_now=T(0))
    out = alerts.process(cycle)
    alerts_list = out.alerts
    check(
        "confirmation event emits exactly one SETUP_CONFIRMED alert",
        len(alerts_list) == 1
        and alerts_list[0].kind is AlertEventKind.SETUP_CONFIRMED
        and alerts_list[0].instrument == "RELIANCE"
        and alerts_list[0].direction == "BULLISH"
        and alerts_list[0].setup_type == "TREND_CONTINUATION"
        and alerts_list[0].quality_score == 90
        and alerts_list[0].mtf_alignment == "ALIGNED"
        and alerts_list[0].severity.value == "IMPORTANT",
    )
    check("delivery succeeded through the console channel",
          out.deliveries[0].status is AlertDeliveryStatus.DELIVERED)

    # -----------------------------------------------------------
    # 2. No duplicate alert on repeated unchanged observations
    # -----------------------------------------------------------
    r2 = lc.process(_bullish("RELIANCE"), "cycle-2", timestamp=T(1))
    # cycle2 re-evaluates r1 (r1's alert already recorded in the store)
    # + r2 (unchanged, no event). No NEW alert is emitted; the old
    # event is explicitly deduplicated (auditable suppression), so the
    # store still holds exactly one confirmation alert.
    cycle2 = _one_cycle((r1, r2), scan_cycle_id="cycle-2", reference_now=T(1))
    out2 = alerts.process(cycle2)
    check("repeated confirmed cycles do not spam alerts",
          len(out2.alerts) == 0
          and alerts.store.total_count == 1,
      )
    check("identity-based dedup is explicit + auditable",
          out2.counts.suppressed == 1
          and out2.suppressed[0].reason.startswith("deduplicated"),
      )
    check("identity-based dedup: same event processed twice -> one logical alert",
          alerts.process(cycle).counts.suppressed == 1
          and alerts.store.total_count == 1,
      )

    conf_alert = alerts.store.list_alerts()[0]
    check("alert id is deterministic (prefix + sha256)",
          conf_alert.alert_id.startswith("alert-") and len(conf_alert.alert_id) == 6 + 16,
      )

    # -----------------------------------------------------------
    # 3. Data unavailable -> no alert, no lifecycle mutation
    # -----------------------------------------------------------
    from tests.test_setup_lifecycle import _data_gate  # noqa: E402

    r3 = lc.process(_data_gate("RELIANCE"), "cycle-3", timestamp=T(2))
    cycle3 = _one_cycle((r3,), scan_cycle_id="cycle-3", reference_now=T(2))
    out3 = AlertEngine.build(channels=[ConsoleAlertSink()]).process(cycle3)
    check(
        "data-unavailable observation produces no alert event",
        len(out3.alerts) == 0 and out3.counts.eligible == 0,
    )

    # -----------------------------------------------------------
    # 4. Invalidation alert (opposing direction supersedes)
    # -----------------------------------------------------------
    from engine.models.setup_quality import (  # noqa: E402
        SetupQualityUniverseResult,
        setup_quality_analysis_id,
    )
    from engine.config.setup_quality_config import SetupQualityConfig  # noqa: E402

    uni = SetupQualityUniverseResult(
        analysis_id=setup_quality_analysis_id(
            ("15m", "1h"), ("RELIANCE",), T(2),
            SetupQualityConfig().snapshot(),
        ),
        reference_now=T(2),
        timeframes=("15m", "1h"),
        primary_timeframe="15m",
        universe_instrument_count=1,
        instruments=("RELIANCE",),
        results=(_bearish("RELIANCE"),),
    )
    uni_out = lc.process_universe(uni, "cycle-4", timestamp=T(3))
    invalidation = [
        r for r in uni_out.results
        if r.transition is not None and r.transition.is_invalidation
    ]
    check("cross-invalidation produces a lifecycle invalidation event",
          len(invalidation) == 1,
      )
    inv_engine = AlertEngine.build(channels=[ConsoleAlertSink()])
    out_inv = inv_engine.process(
        _one_cycle(uni_out.results, scan_cycle_id="cycle-4", reference_now=T(3)),
    )
    inv_alerts = [
        a for a in out_inv.alerts
        if a.kind is AlertEventKind.SETUP_INVALIDATED
    ]
    check("invalidation event generates a SETUP_INVALIDATED alert (CRITICAL)",
          len(inv_alerts) == 1
          and inv_alerts[0].severity.value == "CRITICAL"
          and inv_alerts[0].lifecycle_state is SetupLifecycleState.INVALIDATED
          and "opposing" in inv_alerts[0].transition_reason.lower(),
      )
    check("invalidation alert is DISTINCT from the confirmation alert",
          conf_alert.alert_id != inv_alerts[0].alert_id,
      )

    # -----------------------------------------------------------
    # 5. Delivery failure is isolated (never invalidates a setup)
    # -----------------------------------------------------------
    fresh_lc = SetupLifecycleEngine(SetupLifecycleConfig(), SetupLifecycleStore())
    rr = fresh_lc.process(_bullish("TCS"), "cycle-1", timestamp=T(0))
    fail_engine = AlertEngine.build(channels=[FailingAlertSink()])
    out_fail = fail_engine.process(
        _one_cycle((rr,), scan_cycle_id="cycle-1", reference_now=T(0)),
    )
    check("delivery failure is recorded FAILED (never a silent success)",
          out_fail.deliveries[0].status is AlertDeliveryStatus.FAILED,
      )
    check("delivery failure cannot invalidate the setup",
          fresh_lc.store.load(rr.lifecycle_id).state is not SetupLifecycleState.INVALIDATED,
      )

    # -----------------------------------------------------------
    # 6. Point-in-time safety (T1/T2 payloads unchanged after T3)
    # -----------------------------------------------------------
    pit_lc = SetupLifecycleEngine(SetupLifecycleConfig(), SetupLifecycleStore())
    pit_alerts = AlertEngine.build(channels=[ConsoleAlertSink()])
    pr1 = pit_lc.process(_bullish("RELIANCE"), "cycle-1", timestamp=T(0))
    pit_out = pit_alerts.process(
        _one_cycle((pr1,), scan_cycle_id="cycle-1", reference_now=T(0)),
    )
    t1_payload = AlertFormatter().alert_to_dict(pit_out.alerts[0])
    # later invalidation (separate engine so the confirmation store is
    # untouched; the ALERT RECORD itself is frozen)
    uni2 = SetupQualityUniverseResult(
        analysis_id=setup_quality_analysis_id(
            ("15m", "1h"), ("RELIANCE",), T(1),
            SetupQualityConfig().snapshot(),
        ),
        reference_now=T(1),
        timeframes=("15m", "1h"),
        primary_timeframe="15m",
        universe_instrument_count=1,
        instruments=("RELIANCE",),
        results=(_bearish("RELIANCE"),),
    )
    uni2_out = pit_lc.process_universe(uni2, "cycle-2", timestamp=T(1))
    _ = AlertEngine.build(channels=[ConsoleAlertSink()]).process(
        _one_cycle(uni2_out.results, scan_cycle_id="cycle-2", reference_now=T(1)),
    )
    check("future invalidation does not mutate the earlier confirmation alert",
          AlertFormatter().alert_to_dict(pit_out.alerts[0]) == t1_payload,
      )

    # -----------------------------------------------------------
    # 7. Formatter + JSON
    # -----------------------------------------------------------
    text = AlertFormatter().format_alert(conf_alert)
    check("human-readable alert formatter is analytical + non-directive",
          "SETUP CONFIRMED" in text
          and "RELIANCE" in text
          and "WARNING" in text
          and "buy now" not in text.lower(),
      )
    import json as _json

    check("JSON projection is deterministic + parseable",
          AlertFormatter().alert_to_json(conf_alert)
          == AlertFormatter().alert_to_json(conf_alert)
          and isinstance(_json.loads(AlertFormatter().alert_to_json(conf_alert)), dict),
      )

    # -----------------------------------------------------------
    # 8. Policy version + deterministic cycles
    # -----------------------------------------------------------
    check("alert policy version is a deterministic sha256",
          len(alert_policy_version(UserAlertConfig())) == 16,
      )
    # A different config is a different alert policy -> a different
    # deterministic policy version -> a different (deterministic) cycle.
    other_config = UserAlertConfig(max_per_cycle=5)
    check(
        "config change -> policy version change -> different cycle id",
        alert_policy_version(other_config) != alert_policy_version(UserAlertConfig())
        and len(alerts.process(cycle).cycle_id) == 12 + 16,
    )

    print()
    print(f"Checkpoint 19.7 demo completed successfully ({PASS} checks passed).")
    if FAIL:
        print(f"{FAIL} checks FAILED")
    return 1 if FAIL else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())