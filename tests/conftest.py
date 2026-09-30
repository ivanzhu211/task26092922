import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from restorelib import RestoreLibrary  # noqa: E402
from restorelib.pause import ready_file  # noqa: E402


@pytest.fixture()
def env(tmp_path):
    """构造一套 源目录/目标目录/工作目录 的测试环境。"""
    return Env(tmp_path)


class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.src = tmp_path / "src"
        self.tgt = tmp_path / "tgt"
        self.work = tmp_path / "work"
        for d in (self.src, self.tgt, self.work):
            d.mkdir()
        self.lib = RestoreLibrary(str(self.work))

    def write_source(self, name, data):
        path = self.src / name
        path.write_bytes(data)
        return path

    def make_entry(self, name, data, target_name=None):
        path = self.write_source(name, data)
        return {
            "source": str(path),
            "target": str(self.tgt / (target_name or name)),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def write_entries_json(self, entries, name="entries.json"):
        path = self.root / name
        path.write_text(json.dumps(entries))
        return path

    def target_bytes(self, name):
        path = self.tgt / name
        return path.read_bytes() if path.exists() else None

    def spawn(self, args, pause_at=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        if pause_at:
            env["RESTORELIB_PAUSE_AT"] = pause_at
        return subprocess.Popen(
            [sys.executable, "-m", "restorelib.driver", str(self.work)] + args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def wait_ready(self, name, timeout=30.0):
        path = ready_file(str(self.work), name)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.02)
        raise TimeoutError("pause point %r not reached" % name)

    @staticmethod
    def kill(proc):
        proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=30)

    def journal_dirs(self):
        req = self.work / "requests"
        return sorted(p.name for p in req.iterdir()) if req.exists() else []

    def staging_leftovers(self):
        req = self.work / "requests"
        if not req.exists():
            return []
        return [str(p) for p in req.rglob("*") if p.is_dir() and p.name == "staging"
                and any(p.iterdir())]

    def target_temp_leftovers(self):
        return [str(p) for p in self.tgt.glob("*.restorelib-tmp")]
