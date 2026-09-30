"""Core data types and request state definitions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Request lifecycle states (persisted in the state database).
STAGING: Final[str] = "staging"
ROLLING_BACK: Final[str] = "rolling_back"
ROLLED_BACK: Final[str] = "rolled_back"
APPLYING: Final[str] = "applying"
SUCCEEDED: Final[str] = "succeeded"
FAILED: Final[str] = "failed"

ACTIVE_STATES: Final[frozenset[str]] = frozenset(
    {STAGING, ROLLING_BACK, APPLYING}
)
TERMINAL_STATES: Final[frozenset[str]] = frozenset(
    {ROLLED_BACK, SUCCEEDED, FAILED}
)

# Human-readable outcomes recorded for a finished request.
RESULT_SUCCESS: Final[str] = "success"
RESULT_ROLLED_BACK: Final[str] = "rolled_back"
RESULT_FAILED: Final[str] = "failed"


@dataclass(frozen=True)
class Entry:
    """One file-transfer item of a restore request."""

    source: str
    target: str
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "target": self.target,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "Entry":
        return cls(
            source=str(data["source"]),
            target=str(data["target"]),
            size=int(data["size"]),
            sha256=str(data["sha256"]),
        )


@dataclass(frozen=True)
class RestoreResult:
    """Snapshot of a request's persisted status/result."""

    request_id: str
    state: str
    result: str | None
    message: str | None

    @property
    def succeeded(self) -> bool:
        return self.state == SUCCEEDED

    @property
    def rolled_back(self) -> bool:
        return self.state == ROLLED_BACK

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "state": self.state,
            "result": self.result,
            "message": self.message,
        }
