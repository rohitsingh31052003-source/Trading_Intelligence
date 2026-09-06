"""
Forward-validation persistence — serialization (Checkpoint 19.9).

Deterministic, self-describing JSON serialization for the durable
forward-validation documents:

    * :class:`~engine.models.forward_validation.ForwardObservation`
      (the immutable point-in-time observation record);
    * :class:`~engine.models.forward_validation.ForwardOutcome` (the
      forward-only outcome measurement);
    * :class:`~engine.models.forward_validation.ForwardSession` (the
      auditable forward-validation session).

Design rules (match the Checkpoint 15.5 / 19.8 persistence
conventions):

* Schema-versioned (``FORWARD_OBSERVATION_SCHEMA_VERSION`` /
  ``FORWARD_SESSION_SCHEMA_VERSION``); the version is validated BEFORE
  any model reconstruction (a future schema is rejected, never guessed
  at).
* Deterministic sorted-key bytes; stable value encoding; no ``repr()``
  / memory addresses.
* Lossless for every field; datetimes ISO; enum names carried BOTH as
  ``__enum__`` (member name) AND ``__enum_class__`` (class name) so the
  deserializer resolves the EXACT enum class unambiguously (the 19.9
  enums share member names such as ``DATA_UNAVAILABLE`` /
  ``NOT_EVALUABLE`` with other model layers).
* Tuples preserved as tuples; nested models preserved by value.
* No ``pickle`` / ``eval`` / ``exec``.

The observation serialization is the LEAKAGE-PROOF anchor: tests
serialize an observation at ``T``, add future candles, compute the
outcome, and verify the original observation's serialized bytes remain
byte-identical.
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from engine.config.forward_validation_config import (
    FORWARD_OBSERVATION_SCHEMA_VERSION,
    FORWARD_SESSION_SCHEMA_VERSION,
)
from engine.models.forward_validation import (
    ForwardObservation,
    ForwardOutcome,
    ForwardSession,
    ForwardSessionCounts,
    ForwardObservationStatus,
    LifecycleResolution,
    OutcomeAvailability,
    OutcomeDirection,
)
from engine.models.setup_lifecycle import LifecycleObservationStatus
from engine.models.setup_quality import (
    SetupQualityClassification,
    SetupQualityStatus,
)


# ==================================================================
# ENUM RESOLUTION (class-qualified so shared member names resolve)
# ==================================================================

_ENUMS: dict[str, type[Enum]] = {
    "engine.models.forward_validation.ForwardObservationStatus": ForwardObservationStatus,
    "engine.models.forward_validation.LifecycleResolution": LifecycleResolution,
    "engine.models.forward_validation.OutcomeAvailability": OutcomeAvailability,
    "engine.models.forward_validation.OutcomeDirection": OutcomeDirection,
    # Reused enum vocabularies carried by the observations (FROZEN
    # 19.5 / 19.6 enums — resolved via the SAME class-qualified tags so
    # the round trip is lossless even though member names are shared).
    "engine.models.setup_lifecycle.LifecycleObservationStatus": (
        LifecycleObservationStatus
    ),
    "engine.models.setup_quality.SetupQualityStatus": SetupQualityStatus,
    "engine.models.setup_quality.SetupQualityClassification": (
        SetupQualityClassification
    ),
}


def _type_tag(cls: type) -> str:
    return f"{cls.__module__}.{cls.__name__}"


def _enum_to_json(value: Enum) -> dict[str, str]:
    return {
        "__enum__": value.name,
        "__enum_class__": f"{type(value).__module__}.{type(value).__name__}",
    }


def _enum_from_json(payload: dict[str, Any], expected: type[Enum]) -> Enum:
    name = payload.get("__enum__")
    class_ref = payload.get("__enum_class__")
    if not isinstance(name, str) or not isinstance(class_ref, str):
        raise ValueError("malformed enum tag.")
    cls = _ENUMS.get(class_ref)
    if cls is not None:
        pass
    elif isinstance(expected, type) and issubclass(expected, Enum):
        # Accept the expected class directly (forward-compatible within
        # the same enum vocabulary).
        cls = expected
    else:
        raise ValueError(f"unknown enum class {class_ref!r}.")
    try:
        return cls(name)
    except ValueError as exc:
        raise ValueError(
            f"unknown enum member {name!r} for {class_ref!r}.",
        ) from exc


# ==================================================================
# ENCODING
# ==================================================================

_NESTED_MODELS = {
    _type_tag(ForwardObservation): {
        "_type": "forward_observation",
    },
    _type_tag(ForwardOutcome): {
        "_type": "forward_outcome",
    },
    _type_tag(ForwardSession): {
        "_type": "forward_session",
    },
    _type_tag(ForwardSessionCounts): {
        "_type": "forward_session_counts",
    },
}


def _to_json(value: Any) -> Any:
    """Canonical JSON encoding (deterministic sorted keys)."""

    if isinstance(value, Enum):
        return _enum_to_json(value)
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if is_dataclass(value) and not isinstance(value, type):
        import dataclasses

        tag = _type_tag(type(value))
        nested = _NESTED_MODELS.get(tag)
        if nested is None:
            # Unknown dataclass: encode fields defensively (should not
            # occur for the 19.9 documents).
            fields = {
                f.name: _to_json(getattr(value, f.name))
                for f in dataclasses.fields(value)
            }
            fields["__class__"] = tag
            return fields
        fields = {
            f.name: _to_json(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
        fields["__model__"] = nested["_type"]
        return fields
    if isinstance(value, tuple):
        return {"__tuple__": [_to_json(v) for v in value]}
    if isinstance(value, list):
        return [_to_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in sorted(value.items())}
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float, str)):
        return value
    raise TypeError(
        f"cannot serialize {type(value).__name__} in a forward-validation "
        "document.",
    )


def _from_json(value: Any, expected: type) -> Any:
    """Canonical JSON decoding (mirror of :func:`_to_json`)."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_from_json(v, Any) for v in value]
    if not isinstance(value, dict):
        raise ValueError("malformed document node.")
    if "__enum__" in value:
        return _enum_from_json(value, expected)
    if "__datetime__" in value:
        try:
            return datetime.fromisoformat(value["__datetime__"])
        except ValueError as exc:
            raise ValueError(f"malformed datetime: {exc}") from exc
    if "__tuple__" in value:
        return tuple(_from_json(v, Any) for v in value["__tuple__"])
    model = value.get("__model__")
    if model is not None:
        for tag, meta in _NESTED_MODELS.items():
            if meta["_type"] == model:
                return _reconstruct(tag, value)
        raise ValueError(f"unknown nested model {model!r}.")
    # A plain mapping: decode values recursively.
    return {str(k): _from_json(v, Any) for k, v in value.items()}


