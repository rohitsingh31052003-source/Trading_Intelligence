"""
Checkpoint 19.8 — operator diagnostic CLI tests
(``scripts/check_system_health.py``).

Deterministic / offline (fixture provider); the CLI itself uses the
frozen 19.1 NIFTY Top 200 universe manifest + the deterministic fixture
reference time. No network / credentials / broker execution.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "check_system_health.py"


def _run_cli(*args, timeout: int = 120):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class TestHealthCli:
    def test_fixture_run_ok(self):
        proc = _run_cli("--state-dir", str(ROOT / "data" / "operational_state_test"))
        assert proc.returncode == 0, proc.stderr

    def test_json_pure(self):
        proc = _run_cli("--json")
        assert proc.returncode == 0
        parsed = json.loads(proc.stdout)
        assert "health_state" in parsed
        assert "provider_health" in parsed
        assert "universe" in parsed

    def test_json_deterministic(self):
        a = _run_cli("--json")
        b = _run_cli("--json")
        assert a.returncode == b.returncode == 0
        # The default fixture run is deterministic at the fixed sentinel.
        assert json.loads(a.stdout) == json.loads(b.stdout)

    def test_top200_universe_default(self):
        proc = _run_cli("--json")
        assert "NIFTY Top 200" in json.loads(proc.stdout)["universe"]

    def test_custom_instruments(self):
        proc = _run_cli("--instruments", "RELIANCE,TCS", "--json")
        assert "custom (2)" in json.loads(proc.stdout)["universe"]

    def test_bad_reference_now(self):
        proc = _run_cli("--reference-now", "2026-09-07")
        assert proc.returncode == 2

    def test_load_state_missing(self):
        proc = _run_cli("--load-state", "--state-dir", str(ROOT / "data" / "ops_missing"))
        assert proc.returncode == 0

    def test_no_predictive_language(self):
        proc = _run_cli()
        assert proc.returncode == 0
        lowered = proc.stdout.lower()
        for bad in ("buy", "sell", "enter now", "exit now", "guarantee"):
            assert bad not in lowered

    def test_credentials_not_required(self):
        env = dict(os.environ)
        env.pop("UPSTOX_ANALYTICS_TOKEN", None)
        env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + str(ROOT)
        proc = subprocess.run(
            [sys.executable, str(CLI), "--json"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert proc.returncode == 0, proc.stderr


class TestHealthCliErrors:
    def test_empty_instruments(self):
        proc = _run_cli("--instruments", "  ,")
        assert proc.returncode == 2

    def test_bad_provider(self):
        proc = _run_cli("--provider", "nonsense")
        assert proc.returncode == 2