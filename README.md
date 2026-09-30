# offline_restore

离线灾备场景下的多文件恢复库。把一份已解密备份中的数据库文件，按可信清单
恢复到各自的**固定路径**。不使用版本目录切换、符号链接或专用读取 API：
恢复完成后，每个目标路径直接对应一个普通文件，目标目录中的无关文件不变。

## 适用前提

- 输入已解密、清单可信、源文件来自同一份一致备份（这些步骤不在本库内）。
- 源文件在调用期间不变；源与目标互异、均不含符号链接；目标父目录已存在。
- 同一工作目录同一时刻只有一个调用；调用方在 `restore`/`recover` 期间停用
  目标数据库；进程每次重启先执行 `recover()`。
- 只考虑进程被强制终止（SIGKILL），不考虑断电、介质损坏或外部篡改文件。

## 用法

```python
from offline_restore import RestoreManager
from offline_restore.model import Entry

mgr = RestoreManager("/var/lib/restore-workdir")

entries = [
    Entry(source="/backup/a", target="/var/db/a.db", size=..., sha256="..."),
    Entry(source="/backup/b", target="/var/db/b.db", size=..., sha256="..."),
]

# 成功返回表示整组恢复完成；任何源校验失败都不会改动目标
result = mgr.restore("request-001", entries)

# 重启后先收尾所有被中断的请求（不需要源文件仍然存在）
finished = mgr.recover()

# 查询持久化状态：succeeded / rolled_back / failed，以及进行中状态
info = mgr.inspect("request-001")
```

`request_id` 绑定**目标路径 + 长度 + SHA-256**（不含 source）：
- 同一 ID 携带不同目标/长度/哈希会在改动任何目标前抛 `ConflictError`。
- 成功/回退/失败的终态请求重试只返回既有结果：不读源文件、不写目标。即使
  之后另一个请求已恢复过相同目标，旧请求重试也不会覆盖新内容。

## 崩溃安全协议

SQLite（WAL + `synchronous=FULL`）是唯一的持久化决策记录；每请求暂存位于
`<workdir>/requests/<id>/`。

1. **校验阶段**：流式核对每个源文件的长度与 SHA-256（固定 1 MiB 缓冲，额外
   内存不随备份总字节数增长）。全部通过前不创建元数据、不触碰目标。
2. `staging`：新内容以**硬链接**暂存（跨文件系统时退回流式拷贝），随后写入
   `applying` 决策。关键顺序是：**任何目标改名之前，`applying` 决策已落盘**，
   所以 `staging` 下目标字节与“是否存在”完全未变。
3. `applying`：逐个用 `os.replace` 原子替换固定目标，并以 append-only 的
   `applied.marker` 记录已完成项。崩溃只能**向前**收敛到完整新状态。
4. `staging`/`rolling_back` 下崩溃则记录 `rolled_back`——因为目标从未被改动，
   回退即“保持原状态”（含原本不存在的目标）。
5. `succeeded`/`rolled_back` 以独立事务提交（在返回调用方、清理暂存之前），
   因此已宣布成功的恢复在重启后绝不会回退。
6. 暂存按文件逐个删除且可重复执行；清理途中被 kill，下次继续清理。事务全部
   收尾后 `<workdir>/requests/<id>/` 不残留任何数据。

因此，对任何被中断的请求，`recover()` 收敛后整组目标要么是 restore 前的完整
旧状态，要么是清单规定的完整新状态，二者必居其一，且持久化结果与文件实际
状态一致；`recover()` 自身在任意位置再次被终止，后续调用仍收敛到同一状态。

## 工作目录布局

```
<workdir>/
  state.db                     # 状态机与清单身份（SQLite）
  requests/<id>/
    new/<idx>-<basename>       # 暂存的新内容（硬链接或流式拷贝）
    applied.marker             # 已原子替换的条目索引
```

## 确定性停顿点与测试

`RestoreManager(workdir, pause_hook=...)` 暴露确定性事件；子进程入口
`python -m offline_restore.worker` 通过环境变量在指定事件阻塞（见
`offline_restore/worker.py` 头部说明），测试等待命中文件后用真实 **SIGKILL**
杀掉子进程，而不是在进程内抛异常或正常退出。

```bash
python3 -m pytest
```

测试覆盖（均为真实子进程强杀）：

- 最后一个源文件校验失败：所有已有目标字节不变、原本不存在的目标仍不存在，
  且无暂存/元数据残留。
- 一组目标同时含已有与新文件：分别在“仅部分目标被替换”和“全部目标已替换但
  restore 尚未返回”时杀进程；删除源文件后，在 recover 推进或清理途中再杀
  一次；最终检查整组状态、请求结果与暂存清理。
- 请求 A 成功后请求 B 恢复相同目标；重启后重试 A，目标仍为 B 的内容；复用 A
  的 ID 提交不同哈希/目标在改动前被拒绝。
- 额外：暂存阶段被杀的回退路径，以及 7×7 个“任意恢复/清理停顿点反复强杀”
  参数化组合，全部要求原子收敛且无暂存残留。
