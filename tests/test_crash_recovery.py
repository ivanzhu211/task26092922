"""Crash tests with real SIGKILL of separate processes.

Scenarios (sources are deleted before recovery, so recovery relies only
on the working directory + target files):

* kill restore after only some targets were replaced -> resume forward
  to the full new state; kill recovery once more mid-advance; a final
  recovery converges to success.
* kill restore after all targets were replaced but before restore
  returned -> recovery records success; kill recovery once more during
  scratch cleanup; a final recovery still reports success with no scratch.

Every group mixes one pre-existing target with one originally-missing
target. Unrelated files in the target directory must never change.
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
    work = tmp_path / "work"
    src = tmp_path / "backup"
    tgt = tmp_path / "db"
    for d in (work, src, tgt):
        d.mkdir(parents=True)

    old_a = b"old-A" * 500
    new_a = b"new-A" * 700
    new_b = b"new-B" * 333
    write_file(src / "a", new_a)
    write_file(src / "b", new_b)
    write_file(tgt / "a.db", old_a)           # pre-existing
    # b.db is a brand-new target
    unrelated = b"unrelated-file"
    write_file(tgt / "keep.db", unrelated)    # must never change

    # Sorted-by-target order puts a.db at index 0, b.db at index 1.
    entries = [
        Entry(str(src / "a"), str(tgt / "a.db"),
              len(new_a), sha256_bytes(new_a)),
        Entry(str(src / "b"), str(tgt / "b.db"),
              len(new_b), sha256_bytes(new_b)),
    ]
    paths = [str(tgt / "a.db"), str(tgt / "b.db"), str(tgt / "keep.db")]
    old_state = {
        paths[0]: (True, sha256_bytes(old_a)),
        paths[1]: (False, ""),
        paths[2]: (True, sha256_bytes(unrelated)),
    }
    new_state = {
        paths[0]: (True, sha256_bytes(new_a)),
        paths[1]: (True, sha256_bytes(new_b)),
        paths[2]: (True, sha256_bytes(unrelated)),
    }
    return {
        "work": str(work),
        "srcdir": str(src),
        "paths": paths,
        "entries": entries,
        "old_state": old_state,
        "new_state": new_state,
    }


def _delete_sources(ctx):
    for name in ("a", "b"):
        os.unlink(os.path.join(ctx["srcdir"], name))


def _recover_killed(ctx, tmp_path, pause_at):
    handle, hit = launch_worker(
        tmp_path, recover_job(ctx["work"]), pause_at=pause_at
    )
    handle.wait_hit(hit)
    handle.kill()


def _recover_finish(ctx, tmp_path):
    handle, _ = launch_worker(tmp_path, recover_job(ctx["work"]))
    payload = handle.join()
    assert payload["ok"] is True, payload
    return payload


def _assert_success_recorded(ctx, tmp_path, request_id):
    handle, _ = launch_worker(
        tmp_path, inspect_job(ctx["work"], request_id)
    )
    payload = handle.join()
    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["state"] == "succeeded"
    assert result["result"] == "success"


def _assert_no_scratch(ctx):
    requests_root = os.path.join(ctx["work"], "requests")
    leftovers = [
        os.path.join(root, name)
        for root, _dirs, files in os.walk(requests_root)
        for name in files
    ]
    assert leftovers == [], leftovers
    # Request directory for this id is removed entirely.
    assert not os.path.exists(
        os.path.join(requests_root, "rq-mixed")
    )


def test_partial_replace_then_kill_recovery_forward(tmp_path):
    ctx = _build(tmp_path)
    paths = ctx["paths"]

    # Kill restore right after the first target was replaced.
    handle, hit = launch_worker(
        tmp_path,
        restore_job(ctx["work"], "rq-mixed", ctx["entries"]),
        pause_at="apply:entry:after:0",
    )
    handle.wait_hit(hit)
    handle.kill()

    # Intermediate state: new a.db, missing b.db (a mixed state).
    mid = target_state(paths)
    assert mid[paths[0]] == ctx["new_state"][paths[0]]
    assert mid[paths[1]] == (False, "")
    assert mid[paths[2]] == ctx["old_state"][paths[2]]

    _delete_sources(ctx)

    # Recovery resumes forward; kill it after the second target lands.
    _recover_killed(ctx, tmp_path, "recover:apply:entry:after:1")
    # The group must still be coherent or already finished - never old/new
    # in a way that contradicts the protocol. Here apply is complete but
    # not yet announced.
    assert target_state(paths) == ctx["new_state"]

    # Kill once more, this time during cleanup.
    _recover_killed(ctx, tmp_path, "recover:cleanup:file:after:0")

    # Final recovery converges to the full new state.
    payload = _recover_finish(ctx, tmp_path)
    assert len(payload["results"]) == 1
    assert payload["results"][0]["state"] == "succeeded"

    assert target_state(paths) == ctx["new_state"]
    _assert_success_recorded(ctx, tmp_path, "rq-mixed")
    _assert_no_scratch(ctx)

    # Targets are regular files, not symlinks.
    for path in paths[:2]:
        assert os.path.isfile(path) and not os.path.islink(path)

    # Another restart: nothing left to do, outcome stays "success".
    handle, _ = launch_worker(tmp_path, recover_job(ctx["work"]))
    again = handle.join()
    assert again["ok"] is True and again["results"] == []
    assert target_state(paths) == ctx["new_state"]
    _assert_no_scratch(ctx)


def test_all_replaced_before_return_then_cleanup_kill(tmp_path):
    ctx = _build(tmp_path)
    paths = ctx["paths"]

    # All targets replaced, but restore was killed before it could commit
    # the result / return.
    handle, hit = launch_worker(
        tmp_path,
        restore_job(ctx["work"], "rq-mixed", ctx["entries"]),
        pause_at="apply:after",
    )
    handle.wait_hit(hit)
    handle.kill()

    # Files already show the complete new content; the DB decision is not
    # yet durable. Recovery must finish forward and record success.
    assert target_state(paths) == ctx["new_state"]
    _delete_sources(ctx)

    # Kill during the cleanup portion of recovery (success decision is
    # already durable at this point).
    _recover_killed(ctx, tmp_path, "recover:cleanup:file:after:0")

    handle, _ = launch_worker(
        tmp_path, inspect_job(ctx["work"], "rq-mixed")
    )
    payload = handle.join()
    assert payload["ok"] is True
    assert payload["result"]["state"] == "succeeded"

    payload = _recover_finish(ctx, tmp_path)
    assert [r["state"] for r in payload["results"]] == ["succeeded"] or \
        payload["results"] == []

    assert target_state(paths) == ctx["new_state"]
    _assert_success_recorded(ctx, tmp_path, "rq-mixed")
    _assert_no_scratch(ctx)
