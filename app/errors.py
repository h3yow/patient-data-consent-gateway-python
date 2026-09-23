"""业务异常与拒绝原因映射。"""

from __future__ import annotations

from .models import DenialReason


class GatewayError(Exception):
    """网关门控异常基类。"""

    def __init__(self, reason: DenialReason, message: str) -> None:
        self.reason = reason
        self.message = message
        super().__init__(message)
