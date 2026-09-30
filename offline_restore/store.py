"""Durable request metadata backed by SQLite (WAL, full synchronous).

The database is the sole durable decision record. Every state transition
is its own committed transaction (``synchronous=FULL``), so once
``succeeded``/``rolled_back`` is committed the outcome is permanent and a
later restart can never change it.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Iterable

from . import paths as layout
from .errors import ConflictError
from .model import (
    APPLYING,
    FAILED,
    ROLLED_BACK,
    ROLLING_BACK,
    STAGING,
    SUCCEEDED,
    Entry,
    RestoreResult,
    RESULT_FAILED,
    RESULT_ROLLED_BACK,
    RESULT_SUCCESS,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id          TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    result      TEXT,
    message     TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    request_id  TEXT NOT NULL,
    position    INTEGER NOT NULL,
    source      TEXT NOT NULL,
    target      TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    PRIMARY KEY (request_id, position),
    FOREIGN KEY (request_id) REFERENCES requests(id)
);
CREATE INDEX IF NOT EXISTS idx_entries_request ON entries(request_id);
"""

_STATE_RESULT = {
    SUCCEEDED: RESULT_SUCCESS,
    ROLLED_BACK: RESULT_ROLLED_BACK,
    FAILED: RESULT_FAILED,
}


class Store:
    """Owns the SQLite metadata database inside the working directory."""

    def __init__(self, workdir: str) -> None:
        self.workdir = os.path.abspath(workdir)
        os.makedirs(self.workdir, exist_ok=True)
        self._conn = sqlite3.connect(
            layout.db_path(self.workdir), timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- reads ------------------------------------------------------------

    def get(self, request_id: str) -> RestoreResult | None:
        cur = self._conn.execute(
            "SELECT id, state, result, message FROM requests WHERE id = ?",
            (request_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return RestoreResult(
            request_id=row["id"],
            state=row["state"],
            result=row["result"],
            message=row["message"],
        )

    def get_entries(self, request_id: str) -> list[Entry]:
        cur = self._conn.execute(
            "SELECT source, target, size, sha256 FROM entries "
            "WHERE request_id = ? ORDER BY position",
            (request_id,),
        )
        return [
            Entry(
                source=row["source"],
                target=row["target"],
                size=row["size"],
                sha256=row["sha256"],
            )
            for row in cur.fetchall()
        ]

    def active_requests(self) -> list[str]:
        cur = self._conn.execute(
            "SELECT id FROM requests WHERE state IN (?, ?, ?) "
            "ORDER BY created_at, id",
            (STAGING, ROLLING_BACK, APPLYING),
        )
        return [row["id"] for row in cur.fetchall()]

    def all_request_ids(self) -> list[str]:
        cur = self._conn.execute("SELECT id FROM requests ORDER BY created_at, id")
        return [row["id"] for row in cur.fetchall()]

    # -- writes -----------------------------------------------------------

    def create_request(
        self, request_id: str, entries: list[Entry], state: str
    ) -> None:
        """Insert a new request and its entries atomically.

        Raises ConflictError if the id already exists (caller checks
        identity first).
        """
        import time

        now = time.time()
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO requests "
                    "(id, state, result, message, created_at, updated_at) "
                    "VALUES (?, ?, NULL, NULL, ?, ?)",
                    (request_id, state, now, now),
                )
                self._conn.executemany(
                    "INSERT INTO entries "
                    "(request_id, position, source, target, size, sha256) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            request_id,
                            pos,
                            entry.source,
                            entry.target,
                            entry.size,
                            entry.sha256.lower(),
                        )
                        for pos, entry in enumerate(entries)
                    ],
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                f"request already exists: {request_id}"
            ) from exc

    def update_state(
        self,
        request_id: str,
        state: str,
        result: str | None = None,
        message: str | None = None,
    ) -> None:
        """Persist a state transition as its own durable commit.

        Terminal states get their canonical result filled in unless one is
        explicitly supplied.
        """
        import time

        if result is None:
            result = _STATE_RESULT.get(state)
        now = time.time()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE requests SET state = ?, result = ?, message = ?, "
                "updated_at = ? WHERE id = ?",
                (state, result, message, now, request_id),
            )
            if cur.rowcount != 1:
                raise ConflictError(
                    f"unknown request during update: {request_id}"
                )
