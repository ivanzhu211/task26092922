"""High-level transactional restore manager.

The manager is bound to a caller-provided working directory and exposes
three operations:

* ``restore(request_id, entries)``  - verify all sources, then replace the
  fixed target paths.
* ``recover()``                    - finish any interrupted requests using
  only the working directory and target files (no sources needed).
* ``inspect(request_id)``          - read the persisted request status.

Crash-safety protocol
---------------------
The SQLite database is the durable decision record; per-request scratch
files live under ``<workdir>/requests/<id>/``.

* ``staging``  - all sources verified; new contents are staged as hard
  links (fallback: streaming copies) under ``new/``. The APPLYING
  decision is committed *before* any target is renamed, so in this state
  **no target path has been modified** and rollback only needs to keep
  the originals (restoring nothing).
* ``applying`` - the group decision is durable. Targets are replaced one
  at a time with ``os.replace`` and an append-only ``applied.marker``
  tracks progress. A crash here can only be resumed *forward* to the full
  new state.
* ``succeeded`` / ``rolled_back`` are committed with ``synchronous=FULL``
  before the operation returns and before scratch is removed, so a later
  restart can never reverse an announced outcome.

Scratch cleanup is per-file and restarts from whatever remains, so a kill
during cleanup is also recoverable.
"""

from __future__ import annotations

import os
from typing import Callable, Sequence

from . import fsutil, paths as layout
from .errors import (
    ConflictError,
    ProtocolError,
    RequestNotFoundError,
    RestoreError,
    VerificationError,
)
from .hashing import verify_file
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
from .store import Store

PauseHook = Callable[[str], None]


def _validate_entries(entries: Sequence[Entry]) -> list[Entry]:
    """Normalize/sanity-check a manifest; return entries sorted by target."""
    if not entries:
        raise ValueError("entries must be a non-empty sequence")
    normalized: list[Entry] = []
    seen_targets: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Entry):
            raise TypeError("each entry must be an Entry")
        if not isinstance(entry.size, int) or entry.size < 0:
            raise ValueError(f"invalid size for {entry.target}")
        if len(entry.sha256) != 64:
            raise ValueError(f"sha256 must be 64 hex chars: {entry.target}")
        int(entry.sha256, 16)
        source = os.path.abspath(entry.source)
        target = os.path.abspath(entry.target)
        if source == target:
            raise ValueError(f"source and target must differ: {target}")
        if target in seen_targets:
            raise ValueError(f"duplicate target in manifest: {target}")
        if not os.path.isdir(os.path.dirname(target)):
            raise FileNotFoundError(
                f"target parent directory does not exist: {target}"
            )
        seen_targets.add(target)
        normalized.append(
            Entry(
                source=source,
                target=target,
                size=entry.size,
                sha256=entry.sha256.lower(),
            )
        )
    normalized.sort(key=lambda item: item.target)
    return normalized


def _identity(entries: Sequence[Entry]) -> tuple[tuple[str, int, str], ...]:
    """request_id binding: target path + length + sha256 (source excluded)."""
    return tuple(
        (entry.target, entry.size, entry.sha256.lower())
        for entry in sorted(entries, key=lambda item: item.target)
    )


