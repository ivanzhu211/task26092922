"""Subprocess entry point used by crash tests.

Reads a JSON job spec from argv[1] (or stdin) and writes a JSON result to
the path named by ``OR_RESULT_FILE``. Crash tests launch this as a real
separate process, wait for a deterministic pause-point "hit" file, then
``SIGKILL`` it - simulating forced process termination rather than an
in-process exception.

Job format::

    {"workdir": "...",
     "op": "restore" | "recover" | "inspect",
     "request_id": "...",
     "entries": [ {"source","target","size","sha256"}, ... ]}

Pause control via environment variables:

* ``OR_PAUSE_AT``        - event name at which the process blocks
* ``OR_PAUSE_HIT_FILE``  - file touched when the pause point is reached
* ``OR_PAUSE_RELEASE``   - if set, pause until this file appears;
                           otherwise the process pauses forever (to be
                           killed)
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Callable

from .manager import RestoreManager
from .model import Entry

HIT_POLL_INTERVAL = 0.005


def make_pause_hook() -> Callable[[str], None] | None:
    wanted = os.environ.get("OR_PAUSE_AT")
    if not wanted:
        return None
    hit_file = os.environ.get("OR_PAUSE_HIT_FILE")
    release_file = os.environ.get("OR_PAUSE_RELEASE")

    def hook(event: str) -> None:
        if event != wanted:
            return
        if hit_file:
            with open(hit_file, "w", encoding="ascii") as fh:
                fh.write(event)
                fh.flush()
                os.fsync(fh.fileno())
        if release_file:
            while not os.path.exists(release_file):
                time.sleep(HIT_POLL_INTERVAL)
        else:
            # Block until the parent delivers SIGKILL.
            while True:
                time.sleep(3600)

    return hook


def run(job: dict[str, Any]) -> dict[str, Any]:
    manager = RestoreManager(job["workdir"], pause_hook=make_pause_hook())
    op = job["op"]
    if op == "restore":
        entries = [Entry.from_dict(item) for item in job.get("entries", [])]
        result = manager.restore(job["request_id"], entries)
        return {"ok": True, "result": result.to_dict()}
    if op == "recover":
        results = manager.recover()
        return {"ok": True, "results": [item.to_dict() for item in results]}
    if op == "inspect":
        result = manager.inspect(job["request_id"])
        return {"ok": True, "result": result.to_dict()}
    return {"ok": False, "error": f"unknown op: {op}"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv:
        with open(argv[0], "r", encoding="utf-8") as fh:
            job = json.load(fh)
    else:
        job = json.load(sys.stdin)

    result_file = os.environ.get("OR_RESULT_FILE")
    try:
        payload = run(job)
    except Exception as exc:  # serialized back to the test process
        payload = {
            "ok": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    text = json.dumps(payload)
    if result_file:
        tmp = result_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, result_file)
    else:
        sys.stdout.write(text)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
