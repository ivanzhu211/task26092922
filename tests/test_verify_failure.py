"""最后一个源文件校验失败：所有目标字节与缺失状态均保持原状。"""

import pytest

from restorelib import VerificationError


def test_last_source_verification_fails(env):
    old_a = b"OLD-A" * 500
    (env.tgt / "a.db").write_bytes(old_a)
    # b.db 原先不存在

    good = env.make_entry("a.db", b"NEW-A" * 500)
    bad = env.make_entry("b.db", b"NEW-B" * 500)
    bad["sha256"] = "0" * 64  # 篡改哈希，使最后一个源校验失败

    with pytest.raises(VerificationError):
        env.lib.restore("req-verify", [good, bad])

    assert env.target_bytes("a.db") == old_a
    assert env.target_bytes("b.db") is None
    # 校验失败发生在写日志之前，工作目录不得留下任何请求记录
    assert env.journal_dirs() == []
    assert env.lib.inspect("req-verify")["status"] == "unknown"


def test_last_source_size_mismatch(env):
    old_a = b"OLD-A" * 500
    (env.tgt / "a.db").write_bytes(old_a)

    good = env.make_entry("a.db", b"NEW-A" * 500)
    bad = env.make_entry("b.db", b"NEW-B" * 500)
    bad["size"] += 1  # 长度不符

    with pytest.raises(VerificationError):
        env.lib.restore("req-size", [good, bad])

    assert env.target_bytes("a.db") == old_a
    assert env.target_bytes("b.db") is None
    assert env.journal_dirs() == []
