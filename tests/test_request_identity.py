"""Successful requests are immutable; request_id identity is enforced.

Sequence:
  1. Request A restores targets (fresh process) and succeeds.
  2. Request B restores the *same* fixed target paths with different
     content (another fresh process) and succeeds.
  3. Simulating a restart, retrying A in a new process must return A's
     recorded success WITHOUT touching the targets - so B's content
     remains.
  4. Reusing A's id with a different hash/path must be rejected before
     any target change.
"""

from __future__ import annotations

import os

from offline_restore.model import Entry

from conftest import (
    launch_worker,
    recover_job,
    restore_job,
    sha256_bytes,
    target_state,
    write_file,
)


def _entry(src_dir, name, data, tgt_dir, target_name=None):
    write_file(os.path.join(src_dir, name), data)
    return Entry(
        os.path.join(src_dir, name),
        os.path.join(tgt_dir, target_name or f"{name}.db"),
        len(data),
        sha256_bytes(data),
    )


def test_retry_of_old_success_cannot_overwrite_later_restore(tmp_path):
    work = str(tmp_path / "work")
    src = str(tmp_path / "backup")
    tgt = str(tmp_path / "db")
    for d in (work, src, tgt):
        os.makedirs(d)

    content_a = b"AAA-content" * 100
    content_b = b"BBB-content" * 100

    entries_a = [_entry(src, "a", content_a, tgt)]
    target = entries_a[0].target

    # 1. A succeeds (real process).
    handle, _ = launch_worker(
        tmp_path, restore_job(work, "req-A", entries_a)
    )
    res = handle.join()
    assert res["ok"] and res["result"]["state"] == "succeeded"
    assert open(target, "rb").read() == content_a

    # 2. B restores the same fixed target path with different content.
    entries_b = [_entry(src, "b", content_b, tgt, target_name="a.db")]
    handle, _ = launch_worker(
        tmp_path, restore_job(work, "req-B", entries_b)
    )
    res = handle.join()
    assert res["ok"] and res["result"]["state"] == "succeeded"
    assert open(target, "rb").read() == content_b

    # 3. "Restart": recover first, then retry A. Sources for A are gone,
    #    proving retry neither reads sources nor rewrites targets.
    os.unlink(entries_a[0].source)
    os.unlink(entries_b[0].source)
    handle, _ = launch_worker(tmp_path, recover_job(work))
    rec = handle.join()
    assert rec["ok"] is True and rec["results"] == []

    handle, _ = launch_worker(
        tmp_path, restore_job(work, "req-A", entries_a)
    )
    res = handle.join()
    assert res["ok"] is True
    assert res["result"]["state"] == "succeeded"
    assert res["result"]["result"] == "success"
    # B's content still owns the fixed target path.
    assert open(target, "rb").read() == content_b


def test_reuse_id_with_different_hash_rejected_before_change(tmp_path):
    work = str(tmp_path / "work")
    src = str(tmp_path / "backup")
    tgt = str(tmp_path / "db")
    for d in (work, src, tgt):
        os.makedirs(d)

    data1 = b"version-1"
    entries1 = [_entry(src, "f1", data1, tgt)]
    handle, _ = launch_worker(
        tmp_path, restore_job(work, "fixed-id", entries1)
    )
    assert handle.join()["ok"] is True

    # Same request_id, different sha256 (and source content).
    data2 = b"version-2-different"
    write_file(os.path.join(src, "f2"), data2)
    conflicting = Entry(
        os.path.join(src, "f2"),
        entries1[0].target,
        len(data2),
        sha256_bytes(data2),
    )
    handle, _ = launch_worker(
        tmp_path, restore_job(work, "fixed-id", [conflicting])
    )
    payload = handle.join()
    assert payload["ok"] is False
    assert payload["error_type"] == "ConflictError"

    # Target still holds the originally recorded content.
    assert open(entries1[0].target, "rb").read() == data1

    # Same id, same hash but a different target path -> also conflict.
    moved = Entry(
        os.path.join(src, "f1"),
        os.path.join(tgt, "other.db"),
        len(data1),
        sha256_bytes(data1),
    )
    handle, _ = launch_worker(
        tmp_path, restore_job(work, "fixed-id", [moved])
    )
    payload = handle.join()
    assert payload["ok"] is False
    assert payload["error_type"] == "ConflictError"
    assert not os.path.exists(os.path.join(tgt, "other.db"))
    assert open(entries1[0].target, "rb").read() == data1
