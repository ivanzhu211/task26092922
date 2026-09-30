"""Exception hierarchy for the restore library."""

from __future__ import annotations


class RestoreError(Exception):
    """Base class for all restore-library errors."""


class VerificationError(RestoreError):
    """A source file failed length or SHA-256 verification."""

    def __init__(self, message: str, *, target: str | None = None) -> None:
        super().__init__(message)
        self.target = target


class ConflictError(RestoreError):
    """A request_id was reused with conflicting entries, or the request
    is in an incompatible state."""


class RequestNotFoundError(RestoreError):
    """inspect() was called for an unknown request_id."""


class ProtocolError(RestoreError):
    """Persisted state is inconsistent (internal invariant violation)."""
