"""Killed before the APPLYING decision: recovery must roll back.

No target was ever replaced in STAGING, so after recovery the whole
group must be byte-for-byte the pre-restore state (including absent
targets), the outcome must be recorded as "rolled_back", and scratch
must be gone - even if recovery itself is killed mid-cleanup.
"""

from __future__ import annotations

import os

from offline_restore.model import Entry

from conftest import (
    inspect_job,
    launch_worker,
    recover_job,
    restore_job,
    sha256_bytes,
    target_state,
    write_file,
)


def _build(tmp_path):
    work = str(tmp_path / "work")
    src = str(tmp_path / "backup")
    tgt = str(tmp_path / "db")
    for d in (work, src, tgt):
        os.makedirs(d)

    old_a = b"pre-restore-A"
    new_a = b"would-be-A" * 100
    new_b = b"would-be-B" * 100
    write_file(os.path.join(src, "a"), new_a)
    write_file(os.path.join(src, "b"), new_b)
    write_file(os.path.join(tgt, "a.db"), old_a)   # exists before

    entries = [
        Entry(os.path.join(src, "a"), os.path.join(tgt, "a.db"),
              len(new_a), sha256_bytes(new_a)),
        Entry(os.path.join(src, "b"), os.path.join(tgt, "b.db"),
              len(new_b), sha256_bytes(new_b)),
    ]
    paths = [os.path.join(tgt, n) for n in ("a.db", "b.db")]
    old_state = {
        paths[0]: (True, sha256_bytes(old_a)),
        paths[1]: (False, ""),
    }
    return work, src, entries, paths, old_state, old_a


def test_kill_during_staging_converges_to_rolled_back(tmp_path):
    work, src, entries, paths, old_state, old_a = _build(tmp_path)

    handle, hit = launch_worker(
        tmp_path,
        restore_job(work, "rq-rollback", entries),
        pause_at="stage:entry:after:1",
    )
    handle.wait_hit(hit)
    handle.kill()

    # No target changed; b.db still absent.
    assert target_state(paths) == old_state

    # Sources vanish: rollback must not need them.
    os.unlink(os.path.join(src, "a"))
    os.unlink(os.path.join(src, "b"))

    # Kill a recovery attempt during cleanup; outcome already durable.
    handle, hit = launch_worker(
        tmp_path, recover_job(work),
        pause_at="recover:rollback:after",
    )
    handle.wait_hit(hit)
    handle.kill()

    handle, _ = launch_worker(tmp_path, inspect_job(work, "rq-rollback"))
    payload = handle.join()
    assert payload["ok"] is True
    assert payload["result"]["state"] == "rolled_back"
    assert payload["result"]["result"] == "rolled_back"

    # Final recovery converges; no mixed/new state remains.
    handle, _ = launch_worker(tmp_path, recover_job(work))
    results = handle.join()["results"]
    assert [r["state"] for r in results] == ["rolled_back"]

    assert target_state(paths) == old_state
    assert open(paths[0], "rb").read() == old_a
    assert not os.path.exists(paths[1])
    assert not os.path.exists(os.path.join(work, "requests", "rq-rollback"))

    # Repeated restarts stay at "rolled_back", never resurrect or apply.
    handle, _ = launch_worker(tmp_path, recover_job(work))
    assert handle.join()["results"] == []
    assert target_state(paths) == old_state
