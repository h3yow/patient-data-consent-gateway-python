"""审计记录服务：可重放记录授权、撤销、访问成败与去标识化结果。

审计事件禁止包含原始敏感字段值，只记录字段名、字段类别与脱敏方式。
审计不可写入时抛出 AUDIT_WRITE_FAILED，调用方必须失败关闭。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .errors import GatewayError
from .models import AuditEvent, DenialReason
from .storage import Store

logger = logging.getLogger("consent_gateway.audit")


class AuditLogger:
    def __init__(self, store: Store) -> None:
        self.store = store

    def record(self, event: AuditEvent) -> None:
        """写入一条审计事件；写入失败时抛 GatewayError(AUDIT_WRITE_FAILED)。"""
        try:
            self.store.append_audit(event)
        except GatewayError:
            raise
        except Exception as exc:  # 存储层任何故障都转为可区分原因
            logger.error("audit_write_failed event_type=%s", event.event_type.value)
            raise GatewayError(
                DenialReason.AUDIT_WRITE_FAILED,
                "审计记录无法写入，操作已失败关闭",
            ) from exc

    def build_event(self, **kwargs: object) -> AuditEvent:
        """构造审计事件，自动补齐 event_id 与时间戳。"""
        event_id = kwargs.pop("event_id", None) or f"evt_{uuid.uuid4().hex[:12]}"
        timestamp = kwargs.pop("timestamp", None) or datetime.now(timezone.utc)
        return AuditEvent(event_id=event_id, timestamp=timestamp, **kwargs)  # type: ignore[arg-type]
