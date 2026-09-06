"""
Operational-state persistence — serialization (Checkpoint 19.8).

Deterministic, self-describing JSON serialization for the durable
OPERATIONAL state that supports reliable recovery:

    * :class:`~engine.models.operational_health.ProviderHealth`,
      :class:`~engine.models.operational_health.CycleHealth`,
      :class:`~engine.models.operational_health.DeliveryHealth`,
      :class:`~engine.models.operational_health.OperationalEventRecord`,
      :class:`~engine.models.operational_health.RetryRecord`,
      :class:`~engine.models.operational_health.PerSymbolFailure`,
      :class:`~engine.models.operational_health.RecoveryEvent`,
      :class:`~engine.models.operational_health.OperationalStateBundle`.

Design rules (match the Checkpoint 15.5 persistence conventions):

* Schema-versioned (``OPERATIONAL_STATE_SCHEMA_VERSION = 1``); the
  version is validated BEFORE any model reconstruction (a future schema
  is rejected, never guessed at).
* Deterministic sorted-key bytes; stable value encoding; no ``repr()``
  / memory addresses.
* Lossless for every operational field; datetimes ISO; enum names;
  tuples preserved as tuples.
* No ``pickle`` / ``eval`` / ``exec``.

ANALYTICAL vs OPERATIONAL STATE (mandatory separation): the persisted
operational bundle carries ONLY operational state (health counters,
heartbeat, cycle ids, setup ids, alert ids, outbox depth, lifecycle ids
for identity continuity). It NEVER embeds analytical state (no scores,
no lifecycle observations, no alert payloads) and NEVER fabricates
analytical state from missing/corrupt operational data — a corrupted
operational snapshot is reported and fails closed.
"""

from __future__ import annotations

import json
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from engine.config.reliability_config import (
    OPERATIONAL_STATE_SCHEMA_VERSION,
)
from engine.models.operational_health import (
    CycleHealth,
    DeliveryHealth,
    HeartbeatState,
    OperationalEventRecord,
    OperationalHealthState,
    OperationalStateBundle,
    PerSymbolFailure,
    ProviderHealth,
    RecoveryEvent,
    ReliabilityFailureCategory,
    RetryRecord,
)


def serialize_operational_state(bundle: OperationalStateBundle) -> str:
    """Serialize an :class:`OperationalStateBundle` to canonical JSON."""

    payload = {
        "schema_version": OPERATIONAL_STATE_SCHEMA_VERSION,
        "bundle": _to_json(bundle),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def serialize_operational_state_bytes(bundle: OperationalStateBundle) -> bytes:
    """Serialize to canonical JSON bytes."""
    return serialize_operational_state(bundle).encode("utf-8")


def deserialize_operational_state(payload: str) -> OperationalStateBundle:
    """
    Reconstruct an :class:`OperationalStateBundle` from JSON text.

    Raises ``ValueError`` for an unsupported schema version, malformed
    JSON, or a missing ``bundle`` key.
    """

    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed operational-state JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            "Malformed operational-state payload: expected a JSON object."
        )
    version = parsed.get("schema_version")
    if version != OPERATIONAL_STATE_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported operational-state schema version {version!r}; "
            f"supported is {OPERATIONAL_STATE_SCHEMA_VERSION}."
        )
    if "bundle" not in parsed:
        raise ValueError(
            "Malformed operational-state payload: missing 'bundle' key."
        )
    return _from_json(parsed.get("bundle"), OperationalStateBundle)


def parse_operational_state_header(payload: str) -> dict[str, Any]:
    """Cheap header-only parse of a persisted operational-state document."""
    return json.loads(payload)


def canonical_operational_state_json(bundle: OperationalStateBundle) -> str:
    """Canonical (sorted-key) JSON text for a bundle."""
    return serialize_operational_state(bundle)