class RestoreManager:
    def __init__(
        self,
        workdir: str,
        *,
        pause_hook: PauseHook | None = None,
    ) -> None:
        self.workdir = os.path.abspath(workdir)
        fsutil.ensure_dir(self.workdir)
        fsutil.ensure_dir(layout.requests_dir(self.workdir))
        self._pause_hook = pause_hook

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def restore(
        self, request_id: str, entries: Sequence[Entry]
    ) -> RestoreResult:
        request_id = layout.safe_request_id(request_id)
        entries = _validate_entries(entries)

        with Store(self.workdir) as store:
            existing = store.get(request_id)
            if existing is not None:
                return self._handle_existing(store, request_id, entries, existing)

            # Another request interrupted in a previous process? Finish it
            # first so transactions never overlap on the same targets.
            self._recover_all(store)

            # Phase 1: verify every source. No target (and no metadata row)
            # is touched until every length and hash checks out.
            self._pause("verify:before")
            for entry in entries:
                verify_file(entry.source, entry.size, entry.sha256)
            self._pause("verify:after")

            # Phase 2: durable STAGING row, then stage and snapshot.
            rdir = layout.request_dir(self.workdir, request_id)
            fsutil.ensure_dir(layout.new_dir(self.workdir, request_id))
            store.create_request(request_id, entries, STAGING)

            for idx, entry in enumerate(entries):
                staged = os.path.join(
                    layout.new_dir(self.workdir, request_id),
                    layout.entry_filename(idx, entry.target),
                )
                if not os.path.exists(staged):
                    fsutil.hardlink_or_copy(entry.source, staged)
                self._pause(f"stage:entry:after:{idx}")

            # Phase 3: commit the group decision *before* touching any
            # target, then move targets forward.
            store.update_state(request_id, APPLYING)
            self._apply_new(store, request_id, entries)
            result = store.get(request_id)
            assert result is not None
            return result

    def recover(self) -> list[RestoreResult]:
        with Store(self.workdir) as store:
            return self._recover_all(store)

    def inspect(self, request_id: str) -> RestoreResult:
        request_id = layout.safe_request_id(request_id)
        with Store(self.workdir) as store:
            result = store.get(request_id)
            if result is None:
                raise RequestNotFoundError(
                    f"unknown request_id: {request_id}"
                )
            return result

    # ------------------------------------------------------------------ #
    # Retry / identity handling
    # ------------------------------------------------------------------ #

    def _handle_existing(
        self,
        store: Store,
        request_id: str,
        entries: list[Entry],
        existing: RestoreResult,
    ) -> RestoreResult:
        stored_entries = store.get_entries(request_id)
        if _identity(stored_entries) != _identity(entries):
            raise ConflictError(
                f"request_id {request_id!r} reused with conflicting "
                "target paths, sizes or hashes"
            )
        if existing.state in (SUCCEEDED, ROLLED_BACK, FAILED):
            # Finished request: return the recorded outcome only. No
            # sources are read and no targets are written - even if a
            # later request has since restored the same targets.
            if os.path.exists(
                layout.request_dir(self.workdir, request_id)
            ):
                self._cleanup(request_id)
            return existing
        # Active record (same process retrying mid-flight): finish it.
        return self._finish_request(store, request_id, stored_entries)

    # ------------------------------------------------------------------ #
    # Recovery
    # ------------------------------------------------------------------ #

    def _recover_all(self, store: Store) -> list[RestoreResult]:
        by_id: dict[str, RestoreResult] = {}
        for request_id in store.active_requests():
            entries = store.get_entries(request_id)
            by_id[request_id] = self._finish_request(
                store, request_id, entries, prefix="recover:"
            )
        # Terminal requests whose cleanup was interrupted.
        for request_id in store.all_request_ids():
            if os.path.exists(
                layout.request_dir(self.workdir, request_id)
            ):
                self._cleanup(request_id, prefix="recover:")
                if request_id not in by_id:
                    result = store.get(request_id)
                    if result is not None:
                        by_id[request_id] = result
        self._sweep_orphan_dirs(store)
        return list(by_id.values())

    def _sweep_orphan_dirs(self, store: Store) -> None:
        """Remove scratch directories with no metadata row (e.g. kill
        between creating the directory and the STAGING commit)."""
        root = layout.requests_dir(self.workdir)
        known = set(store.all_request_ids())
        try:
            names = os.listdir(root)
        except FileNotFoundError:
            return
        for name in names:
            if name not in known:
                fsutil.remove_tree(os.path.join(root, name))

    def _finish_request(
        self,
        store: Store,
        request_id: str,
        entries: list[Entry],
        prefix: str = "",
    ) -> RestoreResult:
        result = store.get(request_id)
        if result is None:
            raise ProtocolError(f"missing metadata for request {request_id}")
        state = result.state

        if state == STAGING:
            # No target could have been replaced yet: make the rollback
            # decision durable, then ensure the old state holds.
            store.update_state(request_id, ROLLING_BACK)
            state = ROLLING_BACK

        if state == ROLLING_BACK:
            self._rollback(store, request_id, entries, prefix)
        elif state == APPLYING:
            self._apply_new(store, request_id, entries, prefix)
        elif state in (SUCCEEDED, ROLLED_BACK, FAILED):
            self._cleanup(request_id, prefix)
        else:
            raise ProtocolError(
                f"unknown persisted state {state!r} for {request_id}"
            )

        final = store.get(request_id)
        assert final is not None
        return final

    # ------------------------------------------------------------------ #
    # Apply / rollback phases
    # ------------------------------------------------------------------ #

    def _read_marker(self, path: str) -> set[int]:
        done: set[int] = set()
        try:
            with open(path, "r", encoding="ascii") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        done.add(int(line))
        except FileNotFoundError:
            return set()
        return done

    def _append_marker(self, path: str, idx: int) -> None:
        with open(path, "a", encoding="ascii") as fh:
            fh.write(f"{idx}\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _apply_new(
        self,
        store: Store,
        request_id: str,
        entries: list[Entry],
        prefix: str = "",
    ) -> None:
        marker = layout.applied_marker(self.workdir, request_id)
        done = self._read_marker(marker)
        for idx, entry in enumerate(entries):
            if idx in done:
                continue
            self._pause(f"{prefix}apply:entry:before:{idx}")
            staged = os.path.join(
                layout.new_dir(self.workdir, request_id),
                layout.entry_filename(idx, entry.target),
            )
            if os.path.exists(staged):
                fsutil.durable_replace(staged, entry.target)
            else:
                # Crash window between the rename and the marker append:
                # the target must already hold the new content.
                try:
                    verify_file(entry.target, entry.size, entry.sha256)
                except VerificationError as exc:
                    raise ProtocolError(
                        f"cannot resume apply for {entry.target}: staged "
                        "content missing and target mismatch"
                    ) from exc
            self._append_marker(marker, idx)
            done.add(idx)
            self._pause(f"{prefix}apply:entry:after:{idx}")

        self._pause(f"{prefix}apply:after")
        store.update_state(request_id, SUCCEEDED, result=RESULT_SUCCESS)
        self._cleanup(request_id, prefix)

    def _rollback(
        self,
        store: Store,
        request_id: str,
        entries: list[Entry],
        prefix: str = "",
    ) -> None:
        # The APPLYING decision is always committed before the first
        # target rename, so a request found in STAGING/ROLLING_BACK has
        # never modified a target. Rollback is therefore a pure metadata
        # decision: record it durably, then drop scratch files.
        self._pause(f"{prefix}rollback:before")
        store.update_state(
            request_id, ROLLED_BACK, result=RESULT_ROLLED_BACK
        )
        self._pause(f"{prefix}rollback:after")
        self._cleanup(request_id, prefix)

    # ------------------------------------------------------------------ #
    # Scratch cleanup (interruptible, restartable, target-independent)
    # ------------------------------------------------------------------ #

    def _cleanup(self, request_id: str, prefix: str = "") -> None:
        rdir = layout.request_dir(self.workdir, request_id)
        if not os.path.lexists(rdir):
            return
        removed = 0
        for current, dirs, files in os.walk(rdir, topdown=False):
            for name in files:
                path = os.path.join(current, name)
                try:
                    os.unlink(path)
                    fsutil.fsync_parent(path)
                except FileNotFoundError:
                    pass
                self._pause(f"{prefix}cleanup:file:after:{removed}")
                removed += 1
            for name in dirs:
                path = os.path.join(current, name)
                try:
                    os.rmdir(path)
                except (FileNotFoundError, OSError):
                    pass
        try:
            os.rmdir(rdir)
        except (FileNotFoundError, OSError):
            pass
        try:
            fsutil.fsync_parent(rdir)
        except FileNotFoundError:
            pass
        self._pause(f"{prefix}cleanup:done")

    # ------------------------------------------------------------------ #

    def _pause(self, event: str) -> None:
        if self._pause_hook is not None:
            self._pause_hook(event)
