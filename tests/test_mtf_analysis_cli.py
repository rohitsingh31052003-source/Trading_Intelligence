"""
Checkpoint 19.4 — multi-timeframe market-state analysis CLI tests.

Deterministic, offline subprocess tests for
``scripts/analyze_mtf.py``: fixture-provider analysis, JSON output,
deterministic ordering / ids, explicit unsupported-timeframe handling,
bad-arg handling, and boundary confirmation that the CLI performs NO
broker / setup / ranking work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_CLI = _ROOT / "scripts" / "analyze_mtf.py"

_REF = "2026-09-04T05:30:00Z"  # Friday 11:00 IST (trading weekday)


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestCliBasics:
    def test_fixture_once_exit_zero(self):
        proc = _run(
            "--provider", "fixture",
            "--instruments", "RELIANCE,TCS",
            "--reference-now", _REF,
        )
        assert proc.returncode == 0, proc.stderr
        assert "MULTI-TIMEFRAME MARKET-STATE ANALYSIS ONLY" in proc.stdout
        assert "RELIANCE" in proc.stdout

    def test_nifty200_default_universe_label(self):
        proc = _run("--provider", "fixture", "--reference-now", _REF)
        assert proc.returncode == 0, proc.stderr
        assert "NIFTY Top 200" in proc.stdout
        assert "200" in proc.stdout  # full manifest count

    def test_json_output(self):
        proc = _run(
            "--provider", "fixture",
            "--instruments", "RELIANCE",
            "--reference-now", _REF,
            "--json",
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["analysis_id"].startswith("mtf-")
        assert payload["timeframes"] == ["15m", "1h"]
        assert len(payload["results"]) == 1
        assert payload["results"][0]["instrument"] == "RELIANCE"
        assert len(payload["results"][0]["timeframe_states"]) == 2

    def test_json_no_trailing_banner(self):
        proc = _run(
            "--provider", "fixture",
            "--instruments", "RELIANCE",
            "--reference-now", _REF,
            "--json",
        )
        payload = json.loads(proc.stdout)
        assert "results" in payload  # pure JSON, no trailing text

    def test_unsupported_timeframe_explicit(self):
        proc = _run(
            "--provider", "fixture",
            "--instruments", "RELIANCE",
            "--reference-now", _REF,
            "--json",
        )
        payload = json.loads(proc.stdout)
        states = payload["results"][0]["timeframe_states"]
        by_tf = {s["timeframe"]: s for s in states}
        assert by_tf["15m"]["availability"] in ("STALE", "VALID", "VALID_WITH_GAPS")
        assert by_tf["1h"]["availability"] == "UNSUPPORTED_TIMEFRAME"

    def test_deterministic_json(self):
        a = _run("--provider", "fixture", "--instruments", "RELIANCE",
                 "--reference-now", _REF, "--json").stdout
        b = _run("--provider", "fixture", "--instruments", "RELIANCE",
                 "--reference-now", _REF, "--json").stdout
        assert a == b

    def test_bad_timeframe_exit_2(self):
        proc = _run(
            "--provider", "fixture",
            "--timeframes", "15m,spam",
            "--instruments", "RELIANCE",
        )
        assert proc.returncode == 2

    def test_duplicate_timeframe_exit_2(self):
        proc = _run(
            "--provider", "fixture",
            "--timeframes", "15m,15m",
            "--instruments", "RELIANCE",
        )
        assert proc.returncode == 2

    def test_unknown_provider_exit_2(self):
        proc = _run("--provider", "nobody")
        assert proc.returncode == 2

    def test_naive_reference_now_exit_2(self):
        proc = _run("--provider", "fixture", "--reference-now", "2026-09-04")
        assert proc.returncode == 2

    def test_manual_timeframe_set_accepted(self):
        proc = _run(
            "--provider", "fixture",
            "--timeframes", "5m,15m",
            "--instruments", "RELIANCE",
            "--reference-now", _REF,
            "--json",
        )
        assert proc.returncode == 0, proc.stderr
        payload = json.loads(proc.stdout)
        assert payload["timeframes"] == ["5m", "15m"]

    def test_full_manual_run_overrides_fixture(self):
        proc = _run(
            "--provider", "fixture",
            "--timeframes", "15m,1h",
            "--instruments", "RELIANCE,TCS,NIFTY",
            "--reference-now", _REF,
        )
        assert proc.returncode == 0
        for instrument in ("RELIANCE", "TCS", "NIFTY"):
            assert instrument in proc.stdout

    def test_no_buysell_no_setup_language(self):
        proc = _run("--provider", "fixture", "--instruments", "RELIANCE",
                    "--reference-now", _REF)
        lowered = proc.stdout.lower()
        for phrase in ("entry price", "stop loss", "target price",
                       "trade plan", "alert", "setup score"):
            assert phrase not in lowered
        # the ONLY "broker" mention is the explicit disclaimer line
        assert "no broker execution" in lowered