"""
Checkpoint 19.7 — user alerts CLI tests.

Covers the deterministic offline ``scripts/emit_alerts.py`` operator
interface (no network, no credentials, no external notification
service, no broker execution).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "scripts" / "emit_alerts.py"
PYTHON = sys.executable


def run_cli(*args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        [PYTHON, str(CLI), *args],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=120,
    )


class TestCliBehavior:
    def test_fixture_run_exit_0(self):
        result = run_cli()
        assert result.returncode == 0, result.stderr
        assert "USER ALERTS" in result.stdout

    def test_json_pure_output(self):
        result = run_cli("--json")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert isinstance(payload, dict)
        assert "cycles" in payload
        assert payload["cycles"][0]["scan_cycle_id"] == "cycle-1"
        # banner/warning text must not pollute pure JSON stdout
        assert "WARNING" not in result.stdout

    def test_json_deterministic(self):
        a = run_cli("--json")
        b = run_cli("--json")
        assert a.stdout == b.stdout
        assert a.stdout == b.stdout

    def test_no_channel_records_skipped(self):
        result = run_cli("--json", "--no-channel")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        deliveries = {
            d["status"]
            for cycle in payload["cycles"]
            for d in cycle["deliveries"]
        }
        assert deliveries == {"SKIPPED"}

    def test_allow_detected_enables_detection_alerts(self):
        result = run_cli("--json", "--allow-detected")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        kinds = {
            a["kind"]
            for cycle in payload["cycles"]
            for a in cycle["alerts"]
        }
        assert "SETUP_DETECTED" not in kinds  # no stale detection in demo seq
        assert "SETUP_CONFIRMED" in kinds

    def test_no_detection_alerts_by_default(self):
        result = run_cli("--json")
        payload = json.loads(result.stdout)
        kinds = {
            a["kind"]
            for cycle in payload["cycles"]
            for a in cycle["alerts"]
        }
        assert "SETUP_DETECTED" not in kinds

    def test_bad_args_exit_2(self):
        result = run_cli("--unknown-flag")
        assert result.returncode == 2

    def test_naive_timestamp_rejected(self):
        result = run_cli("--timestamp", "2026-09-04T05:30:00")
        assert result.returncode == 2
        assert "timezone-aware" in result.stderr

    def test_aware_timestamp_accepted(self):
        result = run_cli("--json", "--timestamp", "2026-09-04T05:30:00+00:00")
        assert result.returncode == 0
        payload = json.loads(result.stdout)
        assert (
            payload["cycles"][0]["alerts"][0]["observation_timestamp"]
            == "2026-09-04T05:30:00+00:00"
        )

    def test_output_has_no_buy_sell_language(self):
        result = run_cli()
        lower = result.stdout.lower()
        for kw in (
            "buy now", "sell now", "enter immediately", "guaranteed",
            "profit opportunity",
        ):
            assert kw not in lower

    def test_repeated_calls_deterministic(self):
        a = run_cli("--json")
        b = run_cli("--json")
        assert a.stdout == b.stdout