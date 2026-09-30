"""真实子进程 SIGKILL 覆盖中断：部分替换、全部替换未返回、recover 再中断、回退。"""

import json

NEW_A = b"NEW-A" * 1000
NEW_B = b"NEW-B" * 1000
OLD_A = b"OLD-A" * 1000


def _setup(env):
    """一组目标：a.db 已存在（旧内容），b.db 原先不存在。"""
    (env.tgt / "a.db").write_bytes(OLD_A)
    entries = [
        env.make_entry("a.db", NEW_A),
        env.make_entry("b.db", NEW_B),
    ]
    entries_path = env.write_entries_json(entries)
    return entries, entries_path


def _assert_final_new_state(env, request_id):
    assert env.target_bytes("a.db") == NEW_A
    assert env.target_bytes("b.db") == NEW_B
    info = env.lib.inspect(request_id)
    assert info["status"] == "success"
    assert env.staging_leftovers() == []
    assert env.target_temp_leftovers() == []


def test_kill_after_partial_apply_then_recover_killed_during_cleanup(env):
    entries, entries_path = _setup(env)

    proc = env.spawn(["restore", "req-1", str(entries_path)],
                     pause_at="after_first_apply")
    env.wait_ready("after_first_apply")
    env.kill(proc)
    assert proc.returncode == -9

    # 中断瞬间：a.db 已替换、b.db 未替换（新旧混杂，由 recover 收尾）
    assert env.target_bytes("a.db") == NEW_A
    assert env.target_bytes("b.db") is None

    # 删除源文件：recover 不得依赖源
    for e in entries:
        __import__("os").unlink(e["source"])

    # recover 在清理暂存途中再次被强杀
    proc = env.spawn(["recover"], pause_at="recover_cleanup")
    env.wait_ready("recover_cleanup")
    env.kill(proc)

    # 再次 recover 必须收敛
    proc = env.spawn(["recover"])
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out

    _assert_final_new_state(env, "req-1")


def test_kill_after_all_applied_before_return_then_recover_killed_during_apply(env):
    entries, entries_path = _setup(env)

    proc = env.spawn(["restore", "req-2", str(entries_path)],
                     pause_at="before_done")
    env.wait_ready("before_done")
    env.kill(proc)

    # 全部目标已替换，但 restore 未返回、结果未落盘
    assert env.target_bytes("a.db") == NEW_A
    assert env.target_bytes("b.db") == NEW_B
    assert env.lib.inspect("req-2")["status"] == "interrupted_committed"

    for e in entries:
        __import__("os").unlink(e["source"])

    # recover 在前滚重放途中被强杀
    proc = env.spawn(["recover"], pause_at="recover_after_first_apply")
    env.wait_ready("recover_after_first_apply")
    env.kill(proc)

    proc = env.spawn(["recover"])
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out

    _assert_final_new_state(env, "req-2")


def test_kill_before_commit_rolls_back(env):
    entries, entries_path = _setup(env)

    proc = env.spawn(["restore", "req-3", str(entries_path)],
                     pause_at="before_commit")
    env.wait_ready("before_commit")
    env.kill(proc)

    for e in entries:
        __import__("os").unlink(e["source"])

    proc = env.spawn(["recover"])
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0, out

    # 回退：目标必须恰好是调用 restore 前的完整旧状态
    assert env.target_bytes("a.db") == OLD_A
    assert env.target_bytes("b.db") is None
    assert env.lib.inspect("req-3")["status"] == "rolled_back"
    assert env.staging_leftovers() == []
    assert env.target_temp_leftovers() == []


def test_successful_restore_survives_restart(env):
    entries, entries_path = _setup(env)
    proc = env.spawn(["restore", "req-4", str(entries_path)])
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 0
    assert json.loads(out)["outcome"] == "success"

    # 模拟重启：新实例 + recover，不得回退已成功的请求
    from restorelib import RestoreLibrary
    lib2 = RestoreLibrary(str(env.work))
    lib2.recover()

    assert env.target_bytes("a.db") == NEW_A
    assert env.target_bytes("b.db") == NEW_B
    assert lib2.inspect("req-4")["status"] == "success"
