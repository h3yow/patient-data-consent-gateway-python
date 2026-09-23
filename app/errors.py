"""可区分的网关错误类型。"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    PATIENT_NOT_FOUND = "PATIENT_NOT_FOUND"
    CONSENT_NOT_FOUND = "CONSENT_NOT_FOUND"
    CONSENT_REVOKED = "CONSENT_REVOKED"
    CONSENT_EXPIRED = "CONSENT_EXPIRED"
    PURPOSE_MISMATCH = "PURPOSE_MISMATCH"
    RECIPIENT_MISMATCH = "RECIPIENT_MISMATCH"
    SCOPE_EXCEEDED = "SCOPE_EXCEEDED"
    AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONFLICT = "CONFLICT"


class GatewayError(Exception):
    """网关门控异常，携带稳定的机器可读错误码与安全（已脱敏）消息。"""

    def __init__(self, code: ErrorCode, message: str, status_code: int = 403) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
