"""确定性停顿点：仅供测试通过环境变量触发，便于真实子进程强杀。"""

import os
import time
from pathlib import Path

ENV_VAR = "RESTORELIB_PAUSE_AT"


def pause_point(workdir, name):
    """若环境变量 RESTORELIB_PAUSE_AT 包含 name，则落就绪标记后长时间睡眠。"""
    wanted = os.environ.get(ENV_VAR, "")
    if not wanted:
        return
    if name not in [item.strip() for item in wanted.split(",")]:
        return
    ready = Path(workdir) / (".pause-ready-" + name)
    ready.touch()
    time.sleep(3600)


def ready_file(workdir, name):
    return Path(workdir) / (".pause-ready-" + name)
