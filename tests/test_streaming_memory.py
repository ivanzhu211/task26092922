"""额外内存用量不得随备份总字节数增长：流式校验/暂存/写目标。"""

import os
import tracemalloc


def test_memory_does_not_scale_with_backup_size(env):
    data = os.urandom(32 * 1024 * 1024)  # 32 MB 源文件
    entries = [env.make_entry("big.db", data)]
    del data

    tracemalloc.start()
    env.lib.restore("req-big", entries)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert peak < 16 * 1024 * 1024, "peak python alloc: %d bytes" % peak
    assert env.target_bytes("big.db") is not None
    assert env.lib.inspect("req-big")["status"] == "success"
