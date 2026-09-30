"""Repeated SIGKILL at varied points must converge atomically.

Each case starts a 4-target restore (two pre-existing, two new), kills
the restore process at one event, deletes the sources, then runs two
more recovery passes that are themselves killed mid-advance or
mid-cleanup. The final state must be either the complete old state or
the complete new state, the recorded result must agree with the files,
and no scratch may remain.
"""

from __future__ import annotations

import os

import pytest

from offline_restore.model import Entry

from conftest import (
    launch_worker,
    recover_job,
    restore_job,
    sha256_bytes,
    target_state,
    write_file,
)

RESTORE_KILL_EVENTS = [
    "verify:before",
    "stage:entry:after:0",
    "stage:entry:after:3",
    "apply:entry:after:0",
    "apply:entry:after:2",
    "apply:entry:after:3",
    "apply:after",
]
RECOVER_KILL_EVENTS = [
    "recover:rollback:before",
    "recover:rollback:after",
    "recover:apply:entry:after:1",
    "recover:apply:entry:after:3",
    "recover:apply:after",
    "recover:cleanup:file:after:0",
    "recover:cleanup:done",
]


@pytest.mark.parametrize("restore_event", RESTORE_KILL_EVENTS)
@pytest.mark.parametrize("recover_event", RECOVER_KILL_EVENTS)
def test_kills_converge(tmp_path, restore_event, recover_event):
    work = str(tmp_path / "work")
    src = str(tmp_path / "backup")
    tgt = str(tmp_path / "db")
    for d in (work, src, tgt):
        os.makedirs(d)

    old = {f"t{i}.db": f"old-{i}".encode() * 100 for i in range(4)}
    new = {f"t{i}.db": f"new-{i}".encode() * 100 for i in range(4)}
    preexisting = {0, 2}  # t0 and t2 exist; t1 and t3 are new

    entries = []
    for i in range(4):
        name = f"t{i}.db"
        write_file(os.path.join(src, f"s{i}"), new[name])
        if i in preexisting:
            write_file(os.path.join(tgt, name), old[name])
        entries.append(
            Entry(
                os.path.join(src, f"s{i}"),
                os.path.join(tgt, name),
                len(new[name]),
                sha256_bytes(new[name]),
            )
        )
    paths = [os.path.join(tgt, f"t{i}.db") for i in range(4)]

    def expected_state(which: str) -> dict:
        out = {}
        for i, path in enumerate(paths):
            if which == "old":
                if i in preexisting:
                    out[path] = (True, sha256_bytes(old[f"t{i}.db"]))
                else:
                    out[path] = (False, "")
            else:
                out[path] = (True, sha256_bytes(new[f"t{i}.db"]))
        return out

    old_state = expected_state("old")
    new_state = expected_state("new")

    # 1. Restore process, killed at a fixed event.
    handle, hit = launch_worker(
        tmp_path, restore_job(work, "rq-fuzz", entries),
        pause_at=restore_event,
    )
    mode = handle.wait_hit_or_finish(hit)
    if mode == "hit":
        handle.kill()
    else:
        # Process completed before the event (event not on this path).
        payload = handle.join()
        assert payload["ok"]

    # 2. Sources disappear before any recovery.
    for i in range(4):
        try:
            os.unlink(os.path.join(src, f"s{i}"))
        except FileNotFoundError:
            pass

    # 3. Two recovery passes, each killed at a recovery event.
    for _ in range(2):
        handle, hit = launch_worker(
            tmp_path, recover_job(work), pause_at=recover_event
        )
        if handle.wait_hit_or_finish(hit) == "hit":
            handle.kill()
        else:
            handle.join()

    # 4. A final uninterrupted recovery.
    handle, _ = launch_worker(tmp_path, recover_job(work))
    payload = handle.join()
    assert payload["ok"] is True, payload

    files = target_state(paths)
    assert files in (old_state, new_state)

    if files == new_state:
        expected_result = "succeeded"
    else:
        expected_result = "rolled_back"

    recorded = payload["results"]
    if not recorded:
        # Already terminal before this last pass, or the request was
        # killed before its metadata existed: inspect the record.
        from conftest import inspect_job

        handle, _ = launch_worker(
            tmp_path, inspect_job(work, "rq-fuzz")
        )
        info = handle.join()
        if info["ok"]:
            recorded = [info["result"]]
        else:
            # No record exists: this is only legal when nothing ever
            # changed, i.e. the files are still the complete old state.
            assert info["error_type"] == "RequestNotFoundError"
            assert files == old_state
    if recorded:
        assert recorded[0]["state"] == expected_result

    # Scratch fully cleaned; targets are regular files.
    rdir = os.path.join(work, "requests", "rq-fuzz")
    assert not os.path.exists(rdir)
    for path in paths:
        assert not os.path.islink(path)
        if os.path.exists(path):
            assert os.path.isfile(path)

    # 5. One more restart changes nothing.
    handle, _ = launch_worker(tmp_path, recover_job(work))
    assert handle.join()["results"] == []
    assert target_state(paths) == files
