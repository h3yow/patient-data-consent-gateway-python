"""同意授权服务：授予、撤销与访问决策。

评估固定顺序（决定拒绝原因的优先级，保证结论可解释）：
未授权 -> 已撤销 -> 已过期 -> 用途不匹配 -> 接收方不匹配 -> 超出范围。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .audit import AuditLogger
from .errors import GatewayError
from .models import (
    AuditEventType,
    Consent,
    DecisionResponse,
    DenialReason,
    FieldCategory,
    GrantConsentRequest,
    RevokeConsentRequest,
)
from .storage import Store

logger = logging.getLogger("consent_gateway.consent")


def _as_aware(ts: datetime | None) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


class ConsentService:
    def __init__(self, store: Store, audit: AuditLogger | None = None) -> None:
        self.store = store
        self.audit = audit or AuditLogger(store)

    def grant(self, req: GrantConsentRequest) -> Consent:
        now = _as_aware(req.now)
        consent = Consent(
            consent_id=f"consent_{uuid.uuid4().hex[:12]}",
            patient_id=req.patient_id,
            recipient_id=req.recipient_id,
            purpose=req.purpose,
            allowed_fields=list(dict.fromkeys(req.allowed_fields)),
            granted_at=now,
            expires_at=_as_aware(req.expires_at) if req.expires_at else None,
            revoked_at=None,
            active=True,
        )
        self.store.save_consent(consent)
        # 审计记录只含字段类别，不含任何患者敏感原值
        event = self.audit.build_event(
            event_type=AuditEventType.CONSENT_GRANTED,
            timestamp=now,
            patient_id=consent.patient_id,
            recipient_id=consent.recipient_id,
            purpose=consent.purpose,
            consent_id=consent.consent_id,
            allowed_fields=list(consent.allowed_fields),
            detail="consent granted",
        )
        self.audit.record(event)
        logger.info(
            "consent_granted consent_id=%s patient=%s recipient=%s purpose=%s fields=%s",
            consent.consent_id,
            consent.patient_id,
            consent.recipient_id,
            consent.purpose.value,
            [f.value for f in consent.allowed_fields],
        )
        return consent

    def revoke(self, req: RevokeConsentRequest) -> Consent:
        now = _as_aware(req.now)
        consent = self.store.get_consent(req.consent_id)
        if consent is None:
            raise GatewayError(
                DenialReason.CONSENT_NOT_FOUND,
                f"同意不存在: {req.consent_id}",
            )
        consent.active = False
        consent.revoked_at = now
        self.store.save_consent(consent)
        event = self.audit.build_event(
            event_type=AuditEventType.CONSENT_REVOKED,
            timestamp=now,
            patient_id=consent.patient_id,
            recipient_id=consent.recipient_id,
            purpose=consent.purpose,
            consent_id=consent.consent_id,
            detail="consent revoked",
        )
        # 撤销的审计必须先落盘：审计失败则撤销操作失败关闭
        self.audit.record(event)
        logger.info(
            "consent_revoked consent_id=%s patient=%s recipient=%s",
            consent.consent_id,
            consent.patient_id,
            consent.recipient_id,
        )
        return consent

    def evaluate(
        self,
        patient_id: str,
        recipient_id: str,
        purpose: object,
        requested_fields: list[FieldCategory],
        now: datetime | None = None,
    ) -> DecisionResponse:
        """按固定顺序评估访问请求，返回纯决策结果（不写审计）。"""
        moment = _as_aware(now)
        from .models import Purpose

        purpose_enum = Purpose(purpose)
        requested = list(dict.fromkeys(requested_fields))

        # 1) 未授权：该患者从未授予过任何同意
        all_consents = [
            c
            for c in self.store.find_consents(patient_id, recipient_id, purpose_enum.value)
        ]
        # find_consents 已按 (patient, recipient, purpose) 精确过滤；
        # 为支持跨用途/跨接收方判定，再分别检查“患者对其他用途/接收方”的同意。
        if not all_consents:
            # 判断拒绝子类型（用途优先于接收方，与文档中的固定顺序一致）
            patient_consents = self.store.list_consents_for_patient(patient_id)
            if not patient_consents:
                reason = DenialReason.CONSENT_NOT_FOUND
            elif any(c.purpose == purpose_enum for c in patient_consents):
                # 患者对“同一用途”有授权，但不是给这个接收方的
                reason = DenialReason.RECIPIENT_MISMATCH
            elif any(c.recipient_id == recipient_id for c in patient_consents):
                # 患者给“同一接收方”授过权，但用途不同
                reason = DenialReason.PURPOSE_MISMATCH
            else:
                # 用途与接收方都对不上：按固定顺序先报用途不匹配
                reason = DenialReason.PURPOSE_MISMATCH
            logger.info(
                "access_denied patient=%s recipient=%s purpose=%s reason=%s",
                patient_id,
                recipient_id,
                purpose_enum.value,
                reason.value,
            )
            return DecisionResponse(allowed=False, denial_reason=reason)

        consent = all_consents[0]  # 最近授予的一条

        # 2) 已撤销（立即生效）
        if not consent.active:
            logger.info(
                "access_denied consent_id=%s reason=%s",
                consent.consent_id,
                DenialReason.CONSENT_REVOKED.value,
            )
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.CONSENT_REVOKED,
            )

        # 3) 已过期
        if consent.expires_at is not None and consent.expires_at <= moment:
            logger.info(
                "access_denied consent_id=%s reason=%s expires_at=%s now=%s",
                consent.consent_id,
                DenialReason.CONSENT_EXPIRED.value,
                consent.expires_at.isoformat(),
                moment.isoformat(),
            )
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.CONSENT_EXPIRED,
            )

        # 4/5) 用途、接收方匹配（find_consents 已保证；保留防御性校验）
        if consent.purpose != purpose_enum:
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.PURPOSE_MISMATCH,
            )
        if consent.recipient_id != recipient_id:
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.RECIPIENT_MISMATCH,
            )

        # 6) 范围：任何被请求类别不在授权内，整体拒绝（不返回任何受保护数据）
        granted: set[FieldCategory] = set(consent.allowed_fields)
        if any(field not in granted for field in requested):
            logger.info(
                "access_denied consent_id=%s reason=%s requested=%s granted=%s",
                consent.consent_id,
                DenialReason.SCOPE_EXCEEDED.value,
                [f.value for f in requested],
                [f.value for f in consent.allowed_fields],
            )
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.SCOPE_EXCEEDED,
            )

        logger.info(
            "access_allowed consent_id=%s requested=%s",
            consent.consent_id,
            [f.value for f in requested],
        )
        return DecisionResponse(
            allowed=True,
            consent_id=consent.consent_id,
            allowed_fields=requested,
        )
