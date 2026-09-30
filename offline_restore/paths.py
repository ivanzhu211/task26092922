"""On-disk layout of a manager's working directory.

::

    <workdir>/
      state.db                 SQLite metadata (states, entries)
      requests/<id>/
        new/<idx>-<basename>   staged new contents (hard links/copies)
        applied.marker         entries already durably replaced
"""

from __future__ import annotations

import os

DB_NAME = "state.db"
REQUESTS_DIR = "requests"
NEW_DIR = "new"
APPLIED_MARKER = "applied.marker"

_ALLOWED = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@"
)


def db_path(workdir: str) -> str:
    return os.path.join(workdir, DB_NAME)


def requests_dir(workdir: str) -> str:
    return os.path.join(workdir, REQUESTS_DIR)


def safe_request_id(request_id: str) -> str:
    """Restrict request ids used in filesystem path components."""
    if not request_id or not request_id.strip():
        raise ValueError("request_id must be a non-empty string")
    if request_id in (".", "..") or "/" in request_id or "\0" in request_id:
        raise ValueError(f"unsafe request_id: {request_id!r}")
    if any(ch not in _ALLOWED for ch in request_id):
        raise ValueError(
            f"request_id may only contain [A-Za-z0-9-_.@]: {request_id!r}"
        )
    return request_id


def request_dir(workdir: str, request_id: str) -> str:
    return os.path.join(
        requests_dir(workdir), safe_request_id(request_id)
    )


def new_dir(workdir: str, request_id: str) -> str:
    return os.path.join(request_dir(workdir, request_id), NEW_DIR)


def applied_marker(workdir: str, request_id: str) -> str:
    return os.path.join(request_dir(workdir, request_id), APPLIED_MARKER)


def entry_filename(index: int, target: str) -> str:
    """Staging filename for an entry: zero-padded index plus a sanitized
    basename, keeping the on-disk order aligned with manifest order."""
    base = os.path.basename(os.path.normpath(target))
    safe = "".join(ch if ch in _ALLOWED else "_" for ch in base)
    return f"{index:08d}-{safe}"
