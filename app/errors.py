"""业务异常与拒绝原因映射。"""

from __future__ import annotations

from .models import DenialReason


class GatewayError(Exception):
    """网关门控异常基类。"""

    def __init__(self, reason: DenialReason, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)


class ConsentStateError(Exception):
    """同意状态变更不合法（如借收窄扩大范围、暂停已撤销同意）。

    与门控拒绝区分：这是调用方用法错误，映射 HTTP 400，
    响应体携带可区分的 error 码。
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)
