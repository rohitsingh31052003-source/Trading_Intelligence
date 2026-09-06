"""
Forward-validation persistence — store (Checkpoint 19.9).

A THIN atomic filesystem store for the durable forward-validation
documents:

* one JSON file per forward observation (``<dir>/<observation_id>.json``);
* one JSON file per forward outcome (``<dir>/<outcome_id>.outcome``);
* one JSON manifest per forward session (``<dir>/<session_id>.session``).

The store persists ONLY the immutable forward-validation records
(observations / outcomes / sessions). It NEVER persists the upstream
analytical state (no scores beyond the recorded snapshot, no lifecycle
history, no alert payloads, no market data) and NEVER fabricates records
from missing/corrupt data.

Design rules (match the Checkpoint 19.8 persistence conventions):

* Atomic writes: same-directory temp file + flush + fsync (best-effort)
  + ``os.replace`` — a partially written document is never left in place.
* Safe-id validation: no path traversal (document ids must match the
  safe charset).
* Schema version checked BEFORE any model reconstruction; future
  versions / corrupted JSON / identity mismatches raise typed
  exceptions (never silently accepted).
* Idempotent writes: saving the identical document twice is a no-op;
  saving a DIFFERENT document under the same id must pass
  ``overwrite=True`` (``ForwardStoreIntegrityError`` otherwise).
* Default directory ``./data/forward_validation`` (git-ignored via
  ``data/``), resolved relative to the current working directory (no
  hard-coded absolute paths).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from engine.config.forward_validation_config import (
    FORWARD_OBSERVATION_SCHEMA_VERSION,
    FORWARD_SESSION_SCHEMA_VERSION,
)
from engine.models.forward_validation import (
    ForwardObservation,
    ForwardOutcome,
    ForwardSession,
)
from engine.persistence.exceptions import (
    ForwardObservationNotFoundError,
    ForwardSessionNotFoundError,
    ForwardStoreError,
    ForwardStoreIntegrityError,
    UnsupportedForwardSchemaVersionError,
)
from engine.persistence.forward_validation_serialization import (
    deserialize_observation,
    deserialize_outcome,
    deserialize_session,
    serialize_observation,
    serialize_outcome,
    serialize_session,
)

#: Safe filename charset (opaque document ids only).
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Sub-directories inside the store root (document-kind separation keeps
#: the observation / outcome / session listings distinct).
_OBSERVATION_DIR = "observations"
_OUTCOME_DIR = "outcomes"
_SESSION_DIR = "sessions"

#: Suffixes used to disambiguate outcome vs session documents from the
#: observation JSON documents in a shared directory (mirrors the 19.8
#: ``.suite`` / ``.selection`` convention).
_OUTCOME_SUFFIX = ".outcome"
_SESSION_SUFFIX = ".session"


def _validate_id(document_id: str) -> None:
    if not document_id or not _SAFE_ID_RE.match(document_id):
        raise ForwardStoreError(
            f"Unsafe document id {document_id!r}: ids must match "
            f"{_SAFE_ID_RE.pattern!r}.",
        )


def default_forward_validation_directory() -> Path:
    """Default directory (``./data/forward_validation``), cwd-relative."""
    return Path.cwd() / "data" / "forward_validation"


def _read_document(path: Path, expected_version: int, kind: str) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        header = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ForwardStoreIntegrityError(
            f"Corrupt {kind} document {path!s}: {exc}",
        ) from exc
    if not isinstance(header, dict):
        raise ForwardStoreIntegrityError(
            f"Corrupt {kind} document {path!s}: expected a JSON object.",
        )
    version = header.get("schema_version")
    if version != expected_version:
        raise UnsupportedForwardSchemaVersionError(
            f"Unsupported {kind} schema version {version!r}; supported is "
            f"{expected_version}.",
        )
    return text


class ForwardObservationStore:
    """
    Atomic, filesystem-based persistence for the Checkpoint 19.9
    forward-validation documents.

    Public API:

        save_observation(observation, overwrite=False) -> Path
        load_observation(observation_id) -> ForwardObservation
        save_outcome(outcome, overwrite=False) -> Path
        load_outcome(outcome_id) -> ForwardOutcome
        save_session(session, overwrite=False) -> Path
        load_session(session_id) -> ForwardSession
        exists / list_observations / list_outcomes / list_sessions
        delete_observation / delete_outcome / delete_session
        observation_path / outcome_path / session_path
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self._root = (
            Path(directory)
            if directory is not None
            else default_forward_validation_directory()
        )
        self._observations = self._root / _OBSERVATION_DIR
        self._outcomes = self._root / _OUTCOME_DIR
        self._sessions = self._root / _SESSION_DIR

    @property
    def directory(self) -> Path:
        return self._root

    # ------------------------------------------------------------
    # OBSERVATIONS
    # ------------------------------------------------------------

    def observation_path(self, observation_id: str) -> Path:
        _validate_id(observation_id)
        return self._observations / f"{observation_id}.json"

    def save_observation(
        self,
        observation: ForwardObservation,
        overwrite: bool = False,
    ) -> Path:
        if not isinstance(observation, ForwardObservation):
            raise TypeError("observation must be a ForwardObservation.")
        _validate_id(observation.observation_id)
        target = self.observation_path(observation.observation_id)
        return self._atomic_write(
            target,
            serialize_observation(observation),
            overwrite=overwrite,
        )

    def load_observation(self, observation_id: str) -> ForwardObservation:
        path = self.observation_path(observation_id)
        if not path.exists():
            raise ForwardObservationNotFoundError(
                f"Forward observation {observation_id!r} not found.",
            )
        text = _read_document(
            path, FORWARD_OBSERVATION_SCHEMA_VERSION, "observation",
        )
        try:
            return deserialize_observation(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ForwardStoreIntegrityError(
                f"Could not reconstruct observation {path!s}: {exc}",
            ) from exc

    def observation_exists(self, observation_id: str) -> bool:
        return self.observation_path(observation_id).exists()

    def list_observations(self) -> tuple[str, ...]:
        if not self._observations.exists():
            return ()
        return tuple(
            sorted(
                p.stem for p in self._observations.glob("*.json")
            ),
        )

    def delete_observation(self, observation_id: str) -> bool:
        path = self.observation_path(observation_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    # ------------------------------------------------------------
    # OUTCOMES
    # ------------------------------------------------------------

    def outcome_path(self, outcome_id: str) -> Path:
        _validate_id(outcome_id)
        return self._outcomes / f"{outcome_id}{_OUTCOME_SUFFIX}"

    def save_outcome(
        self,
        outcome: ForwardOutcome,
        overwrite: bool = False,
    ) -> Path:
        if not isinstance(outcome, ForwardOutcome):
            raise TypeError("outcome must be a ForwardOutcome.")
        _validate_id(outcome.outcome_id)
        target = self.outcome_path(outcome.outcome_id)
        return self._atomic_write(
            target,
            serialize_outcome(outcome),
            overwrite=overwrite,
        )

    def load_outcome(self, outcome_id: str) -> ForwardOutcome:
        path = self.outcome_path(outcome_id)
        if not path.exists():
            raise ForwardStoreIntegrityError(
                f"Forward outcome {outcome_id!r} not found.",
            )
        text = _read_document(
            path, FORWARD_OBSERVATION_SCHEMA_VERSION, "outcome",
        )
        try:
            return deserialize_outcome(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ForwardStoreIntegrityError(
                f"Could not reconstruct outcome {path!s}: {exc}",
            ) from exc

    def outcome_exists(self, outcome_id: str) -> bool:
        return self.outcome_path(outcome_id).exists()

    def list_outcomes(self) -> tuple[str, ...]:
        if not self._outcomes.exists():
            return ()
        return tuple(
            sorted(
                p.name[: -len(_OUTCOME_SUFFIX)]
                for p in self._outcomes.glob(f"*{_OUTCOME_SUFFIX}")
            ),
        )

    def delete_outcome(self, outcome_id: str) -> bool:
        path = self.outcome_path(outcome_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    # ------------------------------------------------------------
    # SESSIONS
    # ------------------------------------------------------------

    def session_path(self, session_id: str) -> Path:
        _validate_id(session_id)
        return self._sessions / f"{session_id}{_SESSION_SUFFIX}"

    def save_session(
        self,
        session: ForwardSession,
        overwrite: bool = False,
    ) -> Path:
        if not isinstance(session, ForwardSession):
            raise TypeError("session must be a ForwardSession.")
        _validate_id(session.session_id)
        target = self.session_path(session.session_id)
        return self._atomic_write(
            target,
            serialize_session(session),
            overwrite=overwrite,
        )

    def load_session(self, session_id: str) -> ForwardSession:
        path = self.session_path(session_id)
        if not path.exists():
            raise ForwardSessionNotFoundError(
                f"Forward session {session_id!r} not found.",
            )
        text = _read_document(
            path, FORWARD_SESSION_SCHEMA_VERSION, "session",
        )
        try:
            return deserialize_session(text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ForwardStoreIntegrityError(
                f"Could not reconstruct session {path!s}: {exc}",
            ) from exc

    def session_exists(self, session_id: str) -> bool:
        return self.session_path(session_id).exists()

    def list_sessions(self) -> tuple[str, ...]:
        if not self._sessions.exists():
            return ()
        return tuple(
            sorted(
                p.name[: -len(_SESSION_SUFFIX)]
                for p in self._sessions.glob(f"*{_SESSION_SUFFIX}")
            ),
        )

    def delete_session(self, session_id: str) -> bool:
        path = self.session_path(session_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    # ------------------------------------------------------------
    # ATOMIC WRITE (shared)
    # ------------------------------------------------------------

    def _atomic_write(
        self,
        target: Path,
        text: str,
        *,
        overwrite: bool,
    ) -> Path:
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        if target.exists() and not overwrite:
            existing = target.read_text(encoding="utf-8")
            if existing != text:
                raise ForwardStoreIntegrityError(
                    "Forward-validation document already exists with "
                    "different content. Pass overwrite=True to replace it "
                    "explicitly."
                )
            return target
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
        except ForwardStoreError:
            raise
        except Exception as exc:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise ForwardStoreError(
                f"Failed to atomically write {target!s}: {exc}",
            ) from exc
        return target


__all__ = [
    "ForwardObservationStore",
    "default_forward_validation_directory",
]