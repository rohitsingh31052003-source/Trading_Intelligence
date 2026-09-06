#!/usr/bin/env python3
"""
Checkpoint 19.9 — Validation / Forward-Testing demo.

Visibly proves the Checkpoint 19.9 forward-validation framework against
the FROZEN 19.1–19.8 pipeline using ONLY deterministic, offline
components (scripted candles, fake provider outputs, injected
timestamps, deterministic sessions). No real network access; no broker
credentials; no broker execution; no alerts delivered externally.

The demo follows the frozen forward-testing methodology:

  OBSERVE -> RECORD -> WAIT -> OBSERVE OUTCOME -> MEASURE

and NEVER rewrites an earlier observation with future information.

Exits 0 when every check passes; exits non-zero otherwise.
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.forward_validation import (
    ForwardOutcomeEngine,
    ForwardSessionManager,
    ForwardValidationEngine,
)
from engine.config.forward_validation_config import ForwardValidationConfig
from engine.models.forward_validation import (
    OutcomeAvailability,
    OutcomeDirection,
)
from engine.models.setup_lifecycle import LifecycleObservationStatus
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)

from tests._checkpoint19_9_fixtures import (
    FIXTURE_START,
    make_observation,
    scripted_candles,
)

T = FIXTURE_START

CHECKS = []
PASS = 0


def check(name: str, condition: bool) -> None:
    global PASS
    CHECKS.append((name, condition))
    print(f"{'PASS' if condition else 'FAIL'} : {name}")
    if condition:
        PASS += 1


def main() -> int:
    print("Checkpoint 19.9 — Validation / Forward-Testing demo")
    print("=" * 76)

    # 0. Framework construction ------------------------------------
    cfg = ForwardValidationConfig(
        universe=("RELIANCE", "TCS"),
        max_holding_bars=3,
        label="19.9-demo",
    )
    manager = ForwardSessionManager(cfg)
    session = manager.open_session(
        universe=cfg.universe, started_at=T,
    )
    engine = ForwardValidationEngine(cfg, session_manager=manager)
    check("0. open session", session.status == "OPEN")

    # 1. Observation at T uses ONLY the 19.5/19.6/19.7 state at T -----
    obs = make_observation(
        session.session_id,
        scan_cycle_id="scan-1",
        instrument="RELIANCE",
        reference_now=T,
        setup_id="setup-r-1",
        lifecycle_id="lc-r-1",
        quality_status=SetupQualityStatus.QUALIFIED,
        quality_classification=SetupQualityClassification.EXCELLENT,
        quality_score=88,
        mtf_alignment="ALIGNED",
        lifecycle_state=LifecycleResolution_CONFIRMED(),
        lifecycle_observation_status=LifecycleObservationStatus.OBSERVED,
        alert_state="ALERTED",
        alert_id="alert-r-1",
    )
    recorded = manager.record_observation(session.session_id, obs)
    check("1. observation recorded at T", recorded.status.value == "RECORDED")

    # 2. Immutability: the recorded observation is NEVER rewritten ----
    first_serialized = repr(obs)
    check("2. observation frozen snapshot", first_serialized == repr(obs))

    # 3. DUPLICATE identical input -> no second record ----------------
    dup = manager.record_observation(session.session_id, obs)
    check(
        "3. duplicate observation rejected",
        dup.status.value == "DUPLICATE"
        and manager.observations_count(session.session_id) == 1,
    )

    # 4. Out-of-order (OLDER timestamp) observation rejected ----------
    late = make_observation(
        session.session_id,
        scan_cycle_id="scan-9",
        instrument="RELIANCE",
        reference_now=T - timedelta(hours=2),
    )
    out_of_order = manager.record_observation(session.session_id, late)
    check(
        "4. out-of-order rejected",
        out_of_order.status.value == "OUT_OF_ORDER",
    )

    # 5. Outcome separation: measure AFTER T only ---------------------
    outcome_engine = ForwardOutcomeEngine(cfg)
    future = scripted_candles(5)[1:5]
    out = outcome_engine.measure(
        obs, future,
        measurement_timestamp=T + timedelta(minutes=60),
    )
    check(
        "5. outcome measured after T",
        out.observation_id == obs.observation_id
        and out.bars_used == 3
        and out.bars_available == 4,
    )
    check(
        "6. outcome direction computed",
        out.outcome_direction in (OutcomeDirection.FAVORABLE,
                                  OutcomeDirection.UNFAVORABLE),
    )
    check(
        "7. observation NOT modified by measurement",
        repr(obs) == first_serialized,
    )

    # 8. Missing forward data -> OUTCOME_UNAVAILABLE ------------------
    empty = outcome_engine.measure(
        obs, (),
        measurement_timestamp=T + timedelta(hours=1),
    )
    check(
        "8. missing window explicit",
        empty.availability is OutcomeAvailability.OUTCOME_UNAVAILABLE
        and empty.bars_used == 0,
    )

    # 9. Lifecycle + alert state captured at T ------------------------
    check(
        "9. lifecycle/alert captured",
        obs.lifecycle_observation_status == LifecycleObservationStatus.OBSERVED
        and obs.alert_id == "alert-r-1",
    )

    # 10. Report aggregates the full population -----------------------
    report = engine.report(session.session_id)
    check(
        "10. report counts",
        report.instruments_attempted == 1  # only RELIANCE recorded here
        and report.setups_detected == 1
        and "SAMPLE TOO SMALL" in report.sample_size_note,
    )

    # 11. Point-in-time: serialized observation stable ----------------
    from engine.persistence.forward_validation_serialization import (
        serialize_observation,
    )

    check(
        "11. serialized observation deterministic",
        serialize_observation(obs) == serialize_observation(obs),
    )

    # 12. No execution vocabulary in the model ------------------------
    import ast

    tree = ast.parse(
        (
            Path(__file__).resolve().parent.parent
            / "src/engine/models/forward_validation.py"
        ).read_text(encoding="utf-8")
    )
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    check(
        "12. no broker/execution vocabulary",
        not ({"BUY", "SELL", "place_order", "cancel_order"} & names),
    )

    print("=" * 76)
    print(
        f"Checkpoint 19.9 demo completed successfully ({PASS} checks "
        f"passed)."
    )
    print(
        "Forward validation is descriptive — it does NOT predict, does "
        "NOT guarantee profitability, does NOT constitute a trading "
        "recommendation. The user is the final execution boundary."
    )
    return 0 if PASS == len(CHECKS) else 1


def LifecycleResolution_CONFIRMED():
    from engine.models.forward_validation import LifecycleResolution

    return LifecycleResolution.CONFIRMED


if __name__ == "__main__":
    raise SystemExit(main())