def _to_json(value: Any) -> Any:
    """Recursively encode a model value to JSON-safe form."""

    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, Enum):
        return {"__enum__": value.name}
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "__dataclass__": type(value).__name__,
            "fields": {
                f.name: _to_json(getattr(value, f.name))
                for f in value.__dataclass_fields__.values()  # type: ignore[attr-defined]
            },
        }
    if isinstance(value, (list, tuple)):
        return {"__tuple__": [_to_json(item) for item in value]}
    if isinstance(value, dict):
        return {str(k): _to_json(v) for k, v in value.items()}
    if isinstance(value, (str, int, float)):
        return value
    return str(value)


# Mapping of tagged dataclass name -> model class.
_DATACLASSES: dict[str, type] = {
    "CycleHealth": CycleHealth,
    "DeliveryHealth": DeliveryHealth,
    "OperationalEventRecord": OperationalEventRecord,
    "OperationalStateBundle": OperationalStateBundle,
    "PerSymbolFailure": PerSymbolFailure,
    "ProviderHealth": ProviderHealth,
    "RecoveryEvent": RecoveryEvent,
    "RetryRecord": RetryRecord,
}

# Mapping of enum name -> enum class.
_ENUMS: dict[str, type] = {
    "HeartbeatState": HeartbeatState,
    "OperationalHealthState": OperationalHealthState,
    "ReliabilityFailureCategory": ReliabilityFailureCategory,
}


def _from_json(value: Any, expected_type: type) -> Any:
    """Reconstruct a value of ``expected_type`` from its JSON-safe form."""

    if value is None:
        return None
    if expected_type is datetime:
        if isinstance(value, dict) and "__datetime__" in value:
            return datetime.fromisoformat(value["__datetime__"])
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value
    if expected_type in _ENUMS.values():
        if isinstance(value, dict) and "__enum__" in value:
            return expected_type[value["__enum__"]]
        if isinstance(value, str):
            return expected_type[value]
        return value
    if expected_type in _DATACLASSES.values():
        cls = expected_type
        fields_payload = (
            value.get("fields", {}) if isinstance(value, dict) else {}
        )
        kwargs: dict[str, Any] = {}
        for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
            kwargs[f.name] = _decode_field(fields_payload.get(f.name), f.type)
        return cls(**kwargs)
    if expected_type in (tuple, list, str, int, float, bool):
        if expected_type in (tuple, list):
            if isinstance(value, dict) and "__tuple__" in value:
                return tuple(value["__tuple__"])
            return tuple(value) if isinstance(value, list) else value
        return value
    return value


def _decode_field(raw: Any, ftype: Any = None) -> Any:
    """Decode a single dataclass field from its JSON-safe form."""

    if raw is None:
        return None
    if isinstance(raw, dict) and "__datetime__" in raw:
        return datetime.fromisoformat(raw["__datetime__"])
    if isinstance(raw, dict) and "__enum__" in raw:
        name = raw["__enum__"]
        for enum_cls in _ENUMS.values():
            try:
                return enum_cls[name]
            except KeyError:
                continue
        return name
    if isinstance(raw, dict) and "__dataclass__" in raw:
        cls_name = raw["__dataclass__"]
        cls = _DATACLASSES.get(cls_name)
        if cls is None:
            return None
        fields_payload = raw.get("fields", {})
        kwargs: dict[str, Any] = {}
        for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
            kwargs[f.name] = _decode_field(fields_payload.get(f.name), f.type)
        return cls(**kwargs)
    if isinstance(raw, dict) and "__tuple__" in raw:
        return tuple(_decode_field(item) for item in raw["__tuple__"])
    if isinstance(raw, list):
        return tuple(_decode_field(item) for item in raw)
    if isinstance(raw, dict):
        return {k: _decode_field(v) for k, v in raw.items()}
    return raw


__all__ = [
    "canonical_operational_state_json",
    "deserialize_operational_state",
    "parse_operational_state_header",
    "serialize_operational_state",
    "serialize_operational_state_bytes",
]