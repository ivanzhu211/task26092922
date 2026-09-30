"""A 成功后 B 覆盖相同目标；重启后重试 A 不得回写；复用 A 的 ID 改哈希必须拒绝。"""

import hashlib

import pytest

from restorelib import ConflictError, RestoreLibrary

CONTENT_A = b"CONTENT-A" * 1000
CONTENT_B = b"CONTENT-B" * 1000


def test_retry_after_later_request_does_not_overwrite(env):
    entry_a = env.make_entry("a.db", CONTENT_A, target_name="db.sqlite")
    result = env.lib.restore("req-A", [entry_a])
    assert result.outcome == "success"
    assert env.target_bytes("db.sqlite") == CONTENT_A

    entry_b = env.make_entry("b.db", CONTENT_B, target_name="db.sqlite")
    env.lib.restore("req-B", [entry_b])
    assert env.target_bytes("db.sqlite") == CONTENT_B

    # 模拟重启
    lib2 = RestoreLibrary(str(env.work))
    lib2.recover()

    # 删除 A 的源文件：重试成功请求不得读取源
    import os
    os.unlink(entry_a["source"])

    # 重试 A：只返回既有结果，不得再次写入目标
    retry = lib2.restore("req-A", [entry_a])
    assert retry.outcome == "success"
    assert env.target_bytes("db.sqlite") == CONTENT_B


def test_conflicting_hash_with_same_id_rejected(env):
    entry_a = env.make_entry("a.db", CONTENT_A, target_name="db.sqlite")
    env.lib.restore("req-C", [entry_a])

    # 复用同一 ID 但哈希不同：必须在改动目标前拒绝
    tampered = dict(entry_a)
    tampered["sha256"] = hashlib.sha256(b"DIFFERENT").hexdigest()
    with pytest.raises(ConflictError):
        env.lib.restore("req-C", [tampered])
    assert env.target_bytes("db.sqlite") == CONTENT_A

    # 冲突拒绝同样适用于目标路径或长度变化
    other_target = dict(entry_a, target=str(env.tgt / "other.db"))
    with pytest.raises(ConflictError):
        env.lib.restore("req-C", [other_target])
    other_size = dict(entry_a, size=entry_a["size"] + 1)
    with pytest.raises(ConflictError):
        env.lib.restore("req-C", [other_size])

    assert env.target_bytes("db.sqlite") == CONTENT_A
    assert env.target_bytes("other.db") is None


def test_rolled_back_request_can_be_retried_with_same_params(env):
    entries, entries_path = (
        [env.make_entry("a.db", CONTENT_A)],
        None,
    )
    entries_path = env.write_entries_json(entries)
    proc = env.spawn(["restore", "req-R", str(entries_path)],
                     pause_at="before_commit")
    env.wait_ready("before_commit")
    env.kill(proc)
    env.lib.recover()
    assert env.lib.inspect("req-R")["status"] == "rolled_back"

    # 相同参数可重新执行
    result = env.lib.restore("req-R", entries)
    assert result.outcome == "success"
    assert env.target_bytes("a.db") == CONTENT_A
