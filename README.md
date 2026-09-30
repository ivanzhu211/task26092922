# restorelib：离线灾备多文件恢复库

将一份已解密、清单可信的一致备份中的数据库文件，恢复到各自固定路径。
输入清单每项包含 `source`、`target`、`size`、`sha256`。

## 用法

```python
from restorelib import RestoreLibrary

lib = RestoreLibrary("/path/to/workdir")   # 每次进程重启后先 recover()
lib.recover()

entries = [
    {"source": "/backup/a.db", "target": "/data/a.db",
     "size": 12345, "sha256": "..."},
]
result = lib.restore("req-001", entries)   # 全部校验通过才改动目标
info = lib.inspect("req-001")              # success / rolled_back / interrupted* / unknown
```

## 崩溃一致性设计

工作目录下 `requests/<request_id>/` 保存每请求的日志：

1. **校验**：流式核验全部源文件长度与 SHA-256，任何失败在改动目标前抛出。
2. **暂存**：把新数据复制进日志区 `staging/` 并 fsync。
3. **提交点**：原子写入 `COMMITTED` 标记。此前中断一律回退（目标尚未被碰）；
   此后中断一律前滚——`recover()` 仅凭暂存数据重放，不依赖源文件存在。
4. **应用**：每个目标经 同目录临时文件 + fsync + `os.replace` + 目录 fsync 原子替换。
5. **结果**：`result.json` 原子落盘（`success` / `rolled_back`），随后清理暂存；
   清理可中断、可由后续 `recover()` 续做。

因此 recover 返回后，整组目标恰好是 restore 前的完整旧状态或清单规定的完整新状态。

## 语义

- `request_id` 绑定目标路径及各自长度、哈希；同 ID 参数冲突在改动目标前拒绝。
- 成功请求的重试只返回既有结果，不读源文件、不写目标。
- 回退后的请求可用相同参数重试。
- 全部读写按 1 MiB 块流式进行，内存占用不随备份总字节数增长。

## 测试

```sh
python3 -m pytest tests/
```

测试用真实子进程 + SIGKILL 覆盖中断点（环境变量 `RESTORELIB_PAUSE_AT`
指定停顿点，库在停顿点落就绪标记文件后睡眠，测试随后强杀进程）。
