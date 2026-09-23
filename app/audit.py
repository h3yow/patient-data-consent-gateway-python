"""可追溯审计记录：追加写、单调序号、可重放；写入失败时调用方必须失败关闭。"""
from __future__ import annotations

import threading

from app.clock import Clock
from app.models import AuditEntry, Purpose


class AuditUnavailableError(Exception):
    """审计存储不可写。"""


class AuditLog:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: list[AuditEntry] = []
        self._failing = False

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()
            self._failing = False

    def set_failing(self, failing: bool) -> None:
        """测试支持：模拟审计无法写入。"""
        with self._lock:
            self._failing = failing

    @property
    def failing(self) -> bool:
        with self._lock:
            return self._failing

    def list_entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)

    def _append(self, **fields) -> AuditEntry:
        # 不在日志/异常中携带任何患者数据；fields 由调用方保证已脱敏
        with self._lock:
            if self._failing:
                raise AuditUnavailableError("audit sink is not writable")
            entry = AuditEntry(seq=len(self._entries) + 1, timestamp=self._clock.now(), **fields)
            self._entries.append(entry)
            return entry

    def record_consent_granted(
        self,
        *,
        patient_id: str,
        recipient_id: str,
        purpose: Purpose,
        consent_id: str,
        fields: list[str],
    ) -> AuditEntry:
        return self._append(
            action="consent_granted",
            patient_id=patient_id,
            recipient_id=recipient_id,
            purpose=purpose,
            consent_id=consent_id,
            authorized_fields=list(fields),
            result="allowed",
        )

    def record_consent_revoked(
        self,
        *,
        patient_id: str,
        recipient_id: str,
        consent_id: str,
        reason: str | None,
    ) -> AuditEntry:
        # revoke reason 是操作者自由文本，可能含敏感信息，故只记录是否提供
        return self._append(
            action="consent_revoked",
            patient_id=patient_id,
            recipient_id=recipient_id,
            consent_id=consent_id,
            result="allowed",
            detail="reason_provided" if reason else "no_reason",
        )

    def record_access(
        self,
        *,
        patient_id: str,
        recipient_id: str,
        purpose: Purpose,
        requested_fields: list[str],
        result: str,
        consent_id: str | None = None,
        authorized_fields: list[str] | None = None,
        masked_fields: list[str] | None = None,
        masked_values: dict[str, str] | None = None,
        deny_reason: str | None = None,
        detail: str | None = None,
    ) -> AuditEntry:
        return self._append(
            action="access_allowed" if result == "allowed" else "access_denied",
            patient_id=patient_id,
            recipient_id=recipient_id,
            purpose=purpose,
            consent_id=consent_id,
            requested_fields=list(requested_fields),
            authorized_fields=list(authorized_fields or []),
            masked_fields=list(masked_fields or []),
            masked_values=dict(masked_values or {}),
            result=result,
            deny_reason=deny_reason,
            detail=detail,
        )
