"""Last source file fails verification: nothing at the targets changes.

The failing restore runs in a real subprocess; we also SIGKILL it at the
post-verification boundary in a second case to prove the pre-decision
phase leaves no scratch behind.
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


def _build_case(tmp_path):
    work = tmp_path / "work"
    src = tmp_path / "backup"
    tgt = tmp_path / "db"
    for d in (work, src, tgt):
        d.mkdir(parents=True)

    old_a = b"old-A-content"
    old_c = b"old-C-content"
    write_file(tgt / "a.db", old_a)          # pre-existing
    write_file(tgt / "c.db", old_c)          # pre-existing
    # b.db intentionally absent (new target)

    new_a = b"new-A" * 1000
    new_b = b"new-B" * 1000
    bad_data = b"corrupt"
    write_file(src / "a", new_a)
    write_file(src / "b", new_b)
    write_file(src / "c", bad_data)

    entries = [
        Entry(str(src / "a"), str(tgt / "a.db"),
              len(new_a), sha256_bytes(new_a)),
        Entry(str(src / "b"), str(tgt / "b.db"),
              len(new_b), sha256_bytes(new_b)),
        Entry(str(src / "c"), str(tgt / "c.db"),
              999, sha256_bytes(b"different")),
    ]
    paths = [str(tgt / name) for name in ("a.db", "b.db", "c.db")]
    before = target_state(paths)
    return str(work), entries, paths, before, {
        "a": old_a, "c": old_c, "b": None
    }


def test_last_source_verification_failure_leaves_targets_untouched(tmp_path):
    work, entries, paths, before, original = _build_case(tmp_path)

    handle, _ = launch_worker(tmp_path, restore_job(work, "rq-verify", entries))
    payload = handle.join()

    assert payload["ok"] is False
    assert payload["error_type"] == "VerificationError"

    # No target byte changed, and the originally-missing target stays absent.
    assert target_state(paths) == before
    with open(paths[0], "rb") as fh:
        assert fh.read() == original["a"]
    with open(paths[2], "rb") as fh:
        assert fh.read() == original["c"]
    assert not os.path.exists(paths[1])

    # Nothing to recover; no scratch, no metadata row for the request.
    handle2, _ = launch_worker(tmp_path, recover_job(work))
    rec = handle2.join()
    assert rec["ok"] is True and rec["results"] == []
    assert os.listdir(os.path.join(work, "requests")) == []


def test_kill_after_failed_verification_leaves_no_residue(tmp_path):
    work, entries, paths, before, _ = _build_case(tmp_path)
    # Pause *before* verification even starts, kill the process: the
    # targets and the working directory are both untouched.
    handle, hit = launch_worker(
        tmp_path, restore_job(work, "rq-kill-verify", entries),
        pause_at="verify:before",
    )
    handle.wait_hit(hit)
    handle.kill()

    assert target_state(paths) == before
    handle2, _ = launch_worker(tmp_path, recover_job(work))
    rec = handle2.join()
    assert rec["results"] == []
    assert os.listdir(os.path.join(work, "requests")) == []
