"""
Checkpoint 19.9 — forward-validation persistence tests.

Deterministic, offline tests for the serialization + store layer:
round-trip, restart recovery, duplicate handling, corruption fail-closed,
safe-id, atomic writes, schema versioning, boundary isolation.
"""

from __future__ import annotations

import json

import pytest

from dashboard.forward_validation import ForwardSessionManager
from engine.config.forward_validation_config import ForwardValidationConfig
from engine.models.forward_validation import ForwardSession
from engine.persistence.exceptions import (
    ForwardObservationNotFoundError,
    ForwardSessionNotFoundError,
    ForwardStoreIntegrityError,
    UnsupportedForwardSchemaVersionError,
)
from engine.persistence.forward_validation_store import (
    ForwardObservationStore,
)
from engine.persistence.forward_validation_serialization import (
    FORWARD_OBSERVATION_SCHEMA_VERSION,
    canonical_observation_json,
    canonical_outcome_json,
    canonical_session_json,
    deserialize_observation,
    deserialize_outcome,
    deserialize_session,
    serialize_observation,
    serialize_outcome,
    serialize_session,
)

from tests._checkpoint19_9_fixtures import (
    FIXTURE_START,
    make_observation,
    make_outcome,
)

T = FIXTURE_START


class TestSerialization:
    def test_observation_round_trip(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        text = serialize_observation(obs)
        loaded = deserialize_observation(text)
        assert loaded == obs

    def test_observation_deterministic_bytes(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        assert serialize_observation(obs) == serialize_observation(obs)
        assert canonical_observation_json(obs) == canonical_observation_json(obs)

    def test_observation_schema_version_tagged(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        mapping = json.loads(serialize_observation(obs))
        assert mapping["schema_version"] == FORWARD_OBSERVATION_SCHEMA_VERSION
        assert mapping["document"] == "forward_observation"

    def test_outcome_round_trip(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs)
        loaded = deserialize_outcome(serialize_outcome(out))
        assert loaded == out

    def test_outcome_deterministic_bytes(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs)
        assert serialize_outcome(out) == serialize_outcome(out)
        assert canonical_outcome_json(out) == canonical_outcome_json(out)

    def test_session_round_trip(self):
        session = ForwardSession(
            session_id="fsess-x",
            provider="fixture",
            timeframes=("15m", "1h"),
            primary_timeframe="15m",
            universe=("RELIANCE",),
            universe_version="u1",
            policy_versions=(("validation_policy_version", "p1"),),
            started_at=T,
            status="OPEN",
        )
        loaded = deserialize_session(serialize_session(session))
        assert loaded == session

    def test_session_deterministic_bytes(self):
        session = ForwardSession(
            session_id="fsess-x",
            provider="fixture",
            timeframes=("15m",),
            primary_timeframe="15m",
            universe=("RELIANCE",),
            universe_version="",
            policy_versions=(),
            started_at=T,
            status="OPEN",
        )
        assert serialize_session(session) == serialize_session(session)
        assert canonical_session_json(session) == canonical_session_json(session)

    def test_parse_header(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        mapping = json.loads(serialize_observation(obs))
        assert mapping["schema_version"] == FORWARD_OBSERVATION_SCHEMA_VERSION
        assert mapping["document"] == "forward_observation"
        # Wrong document-type for the observation deserializer is
        # rejected (fail closed).
        mapping["document"] = "forward_session"
        with pytest.raises(ValueError):
            deserialize_observation(json.dumps(mapping))

    def test_future_schema_rejected(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        mapping = json.loads(serialize_observation(obs))
        mapping["schema_version"] = 999
        with pytest.raises(ValueError):
            deserialize_observation(json.dumps(mapping))

    def test_malformed_json_rejected(self):
        with pytest.raises(ValueError):
            deserialize_observation("{not-json")


class TestStore:
    def test_save_load_observation(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        assert store.observation_exists(obs.observation_id)
        assert store.load_observation(obs.observation_id) == obs

    def test_save_load_outcome(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs)
        store.save_outcome(out)
        assert store.load_outcome(out.outcome_id) == out

    def test_save_load_session(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        session = ForwardSession(
            session_id="fsess-x",
            provider="fixture",
            timeframes=("15m",),
            primary_timeframe="15m",
            universe=("RELIANCE",),
            universe_version="",
            policy_versions=(),
            started_at=T,
            status="OPEN",
        )
        store.save_session(session)
        assert store.load_session(session.session_id) == session

    def test_restart_recovery(self, tmp_path):
        # Save via store #1; load via a NEW store #2 (restart).
        store1 = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs)
        store1.save_observation(obs)
        store1.save_outcome(out)
        store2 = ForwardObservationStore(tmp_path)
        assert store2.load_observation(obs.observation_id) == obs
        assert store2.load_outcome(out.outcome_id) == out

    def test_duplicate_identical_save_idempotent(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        store.save_observation(obs)  # identical -> idempotent
        assert len(list(tmp_path.iterdir())) == 1

    def test_duplicate_conflicting_raises(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        conflicting = make_observation(
            "s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T,
            quality_score=1,
        )
        with pytest.raises(ForwardStoreIntegrityError):
            store.save_observation(conflicting)

    def test_duplicate_conflicting_overwrite(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        conflicting = make_observation(
            "s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T,
            quality_score=1,
        )
        store.save_observation(conflicting, overwrite=True)
        assert store.load_observation(obs.observation_id).quality_score == 1

    def test_load_missing_raises(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        with pytest.raises(ForwardObservationNotFoundError):
            store.load_observation("fobs-missing")
        with pytest.raises(ForwardSessionNotFoundError):
            store.load_session("fsess-missing")

    def test_safe_id_rejects_traversal(self, tmp_path):
        from engine.persistence.exceptions import ForwardStoreError

        store = ForwardObservationStore(tmp_path)
        with pytest.raises(ForwardStoreError):
            store.observation_path("../../etc/passwd")
        with pytest.raises(ForwardStoreError):
            store.observation_path("a/b")

    def test_future_schema_store_rejected(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        path = store.observation_path(obs.observation_id)
        mapping = json.loads(path.read_text(encoding="utf-8"))
        mapping["schema_version"] = 999
        path.write_text(json.dumps(mapping), encoding="utf-8")
        with pytest.raises(UnsupportedForwardSchemaVersionError):
            store.load_observation(obs.observation_id)

    def test_filenames_suffix(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        assert obs.observation_id.startswith("fobs-")
        store.save_observation(obs)
        assert store.observation_exists(obs.observation_id)
        assert obs.observation_id in store.list_observations()
        # Files live under the observations/ subdirectory.
        files = [p.name for p in (tmp_path / "observations").iterdir()]
        assert any(f.startswith("fobs-") for f in files)

    def test_atomic_write_no_temp_leftover(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        store.save_observation(obs)
        names = [p.name for p in tmp_path.iterdir()]
        assert not any("tmp" in n for n in names)

    def test_default_directory(self):
        from pathlib import Path

        from engine.persistence.forward_validation_store import (
            default_forward_validation_directory,
        )

        assert default_forward_validation_directory() == (
            Path.cwd() / "data" / "forward_validation"
        )


class TestRestartIntegration:
    def test_manager_restart_preserves_identity(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        cfg = ForwardValidationConfig()
        manager1 = ForwardSessionManager(cfg, store=store)
        session = manager1.open_session(universe=("RELIANCE",), started_at=T)
        obs = make_observation(session.session_id, scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        manager1.record_observation(session.session_id, obs)
        store.save_observation(obs)

        # Restart: a fresh manager over the same store.
        manager2 = ForwardSessionManager(cfg, store=store)
        reloaded = store.load_observation(obs.observation_id)
        assert reloaded == obs
        # Deterministic session identity survives.
        session2 = manager2.open_session(universe=("RELIANCE",), started_at=T)
        assert session2.session_id == session.session_id

    def test_outcome_and_observation_separate_files(self, tmp_path):
        store = ForwardObservationStore(tmp_path)
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        out = make_outcome(obs)
        store.save_observation(obs)
        store.save_outcome(out)
        assert store.load_observation(obs.observation_id) == obs
        assert store.load_outcome(out.outcome_id) == out


class TestImmutability:
    def test_frozen_models(self):
        obs = make_observation("s", scan_cycle_id="c", instrument="RELIANCE", reference_now=T)
        assert hasattr(obs, "__slots__")
        with pytest.raises(AttributeError):
            obs.observation_id = "x"  # type: ignore[misc]