"""
Checkpoint 19.9 — forward-validation operator CLI tests.

Deterministic, offline subprocess tests over ``scripts/forward_validation.py``:
start / demo / report sub-commands, exit codes, JSON purity + determinism,
no BUY/SELL/recommendation language, broker-boundary banner.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "forward_validation.py"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=60,
    )


class TestCLI:
    def test_demo_exits_zero(self):
        result = run_cli("demo")
        assert result.returncode == 0
        assert "Checkpoint 19.9 demo completed successfully." in result.stdout

    def test_demo_no_buysell_language(self):
        result = run_cli("demo")
        lowered = result.stdout.lower()
        assert "buy" not in lowered
        assert "sell" not in lowered

    def test_demo_banner(self):
        result = run_cli("demo")
        assert "no trade execution" in result.stdout
        assert "no broker orders" in result.stdout

    def test_start_exits_zero(self):
        result = run_cli(
            "--timeframes", "15m",
            "--reference-now", "2026-09-04T05:30:00+00:00",
            "--instruments", "RELIANCE", "TCS",
            "start",
        )
        assert result.returncode == 0
        assert "opened" in result.stdout
        assert "fsess-" in result.stdout

    def test_start_json_pure(self):
        result = run_cli(
            "--timeframes", "15m",
            "--reference-now", "2026-09-04T05:30:00+00:00",
            "--instruments", "RELIANCE",
            "--json",
            "start",
        )
        assert result.returncode == 0
        payload = json.loads(result.stdout[result.stdout.index("{"):])
        assert payload["provider"] == "fixture"
        assert payload["primary_timeframe"] == "15m"
        assert payload["universe_size"] == 1

    def test_start_json_deterministic(self):
        args = (
            "--timeframes", "15m",
            "--reference-now", "2026-09-04T05:30:00+00:00",
            "--instruments", "RELIANCE",
            "--json",
            "start",
        )
        a = run_cli(*args)
        b = run_cli(*args)
        pa = json.loads(a.stdout[a.stdout.index("{"):])
        pb = json.loads(b.stdout[b.stdout.index("{"):])
        assert pa == pb

    def test_report_empty(self):
        result = run_cli("--provider", "fixture", "report")
        assert result.returncode == 0
        assert "FORWARD-VALIDATION REPORT" in result.stdout

    def test_report_no_sessions_json(self):
        result = run_cli("--json", "report")
        assert result.returncode == 0

    def test_naive_reference_now_rejected(self):
        result = run_cli(
            "--reference-now", "2026-09-04T05:30:00",
            "start",
        )
        assert result.returncode == 2

    def test_bad_timeframe_rejected(self):
        result = run_cli("--timeframes", "1D", "start")
        assert result.returncode == 2

    def test_unknown_command(self):
        result = run_cli("bogus")
        assert result.returncode == 2

    def test_no_credentials_required(self):
        # The normal fixture path requires no credentials/network: it
        # runs and exits 0 without any token.
        result = run_cli("start")
        assert result.returncode == 0

    def test_state_dir_persists(self, tmp_path):
        state = tmp_path / "state"
        result = run_cli(
            "--timeframes", "15m",
            "--reference-now", "2026-09-04T05:30:00+00:00",
            "--instruments", "RELIANCE",
            "--state-dir", str(state),
            "start",
        )
        assert result.returncode == 0
        assert (state / "sessions").exists()