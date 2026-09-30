"""离线灾备多文件恢复库。

设计要点：
- 校验全部通过后，把新数据暂存进工作目录的日志区，再写 COMMITTED 标记。
- 只有 COMMITTED 落盘后才允许改动目标；此前被中断一律回退（目标尚未被碰）。
- COMMITTED 之后被中断一律前滚：recover 仅凭日志区暂存数据重放，不依赖源文件。
- 目标写入用 同目录临时文件 + fsync + os.replace + 目录 fsync，保证单文件原子替换。
- 结果（success / rolled_back）原子落盘，清理暂存可中断、可续做。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .errors import ConflictError, RestoreError, VerificationError
from .pause import pause_point

_CHUNK = 1024 * 1024
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]+")

OUTCOME_SUCCESS = "success"
OUTCOME_ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class Entry:
    """清单项：源文件、目标固定路径、期望长度与 SHA-256。"""

    source: str
    target: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RequestResult:
    """恢复请求的最终结果。"""

    request_id: str
    outcome: str  # "success" | "rolled_back"
    targets: List[str] = field(default_factory=list)


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_file(src, dst) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def _sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class RestoreLibrary:
    """绑定调用方工作目录的恢复库实例。"""

    def __init__(self, workdir: str) -> None:
        self._workdir = Path(workdir)
        self._requests = self._workdir / "requests"
        self._requests.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ API

    def restore(self, request_id: str, entries) -> RequestResult:
        normalized = self._normalize_entries(entries)
        self._validate_request_id(request_id)
        params = self._canonical_params(normalized)
        rdir = self._requests / request_id

        if rdir.exists():
            record = self._read_manifest(rdir)
            if record["params"] != params:
                raise ConflictError(
                    "request_id %r 已绑定不同的目标/长度/哈希" % request_id
                )
            result = self._read_result(rdir)
            if result is None:
                # 防御：调用方未先 recover 时，先收尾再决定。
                self._finalize(rdir, pause_prefix="restore")
                result = self._read_result(rdir)
            if result["outcome"] == OUTCOME_SUCCESS:
                # 成功请求的重试：只返回既有结果，不读源、不写目标。
                return RequestResult(
                    request_id, OUTCOME_SUCCESS, list(result["targets"])
                )
            shutil.rmtree(rdir)

        # 核验全部源文件；任何失败都在改动目标之前抛出。
        self._verify_sources(normalized)

        rdir.mkdir()
        self._write_manifest(rdir, request_id, normalized, params)
        staging = rdir / "staging"
        staging.mkdir()
        for index, entry in enumerate(normalized):
            _copy_file(entry.source, staging / str(index))
        _fsync_dir(staging)
        _fsync_dir(rdir)

        pause_point(str(self._workdir), "before_commit")
        self._write_marker(rdir, "COMMITTED")

        self._apply_all(normalized, staging, first_pause="after_first_apply")
        pause_point(str(self._workdir), "before_done")

        self._write_result(rdir, request_id, OUTCOME_SUCCESS, normalized)
        self._cleanup_staging(rdir, pause_name=None)
        return RequestResult(
            request_id, OUTCOME_SUCCESS, [e.target for e in normalized]
        )

    def recover(self) -> None:
        for child in sorted(self._requests.iterdir()):
            if child.is_dir():
                self._finalize(child, pause_prefix="recover")

    def inspect(self, request_id: str) -> dict:
        self._validate_request_id(request_id)
        rdir = self._requests / request_id
        if not rdir.is_dir():
            return {"request_id": request_id, "status": "unknown"}
        result = self._read_result(rdir)
        if result is not None:
            return {
                "request_id": request_id,
                "status": result["outcome"],
                "targets": list(result["targets"]),
            }
        committed = (rdir / "COMMITTED").exists()
        return {
            "request_id": request_id,
            "status": "interrupted_committed" if committed else "interrupted",
        }

    # ------------------------------------------------------------- 内部步骤

    def _finalize(self, rdir: Path, pause_prefix: str) -> None:
        """把被中断的请求收尾到 回退 或 成功，并清理暂存。可重入。"""
        result = self._read_result(rdir)
        if result is not None:
            self._cleanup_staging(rdir, pause_name=pause_prefix + "_cleanup")
            return
        record = self._read_manifest(rdir)
        entries = [
            Entry(e["source"], e["target"], e["size"], e["sha256"])
            for e in record["entries"]
        ]
        request_id = record["request_id"]
        if (rdir / "COMMITTED").exists():
            staging = rdir / "staging"
            self._apply_all(
                entries, staging, first_pause=pause_prefix + "_after_first_apply"
            )
            self._write_result(rdir, request_id, OUTCOME_SUCCESS, entries)
        else:
            self._write_result(rdir, request_id, OUTCOME_ROLLED_BACK, entries)
        self._cleanup_staging(rdir, pause_name=pause_prefix + "_cleanup")

    def _apply_all(
        self, entries: List[Entry], staging: Path, first_pause: Optional[str]
    ) -> None:
        for index, entry in enumerate(entries):
            target = Path(entry.target)
            tmp = target.parent / (target.name + ".restorelib-tmp")
            _copy_file(staging / str(index), tmp)
            os.replace(tmp, target)
            _fsync_dir(target.parent)
            if index == 0 and first_pause:
                pause_point(str(self._workdir), first_pause)

    def _cleanup_staging(self, rdir: Path, pause_name: Optional[str]) -> None:
        staging = rdir / "staging"
        if staging.exists():
            if pause_name:
                pause_point(str(self._workdir), pause_name)
            shutil.rmtree(staging, ignore_errors=True)
        for leftover in rdir.glob("*.tmp"):
            leftover.unlink(missing_ok=True)

    # ------------------------------------------------------------- 持久化

    def _write_manifest(self, rdir, request_id, entries, params) -> None:
        payload = {
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
            "params": params,
        }
        self._atomic_write_json(rdir / "manifest.json", payload)

    def _read_manifest(self, rdir) -> dict:
        with open(rdir / "manifest.json", "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_result(self, rdir, request_id, outcome, entries) -> None:
        payload = {
            "request_id": request_id,
            "outcome": outcome,
            "targets": [e.target for e in entries],
        }
        self._atomic_write_json(rdir / "result.json", payload)

    def _read_result(self, rdir) -> Optional[dict]:
        path = rdir / "result.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _write_marker(self, rdir, name) -> None:
        marker = rdir / name
        marker.write_text("1", encoding="ascii")
        with open(marker, "r") as handle:
            os.fsync(handle.fileno())
        _fsync_dir(rdir)

    def _atomic_write_json(self, path: Path, payload: dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)

    # ------------------------------------------------------------- 校验

    def _verify_sources(self, entries: List[Entry]) -> None:
        failures = []
        for entry in entries:
            try:
                actual_size = os.path.getsize(entry.source)
            except OSError as exc:
                failures.append("%s: %s" % (entry.source, exc))
                continue
            if actual_size != entry.size:
                failures.append(
                    "%s: size %d != expected %d"
                    % (entry.source, actual_size, entry.size)
                )
                continue
            actual_hash = _sha256_of(entry.source)
            if actual_hash != entry.sha256:
                failures.append(
                    "%s: sha256 %s != expected %s"
                    % (entry.source, actual_hash, entry.sha256)
                )
        if failures:
            raise VerificationError(failures)

    @staticmethod
    def _normalize_entries(entries) -> List[Entry]:
        normalized = []
        for item in entries:
            if isinstance(item, Entry):
                normalized.append(item)
            else:
                normalized.append(
                    Entry(
                        source=str(item["source"]),
                        target=str(item["target"]),
                        size=int(item["size"]),
                        sha256=str(item["sha256"]),
                    )
                )
        if not normalized:
            raise RestoreError("entries 不能为空")
        targets = [e.target for e in normalized]
        if len(set(targets)) != len(targets):
            raise RestoreError("清单中存在重复目标路径")
        return normalized

    @staticmethod
    def _canonical_params(entries: List[Entry]):
        return sorted([e.target, e.size, e.sha256] for e in entries)

    @staticmethod
    def _validate_request_id(request_id: str) -> None:
        if (
            not request_id
            or request_id in (".", "..")
            or not _REQUEST_ID_RE.fullmatch(request_id)
        ):
            raise RestoreError("非法 request_id: %r" % (request_id,))
