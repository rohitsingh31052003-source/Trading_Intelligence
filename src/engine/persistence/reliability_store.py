"""
Operational-state persistence — store (Checkpoint 19.8).

A SINGLE-FILE atomic filesystem store for the durable OPERATIONAL state
bundle. The store persists ONLY what reliable recovery requires:

* operational health counters (provider / cycle / delivery);
* the heartbeat instant;
* the observed SETUP identity ids and ALERT ids (identity continuity —
  a restart must not recreate setups as new setups without justification
  and must not re-deduplicate already-emitted alerts);
* the per-symbol failure log, retry records, recovery log;
* the alert-outbox PENDING count (delivery reliability).

It NEVER stores analytical state (no scores, no lifecycle observations,
no alert payloads, no market data).

Design rules (match every other store in the repository):

* Atomic writes: same-directory temp file + flush + fsync (best-effort)
  + ``os.replace`` — a partially written snapshot is never left in place.
* Safe-id validation: no path traversal.
* Schema version checked BEFORE any model reconstruction; future
  versions / corrupted JSON / identity mismatches raise typed
  exceptions (never silently accepted; analytical state is never
  fabricated from missing/corrupt operational state).
* Idempotent saves: writing the same bundle twice is a no-op; writing a
  DIFFERENT bundle must pass ``overwrite=True``.
* Default directory ``./data/operational_state`` (git-ignored via
  ``data/``), resolved relative to the current working directory (no
  hard-coded absolute paths).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from engine.config.reliability_config import (
    OPERATIONAL_STATE_SCHEMA_VERSION,
)
from engine.models.operational_health import OperationalStateBundle
from engine.persistence.reliability_serialization import (
    canonical_operational_state_json,
    deserialize_operational_state,
    parse_operational_state_header,
    serialize_operational_state,
)
from engine.persistence.exceptions import (
    OperationalStoreError,
    OperationalStateIntegrityError,
    UnsupportedOperationalStateSchemaVersionError,
)

#: Safe filename charset (opaque snapshot ids only).
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Persisted operational-state file name (single document store).
OPERATIONAL_STATE_FILENAME = "operational_state.json"

#: Lock file used by :class:`SingleInstanceGuard` when a directory is
#: provided (explicit, observable, recoverable).
INSTANCE_LOCK_FILENAME = "scanner.lock"


def _validate_id(snapshot_id: str) -> None:
    if not snapshot_id or not _SAFE_ID_RE.match(snapshot_id):
        raise OperationalStoreError(
            f"Unsafe state id {snapshot_id!r}: ids must match "
            f"{_SAFE_ID_RE.pattern!r}."
        )


def default_operational_state_directory() -> Path:
    """Default directory (``./data/operational_state``), cwd-relative."""
    return Path.cwd() / "data" / "operational_state"


class OperationalStateStore:
    """
    Atomic, filesystem-based persistence for the operational-state
    bundle (Checkpoint 19.8).

    Public API:

        save_snapshot(bundle, overwrite=False) -> Path
        load_snapshot() -> OperationalStateBundle | None
        exists() -> bool
        delete() -> None
        path() -> Path
        snapshot_exists() -> bool
        read_snapshot_text() -> str | None

    + an optional small lock-file helper (:meth:`lock_path` /
    :meth:`write_lock` / :meth:`read_lock` / :meth:`clear_lock`) for the
    single-instance guard.
    """

    def __init__(
        self,
        directory: Path | str | None = None,
    ) -> None:
        if directory is None:
            directory = default_operational_state_directory()
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def path(self) -> Path:
        return self._directory / OPERATIONAL_STATE_FILENAME

    def lock_path(self) -> Path:
        return self._directory / INSTANCE_LOCK_FILENAME

    # ------------------------------------------------------------
    # LOCK FILE (single-instance guard storage)
    # ------------------------------------------------------------

    def write_lock(self, payload: dict) -> None:
        """Atomically write a lock marker."""
        self._directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        self._atomic_write(self.lock_path(), text)

    def read_lock(self) -> dict | None:
        path = self.lock_path()
        if not path.exists():
            return None
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def clear_lock(self) -> None:
        path = self.lock_path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------
    # WRITE
    # ------------------------------------------------------------

    def save_snapshot(
        self,
        bundle: OperationalStateBundle,
        overwrite: bool = False,
    ) -> Path:
        """Persist the operational-state bundle atomically.

        Idempotent for identical content; conflicting content raises
        :class:`OperationalStateIntegrityError` unless ``overwrite``.
        """

        if not isinstance(bundle, OperationalStateBundle):
            raise TypeError("bundle must be an OperationalStateBundle.")
        _validate_id(bundle.snapshot_id)
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self.path()
        if target.exists() and not overwrite:
            existing = target.read_text(encoding="utf-8")
            new_text = serialize_operational_state(bundle)
            if existing != new_text:
                raise OperationalStateIntegrityError(
                    "Operational state already exists with different "
                    "content. Pass overwrite=True to replace it "
                    "explicitly."
                )
            return target
        self._atomic_write(target, serialize_operational_state(bundle))
        return target

    def _atomic_write(self, target: Path, text: str) -> None:
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        prefix = target.name + "."
        fd, tmp_name = tempfile.mkstemp(
            prefix=prefix, suffix=".tmp", dir=str(directory),
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                try:
                    os.fsync(handle.fileno())
                except OSError:
                    pass
            os.replace(tmp_path, target)
        except OperationalStoreError:
            raise
        except Exception as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise OperationalStoreError(
                f"Failed to atomically write operational state "
                f"{target!s}: {exc}"
            ) from exc

    # ------------------------------------------------------------
    # READ
    # ------------------------------------------------------------

    def load_snapshot(self) -> OperationalStateBundle | None:
        """Load + verify the persisted bundle (``None`` when absent).

        Raises :class:`OperationalStateIntegrityError` /
        :class:`UnsupportedOperationalStateSchemaVersionError` /
        :class:`OperationalStoreError` for corrupted / future-schema /
        malformed state — a corrupted operational snapshot is FAILED
        CLOSED, never silently invented.
        """

        path = self.path()
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        try:
            header = parse_operational_state_header(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise OperationalStoreError(
                f"Could not parse operational-state header {path!s}: {exc}"
            ) from exc
        if not isinstance(header, dict):
            raise OperationalStoreError(
                f"Malformed operational-state header in {path!s}: "
                "expected a JSON object."
            )
        version = header.get("schema_version")
        if version != OPERATIONAL_STATE_SCHEMA_VERSION:
            raise UnsupportedOperationalStateSchemaVersionError(
                f"Unsupported operational-state schema version {version!r}; "
                f"supported is {OPERATIONAL_STATE_SCHEMA_VERSION}."
            )
        try:
            bundle = deserialize_operational_state(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise OperationalStoreError(
                f"Could not reconstruct operational state from "
                f"{path!s}: {exc}"
            ) from exc
        return bundle

    def read_snapshot_text(self) -> str | None:
        path = self.path()
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def exists(self) -> bool:
        return self.path().exists()

    snapshot_exists = exists

    def delete(self) -> bool:
        path = self.path()
        if path.exists():
            try:
                path.unlink()
            except OSError:
                return False
            return True
        return False

    # ------------------------------------------------------------
    # INTEGRITY
    # ------------------------------------------------------------

    def verify_snapshot(self, bundle: OperationalStateBundle) -> bool:
        """Best-effort round-trip integrity check (deterministic)."""

        text = serialize_operational_state(bundle)
        loaded = deserialize_operational_state(text)
        return (
            loaded.snapshot_id == bundle.snapshot_id
            and canonical_operational_state_json(loaded)
            == canonical_operational_state_json(bundle)
        )


__all__ = [
    "INSTANCE_LOCK_FILENAME",
    "OPERATIONAL_STATE_FILENAME",
    "OperationalStateStore",
    "default_operational_state_directory",
]