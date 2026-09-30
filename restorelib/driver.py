"""子进程驱动入口：测试用真实进程强杀覆盖中断。

用法：
    python -m restorelib.driver <workdir> restore <request_id> <entries.json>
    python -m restorelib.driver <workdir> recover
    python -m restorelib.driver <workdir> inspect <request_id>
"""

import json
import sys

from .core import RestoreLibrary
from .errors import RestoreError


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    workdir, op, rest = args[0], args[1], args[2:]
    lib = RestoreLibrary(workdir)
    try:
        if op == "restore":
            request_id, entries_path = rest
            with open(entries_path, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
            result = lib.restore(request_id, entries)
            print(json.dumps({"outcome": result.outcome, "targets": result.targets}))
        elif op == "recover":
            lib.recover()
            print(json.dumps({"outcome": "recovered"}))
        elif op == "inspect":
            (request_id,) = rest
            print(json.dumps(lib.inspect(request_id)))
        else:
            print("unknown op: %s" % op, file=sys.stderr)
            return 2
    except RestoreError as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
