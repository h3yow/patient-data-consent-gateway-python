"""同意授权服务：授予、撤销、收窄、暂停、恢复与访问决策。

评估固定顺序（决定拒绝原因的优先级，保证结论可解释）：
未授权 -> 已撤销 -> 已暂停 -> 已过期 -> 用途不匹配 -> 接收方不匹配 -> 超出范围。

所有改变同意状态的写操作（授予/撤销/收窄/暂停/恢复）都遵循同一原子提交规则：
审计先落盘、状态后提交；审计写不进去则整体失败关闭，不留半生效状态。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .audit import AuditLogger
from .errors import ConsentStateError, GatewayError
from .models import (
    AuditEvent,
    AuditEventType,
    Consent,
    DecisionResponse,
    DenialReason,
    FieldCategory,
    GrantConsentRequest,
    NarrowConsentRequest,
    ResumeConsentRequest,
    RevokeConsentRequest,
    SuspendConsentRequest,
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

    # ---- 授予 ------------------------------------------------------------

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
            suspended=False,
        )
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
        with self.store.lock:
            # 原子提交：审计先落盘，随后同意才生效；审计失败则同意不存在
            self._commit(consent, event)
        logger.info(
            "consent_granted consent_id=%s patient=%s recipient=%s purpose=%s fields=%s",
            consent.consent_id,
            consent.patient_id,
            consent.recipient_id,
            consent.purpose.value,
            [f.value for f in consent.allowed_fields],
        )
        return consent

    # ---- 撤销 ------------------------------------------------------------

    def revoke(self, req: RevokeConsentRequest) -> Consent:
        now = _as_aware(req.now)
        with self.store.lock:
            current = self._require_consent(req.consent_id)
            if not current.active:
                # 撤销幂等：重复撤销返回同一结论，不重复落审计
                return current
            # 在副本上变更：审计失败时存储内原对象保持撤销前状态
            consent = current.model_copy(deep=True)
            consent.active = False
            consent.revoked_at = now
            consent.suspended = False
            event = self.audit.build_event(
                event_type=AuditEventType.CONSENT_REVOKED,
                timestamp=now,
                patient_id=consent.patient_id,
                recipient_id=consent.recipient_id,
                purpose=consent.purpose,
                consent_id=consent.consent_id,
                detail="consent revoked",
            )
            # 审计先落盘、撤销状态后提交：失败则同意仍处于撤销前状态
            self._commit(consent, event)
        logger.info(
            "consent_revoked consent_id=%s patient=%s recipient=%s",
            consent.consent_id,
            consent.patient_id,
            consent.recipient_id,
        )
        return consent

    # ---- 收窄 ------------------------------------------------------------

    def narrow(self, req: NarrowConsentRequest) -> Consent:
        now = _as_aware(req.now)
        with self.store.lock:
            current = self._require_consent(req.consent_id)
            self._require_not_revoked(current)
            before = list(current.allowed_fields)
            wanted = list(dict.fromkeys(req.allowed_fields))
            current_set = set(before)
            # 只减不增：新范围不得包含当前范围之外的类别
            if any(f not in current_set for f in wanted):
                raise ConsentStateError(
                    "scope_widening_rejected",
                    "收窄只能移除字段类别；需要更多字段时请重新授予一条新同意",
                )
            # 且必须严格变小（至少移除一个类别）
            if len(set(wanted)) == len(current_set):
                raise ConsentStateError(
                    "scope_unchanged",
                    "收窄后的范围必须严格小于当前范围（至少移除一个类别）",
                )
            # 在副本上变更：审计失败时存储内原对象的范围保持不变
            consent = current.model_copy(deep=True)
            kept = set(wanted)
            consent.allowed_fields = [f for f in before if f in kept]
            event = self.audit.build_event(
                event_type=AuditEventType.CONSENT_SCOPE_NARROWED,
                timestamp=now,
                patient_id=consent.patient_id,
                recipient_id=consent.recipient_id,
                purpose=consent.purpose,
                consent_id=consent.consent_id,
                allowed_fields=list(consent.allowed_fields),
                previous_fields=before,
                detail="consent scope narrowed",
            )
            # 审计先落盘、范围后提交：失败则范围保持收窄前
            self._commit(consent, event)
        logger.info(
            "consent_scope_narrowed consent_id=%s before=%s after=%s",
            consent.consent_id,
            [f.value for f in before],
            [f.value for f in consent.allowed_fields],
        )
        return consent

    # ---- 暂停 / 恢复 -----------------------------------------------------

    def suspend(self, req: SuspendConsentRequest) -> Consent:
        now = _as_aware(req.now)
        with self.store.lock:
            current = self._require_consent(req.consent_id)
            self._require_not_revoked(current)
            if current.suspended:
                # 暂停幂等：重复暂停不重复落审计
                return current
            consent = current.model_copy(deep=True)
            consent.suspended = True
            consent.suspended_at = now
            event = self.audit.build_event(
                event_type=AuditEventType.CONSENT_SUSPENDED,
                timestamp=now,
                patient_id=consent.patient_id,
                recipient_id=consent.recipient_id,
                purpose=consent.purpose,
                consent_id=consent.consent_id,
                allowed_fields=list(consent.allowed_fields),
                detail="consent suspended",
            )
            self._commit(consent, event)
        logger.info("consent_suspended consent_id=%s", consent.consent_id)
        return consent

    def resume(self, req: ResumeConsentRequest) -> Consent:
        now = _as_aware(req.now)
        with self.store.lock:
            current = self._require_consent(req.consent_id)
            # 已撤销的同意不能恢复
            if not current.active:
                raise ConsentStateError(
                    "consent_revoked",
                    "已撤销的同意不能恢复；需要继续共享请重新授予",
                )
            if not current.suspended:
                # 恢复仅对暂停中的同一生效；未暂停时为幂等空操作
                return current
            consent = current.model_copy(deep=True)
            consent.suspended = False
            consent.resumed_at = now
            event = self.audit.build_event(
                event_type=AuditEventType.CONSENT_RESUMED,
                timestamp=now,
                patient_id=consent.patient_id,
                recipient_id=consent.recipient_id,
                purpose=consent.purpose,
                consent_id=consent.consent_id,
                allowed_fields=list(consent.allowed_fields),
                detail="consent resumed",
            )
            self._commit(consent, event)
        logger.info("consent_resumed consent_id=%s", consent.consent_id)
        return consent

    # ---- 评估 ------------------------------------------------------------

    def evaluate(
        self,
        patient_id: str,
        recipient_id: str,
        purpose: object,
        requested_fields: list[FieldCategory],
        now: datetime | None = None,
    ) -> DecisionResponse:
        """按固定顺序评估访问请求，返回纯决策结果（不写审计）。

        调用方（AccessService）在存储锁内调用本方法，保证与状态变更互斥：
        每个访问看到的要么全是变更前状态、要么全是变更后状态。
        """
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

        # 3) 已暂停（暂停不是撤销，也不改变有效期；恢复后同一条继续生效）
        if consent.suspended:
            logger.info(
                "access_denied consent_id=%s reason=%s",
                consent.consent_id,
                DenialReason.CONSENT_SUSPENDED.value,
            )
            return DecisionResponse(
                allowed=False,
                consent_id=consent.consent_id,
                denial_reason=DenialReason.CONSENT_SUSPENDED,
            )

        # 4) 已过期（暂停期间有效期照常流逝；过期同意即使恢复也仍按过期处理）
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

        # 5/6) 用途、接收方匹配（find_consents 已保证；保留防御性校验）
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

        # 7) 范围：任何被请求类别不在授权内，整体拒绝（不返回任何受保护数据）
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

    # ---- 内部辅助 --------------------------------------------------------

    def _commit(self, consent: Consent, event: AuditEvent) -> None:
        """审计先落盘、同意状态后提交的原子写（须在 store.lock 内调用）。

        审计写失败时 record 抛 AUDIT_WRITE_FAILED，save_consent 不会执行，
        从而保证失败前后同一访问请求拿到的结论完全一致。
        """
        self.audit.record(event)
        self.store.save_consent(consent)

    def _require_consent(self, consent_id: str) -> Consent:
        consent = self.store.get_consent(consent_id)
        if consent is None:
            raise GatewayError(
                DenialReason.CONSENT_NOT_FOUND,
                f"同意不存在: {consent_id}",
            )
        return consent

    @staticmethod
    def _require_not_revoked(consent: Consent) -> None:
        if not consent.active:
            raise ConsentStateError(
                "consent_revoked",
                "已撤销的同意不能再变更；需要继续共享请重新授予",
            )
