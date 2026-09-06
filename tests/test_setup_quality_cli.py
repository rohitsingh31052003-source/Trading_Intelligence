"""
Checkpoint 19.5 — setup-quality operator CLI tests.

Prove the operator diagnostic CLI (``scripts/analyze_setup_quality.py``)
behaves deterministically, offline by default, with honest per-symbol
reporting and no trading/alert/execution side effects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "analyze_setup_quality.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCli:
    def test_fixture_run_exit_0(self):
        proc = _run("--instruments", "RELIANCE")
        assert proc.returncode == 0, proc.stderr

    def test_fixture_run_reports(self):
        proc = _run("--instruments", "RELIANCE")
        assert "SETUP-QUALITY INTELLIGENCE" in proc.stdout
        assert "RELIANCE" in proc.stdout
        assert "no trade execution" in proc.stdout.lower()

    def test_top_n_parsed(self):
        proc = _run("--instruments", "RELIANCE,TCS", "--top-n", "1")
        assert proc.returncode == 0, proc.stderr

    def test_json_pure(self):
        proc = _run("--instruments", "RELIANCE", "--json")
        assert proc.returncode == 0, proc.stderr
        # Single, balanced JSON document on stdout (no trailing banner).
        payload = json.loads(proc.stdout)
        assert payload["universe_instrument_count"] == 1
        assert payload["counts"]["tested"] == 1
        assert len(payload["results"]) == 1
        # The whole stdout IS a single JSON document (no trailing text).
        assert json.loads(proc.stdout) is not None

    def test_json_deterministic(self):
        a = _run("--instruments", "RELIANCE", "--json")
        b = _run("--instruments", "RELIANCE", "--json")
        assert a.stdout == b.stdout

    def test_bad_args_exit_2(self):
        # Empty instruments produce an explicit bad-args exit.
        proc = _run("--instruments", ",")
        assert proc.returncode == 2

    def test_bad_timeframes_exit_2(self):
        proc = _run("--timeframes", "7m")
        assert proc.returncode == 2

    def test_naive_reference_now_exit_2(self):
        proc = _run("--reference-now", "2026-09-04T05:30:00")
        assert proc.returncode == 2

    def test_default_universe_top200_label(self):
        proc = _run("--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["universe_instrument_count"] == 200

    def test_no_buy_sell_language(self):
        proc = _run("--instruments", "RELIANCE")
        upper = proc.stdout.upper()
        assert " BUY " not in upper
        assert " SELL " not in upper
        assert "broker execution" in proc.stdout.lower()

    def test_no_alerts(self):
        proc = _run("--instruments", "RELIANCE")
        lower = proc.stdout.lower()
        assert "telegram" not in lower
        assert "whatsapp" not in lower
        assert "push notification" not in lower