"""异常类型定义。"""


class RestoreError(Exception):
    """恢复库的基础异常。"""


class VerificationError(RestoreError):
    """源文件长度或 SHA-256 校验失败。"""

    def __init__(self, failures):
        self.failures = list(failures)
        super().__init__("source verification failed: " + "; ".join(self.failures))


class ConflictError(RestoreError):
    """同一 request_id 绑定的目标/长度/哈希与已有记录冲突。"""


class UnknownRequestError(RestoreError):
    """查询的 request_id 不存在。"""
