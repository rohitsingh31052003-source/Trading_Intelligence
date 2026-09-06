"""
Checkpoint 19.6 — setup-lifecycle operator CLI tests.

Prove the operator diagnostic CLI (``scripts/analyze_setup_lifecycle.py``)
behaves deterministically, offline by default, with honest lifecycle
reporting and no trading/alert/execution/persistence side effects.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "scripts" / "analyze_setup_lifecycle.py"
)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestCli:
    def test_fixture_run_exit_0(self):
        proc = _run()
        assert proc.returncode == 0, proc.stderr

    def test_fixture_run_reports(self):
        proc = _run()
        assert "SETUP LIFECYCLE" in proc.stdout
        assert "RELIANCE" in proc.stdout
        assert "no trade execution" in proc.stdout.lower()

    def test_json_pure(self):
        proc = _run("--json")
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert "lifecycles" in payload
        assert "active_count" in payload
        assert "terminal_count" in payload
        # Whole stdout IS a single JSON document (no trailing banner).
        assert json.loads(proc.stdout) is not None

    def test_json_deterministic(self):
        a = _run("--json")
        b = _run("--json")
        assert a.stdout == b.stdout

    def test_bad_args_exit_2(self):
        proc = _run("--unknown-flag")
        assert proc.returncode == 2

    def test_naive_timestamp_exit_2(self):
        proc = _run("--timestamp", "2026-09-04T05:30:00")
        assert proc.returncode == 2

    def test_aware_timestamp_exit_0(self):
        proc = _run("--timestamp", "2026-09-04T05:30:00Z")
        assert proc.returncode == 0, proc.stderr

    def test_no_buy_sell_language(self):
        proc = _run()
        upper = proc.stdout.upper()
        for kw in ("BUY ", " SELL", "ENTER ", " EXIT ", "HOLD POSITION",
                   "ORDER PLACED", "LONG POSITION", "SHORT POSITION"):
            assert kw not in upper

    def test_no_alert_language(self):
        proc = _run()
        lower = proc.stdout.lower()
        for kw in ("telegram", "whatsapp", "notification", "alert sent"):
            assert kw not in lower

    def test_disclaimer_present(self):
        proc = _run()
        assert "WARNING" in proc.stdout
        assert "NOT a trade signal" in proc.stdout

    def test_demo_creates_lifecycles(self):
        proc = _run("--json")
        payload = json.loads(proc.stdout)
        assert payload["active_count"] >= 1
        assert len(payload["lifecycles"]) >= 1
