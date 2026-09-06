"""
Execution authorization persistence — exceptions (Checkpoint 15.5).

Typed exception hierarchy for the authorization persistence layer.
No error is silently swallowed.
"""

from __future__ import annotations


class AuthorizationStoreError(Exception):
    """Base error for authorization persistence."""


class AuthorizationNotFoundError(AuthorizationStoreError):
    """A requested authorization was not found in the store."""


class AuthorizationIntegrityError(AuthorizationStoreError):
    """A persisted authorization failed an integrity check."""


class UnsupportedAuthorizationSchemaVersionError(AuthorizationStoreError):
    """A persisted authorization uses an unsupported schema version."""


# ============================================================
# EXECUTION COMMAND PERSISTENCE EXCEPTIONS (Checkpoint 16.5)
# ============================================================


class CommandStoreError(Exception):
    """Base error for execution command persistence."""


class CommandNotFoundError(CommandStoreError):
    """A requested command was not found in the store."""


class CommandIntegrityError(CommandStoreError):
    """A persisted command failed an integrity check."""


class UnsupportedCommandSchemaVersionError(CommandStoreError):
    """A persisted command uses an unsupported schema version."""


# ============================================================
# SUBMISSION LIFECYCLE PERSISTENCE EXCEPTIONS (Checkpoint 17.2)
# ============================================================


class SubmissionStoreError(Exception):
    """Base error for submission lifecycle persistence."""


class SubmissionNotFoundError(SubmissionStoreError):
    """A requested submission lifecycle was not found in the store."""


class SubmissionIntegrityError(SubmissionStoreError):
    """A persisted submission lifecycle failed an integrity check."""


class UnsupportedSubmissionSchemaVersionError(SubmissionStoreError):
    """A persisted submission lifecycle uses an unsupported schema version."""


class OperationalStoreError(Exception):
    """Base error for the operational-state store (Checkpoint 19.8)."""


class OperationalStateNotFoundError(OperationalStoreError):
    """No persisted operational-state snapshot exists."""


class OperationalStateIntegrityError(OperationalStoreError):
    """A persisted operational-state snapshot is internally inconsistent
    or conflicts with an existing snapshot on a non-overwrite save."""


class UnsupportedOperationalStateSchemaVersionError(OperationalStoreError):
    """A persisted operational-state snapshot uses an unsupported schema
    version."""


__all__ = [
    "AuthorizationIntegrityError",
    "AuthorizationNotFoundError",
    "AuthorizationStoreError",
    "CommandIntegrityError",
    "CommandNotFoundError",
    "CommandStoreError",
    "OperationalStateIntegrityError",
    "OperationalStateNotFoundError",
    "OperationalStoreError",
    "SubmissionIntegrityError",
    "SubmissionNotFoundError",
    "SubmissionStoreError",
    "UnsupportedAuthorizationSchemaVersionError",
    "UnsupportedCommandSchemaVersionError",
    "UnsupportedOperationalStateSchemaVersionError",
    "UnsupportedSubmissionSchemaVersionError",
]
