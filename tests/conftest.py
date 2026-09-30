"""Shared helpers for crash-recovery tests.

Every interrupted scenario runs a *real* separate Python process that
executes ``offline_restore.worker``. The child blocks at a deterministic
pause point (an environment-selected event) and the test kills it with
SIGKILL - never an in-process exception or a clean exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

WAIT_TIMEOUT = 15.0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_file(path: str | os.PathLike[str], data: bytes) -> str:
    path = str(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def target_state(paths: list[str]) -> dict[str, tuple[bool, str]]:
    """Snapshot (exists, sha256-of-content) for the given target paths."""
    state: dict[str, tuple[bool, str]] = {}
    for path in paths:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                state[path] = (True, hashlib.sha256(fh.read()).hexdigest())
        else:
            state[path] = (False, "")
    return state


def _env_for_pause(
    hit_file: str | None,
    pause_at: str | None,
    release_file: str | None,
    extra: dict[str, str] | None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if pause_at:
        env["OR_PAUSE_AT"] = pause_at
    if hit_file:
        env["OR_PAUSE_HIT_FILE"] = hit_file
    if release_file:
        env["OR_PAUSE_RELEASE"] = release_file
    if extra:
        env.update(extra)
    return env


class WorkerHandle:
    def __init__(self, proc: subprocess.Popen[bytes], result_file: str) -> None:
        self.proc = proc
        self.result_file = result_file

    def wait_hit(self, hit_file: str, timeout: float = WAIT_TIMEOUT) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(hit_file):
                return
            code = self.proc.poll()
            if code is not None:
                out, err = self.proc.communicate(timeout=5)
                raise AssertionError(
                    f"worker exited early (code={code}) before hit file; "
                    f"stderr={err.decode(errors='replace')[-2000:]}"
                )
            time.sleep(0.005)
        self.kill()
        raise TimeoutError(f"pause hit file never appeared: {hit_file}")

    def wait_hit_or_finish(
        self, hit_file: str, timeout: float = WAIT_TIMEOUT
    ) -> str:
        """Return "hit" if the pause point was reached, or "finished"
        if the process exited first (event never occurs on this path)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(hit_file):
                return "hit"
            if self.proc.poll() is not None:
                return "finished"
            time.sleep(0.005)
        self.kill()
        raise TimeoutError(f"neither hit nor exit observed: {hit_file}")

    def kill(self, sig: int = signal.SIGKILL) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass

    def join(self, timeout: float = WAIT_TIMEOUT) -> dict:
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            raise
        if not os.path.exists(self.result_file):
            out, err = self.proc.communicate()
            raise AssertionError(
                f"no result file (exit={code}); "
                f"stderr={err.decode(errors='replace')[-2000:]}"
            )
        with open(self.result_file, "r", encoding="utf-8") as fh:
            return json.load(fh)


def launch_worker(
    tmp_path: Path,
    job: dict,
    *,
    pause_at: str | None = None,
    release_file: str | None = None,
) -> tuple[WorkerHandle, str | None]:
    """Start a worker process for ``job``.

    Returns (handle, hit_file). When ``pause_at`` is given the child will
    touch ``hit_file`` upon reaching that event and block until killed (or
    until ``release_file`` appears).
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    job_file = run_dir / f"job-{os.getpid()}-{len(os.listdir(run_dir))}.json"
    job_file.write_text(json.dumps(job), encoding="utf-8")
    result_file = str(run_dir / f"result-{job_file.stem}.json")

    hit_file: str | None = None
    if pause_at:
        hit_file = str(run_dir / f"hit-{job_file.stem}.json")
        if os.path.exists(hit_file):
            os.unlink(hit_file)

    env = _env_for_pause(hit_file, pause_at, release_file,
                         {"OR_RESULT_FILE": result_file})
    proc = subprocess.Popen(
        [sys.executable, "-m", "offline_restore.worker", str(job_file)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return WorkerHandle(proc, result_file), hit_file


def restore_job(workdir: str, request_id: str, entries: list) -> dict:
    return {
        "workdir": workdir,
        "op": "restore",
        "request_id": request_id,
        "entries": [
            {
                "source": e.source,
                "target": e.target,
                "size": e.size,
                "sha256": e.sha256,
            }
            for e in entries
        ],
    }


def recover_job(workdir: str) -> dict:
    return {"workdir": workdir, "op": "recover"}


def inspect_job(workdir: str, request_id: str) -> dict:
    return {"workdir": workdir, "op": "inspect", "request_id": request_id}

