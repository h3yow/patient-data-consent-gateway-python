"""数据访问服务：决策 -> 审计 -> 投影/脱敏 -> 返回。

关键不变式：
- 被拒绝时 data 为 None，绝不返回任何受保护数据；
- 每次访问（无论成败）都先写审计，审计写不进去则失败关闭，
  返回 AUDIT_WRITE_FAILED 且不返回数据；
- 审计事件只含字段名/类别与脱敏标记，不含原始敏感值。
"""

from __future__ import annotations

import logging

from .audit import AuditLogger
from .consent import ConsentService, _as_aware
from .deidentify import project_patient
from .errors import GatewayError
from .models import (
    AccessRequest,
    AuditEventType,
    DataAccessResponse,
    DenialReason,
)
from .storage import Store

logger = logging.getLogger("consent_gateway.access")


class AccessService:
    def __init__(self, store: Store, audit: AuditLogger | None = None) -> None:
        self.store = store
        self.audit = audit or AuditLogger(store)
        self.consents = ConsentService(store, self.audit)

    def access_data(self, req: AccessRequest) -> DataAccessResponse:
        now = _as_aware(req.now)

        patient = self.store.get_patient(req.patient_id)
        if patient is None:
            # 患者不存在按“未授权”处理，且不泄露任何数据
            decision = self._deny_with_audit(
                req, now, DenialReason.CONSENT_NOT_FOUND
            )
            return DataAccessResponse(
                allowed=False, denial_reason=decision
            )

        decision = self.consents.evaluate(
            patient_id=req.patient_id,
            recipient_id=req.recipient_id,
            purpose=req.purpose,
            requested_fields=list(req.requested_fields),
            now=now,
        )

        if not decision.allowed:
            # 审计先落盘；若审计本身失败，这里抛出 AUDIT_WRITE_FAILED（失败关闭）
            self._record_denied(req, now, decision.denial_reason, decision.consent_id)
            logger.info(
                "access_denied patient=%s recipient=%s purpose=%s requested=%s reason=%s",
                req.patient_id,
                req.recipient_id,
                req.purpose.value,
                [f.value for f in req.requested_fields],
                decision.denial_reason.value if decision.denial_reason else "unknown",
            )
            return DataAccessResponse(
                allowed=False,
                consent_id=decision.consent_id,
                denial_reason=decision.denial_reason,
            )

        # 授权通过：按请求范围投影（敏感字段在 project_patient 内脱敏）
        data, returned_fields, masked_fields = project_patient(
            patient, decision.allowed_fields
        )

        # 成功访问也必须先审计（含去标识化结果：仅字段名/类别，无原值）
        self._record_granted(
            req,
            now,
            decision.consent_id,
            returned_fields,
            masked_fields,
        )

        logger.info(
            "access_granted patient=%s recipient=%s purpose=%s returned=%s masked=%s",
            req.patient_id,
            req.recipient_id,
            req.purpose.value,
            returned_fields,
            masked_fields,
        )
        return DataAccessResponse(
            allowed=True,
            consent_id=decision.consent_id,
            data=data,
            returned_fields=returned_fields,
            masked_fields=masked_fields,
        )

    # ---- 审计构造 --------------------------------------------------------

    def _record_denied(
        self,
        req: AccessRequest,
        now,
        reason: DenialReason | None,
        consent_id: str | None,
    ) -> None:
        event = self.audit.build_event(
            event_type=AuditEventType.ACCESS_DENIED,
            timestamp=now,
            patient_id=req.patient_id,
            recipient_id=req.recipient_id,
            purpose=req.purpose,
            consent_id=consent_id,
            requested_fields=list(req.requested_fields),
            denial_reason=reason,
            detail="access denied; no protected data returned",
        )
        self.audit.record(event)

    def _record_granted(
        self,
        req: AccessRequest,
        now,
        consent_id: str | None,
        returned_fields: list[str],
        masked_fields: list[str],
    ) -> None:
        event = self.audit.build_event(
            event_type=AuditEventType.ACCESS_GRANTED,
            timestamp=now,
            patient_id=req.patient_id,
            recipient_id=req.recipient_id,
            purpose=req.purpose,
            consent_id=consent_id,
            requested_fields=list(req.requested_fields),
            returned_fields=returned_fields,
            masked_fields=masked_fields,
            detail="access granted; sensitive fields masked before return",
        )
        self.audit.record(event)

    def _deny_with_audit(
        self, req: AccessRequest, now, reason: DenialReason
    ) -> DenialReason:
        self._record_denied(req, now, reason, None)
        return reason