def _reconstruct(tag: str, payload: dict[str, Any]):
    """Reconstruct a nested model by class-qualified name (lazy
    import)."""

    module_name, class_name = tag.rsplit(".", 1)
    import importlib

    module = importlib.import_module(module_name)
    cls = getattr(module, class_name)
    fields = {}
    for name, raw in payload.items():
        if name == "__model__":
            continue
        fields[name] = _from_json(raw, Any)
    # ForwardObservation / ForwardOutcome / ForwardSession / Counts all
    # accept keyword construction; __post_init__ re-validates.
    return cls(**fields)


# ==================================================================
# PUBLIC API
# ==================================================================


def serialize_observation(observation: ForwardObservation) -> str:
    """Serialize a forward observation to canonical JSON."""

    payload = {
        "schema_version": FORWARD_OBSERVATION_SCHEMA_VERSION,
        "document": "forward_observation",
        "observation": _to_json(observation),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def deserialize_observation(payload: str) -> ForwardObservation:
    """
    Reconstruct a :class:`ForwardObservation` from JSON text.

    Raises ``ValueError`` for an unsupported schema version, malformed
    JSON, a missing/incompatible document key, or an unknown enum
    member.
    """

    parsed = _parse_wrapped(payload, "forward_observation")
    observation = _from_json(parsed.pop("observation"), ForwardObservation)
    if not isinstance(observation, ForwardObservation):
        raise ValueError("payload did not reconstruct a ForwardObservation.")
    return observation


def serialize_outcome(outcome: ForwardOutcome) -> str:
    """Serialize a forward outcome to canonical JSON."""

    payload = {
        "schema_version": FORWARD_OBSERVATION_SCHEMA_VERSION,
        "document": "forward_outcome",
        "outcome": _to_json(outcome),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def deserialize_outcome(payload: str) -> ForwardOutcome:
    """
    Reconstruct a :class:`ForwardOutcome` from JSON text.

    Raises ``ValueError`` for an unsupported schema version, malformed
    JSON, a missing/incompatible document key, or an unknown enum
    member.
    """

    parsed = _parse_wrapped(payload, "forward_outcome")
    outcome = _from_json(parsed.pop("outcome"), ForwardOutcome)
    if not isinstance(outcome, ForwardOutcome):
        raise ValueError("payload did not reconstruct a ForwardOutcome.")
    return outcome


def serialize_session(session: ForwardSession) -> str:
    """Serialize a forward session to canonical JSON."""

    payload = {
        "schema_version": FORWARD_SESSION_SCHEMA_VERSION,
        "document": "forward_session",
        "session": _to_json(session),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def deserialize_session(payload: str) -> ForwardSession:
    """
    Reconstruct a :class:`ForwardSession` from JSON text.

    Raises ``ValueError`` for an unsupported schema version, malformed
    JSON, a missing/incompatible document key, or an unknown enum
    member.
    """

    parsed = _parse_wrapped(payload, "forward_session")
    session = _from_json(parsed.pop("session"), ForwardSession)
    if not isinstance(session, ForwardSession):
        raise ValueError("payload did not reconstruct a ForwardSession.")
    return session


def _parse_wrapped(payload: str, expected_document: str) -> dict[str, Any]:
    """Common wrapper parse + schema/document checks."""

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed forward-validation JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "Malformed forward-validation payload: expected a JSON object.",
        )
    version = parsed.get("schema_version")
    document = parsed.get("document")
    if document != expected_document:
        raise ValueError(
            f"Malformed forward-validation payload: expected document "
            f"{expected_document!r}, got {document!r}.",
        )
    schema_version = (
        FORWARD_OBSERVATION_SCHEMA_VERSION
        if "observation" in parsed or "outcome" in parsed
        else FORWARD_SESSION_SCHEMA_VERSION
    )
    if version != schema_version:
        raise ValueError(
            f"Unsupported forward-validation schema version {version!r}; "
            f"supported is {schema_version}.",
        )
    return parsed


def canonical_observation_json(observation: ForwardObservation) -> str:
    """Canonical (sorted-key) JSON text of an observation document."""
    return serialize_observation(observation)


def canonical_outcome_json(outcome: ForwardOutcome) -> str:
    """Canonical (sorted-key) JSON text of an outcome document."""
    return serialize_outcome(outcome)


def canonical_session_json(session: ForwardSession) -> str:
    """Canonical (sorted-key) JSON text of a session document."""
    return serialize_session(session)


def serialize_observation_bytes(observation: ForwardObservation) -> bytes:
    return serialize_observation(observation).encode("utf-8")


def serialize_outcome_bytes(outcome: ForwardOutcome) -> bytes:
    return serialize_outcome(outcome).encode("utf-8")


def serialize_session_bytes(session: ForwardSession) -> bytes:
    return serialize_session(session).encode("utf-8")


__all__ = [
    "canonical_observation_json",
    "canonical_outcome_json",
    "canonical_session_json",
    "deserialize_observation",
    "deserialize_outcome",
    "deserialize_session",
    "serialize_observation",
    "serialize_observation_bytes",
    "serialize_outcome",
    "serialize_outcome_bytes",
    "serialize_session",
    "serialize_session_bytes",
]