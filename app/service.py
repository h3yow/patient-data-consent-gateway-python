"""同意与访问网关的核心决策逻辑。

关键不变量：
- 决策（含状态判定）与审计追加写在同一把存储锁内线性化，撤销对后续访问立即生效；
- 审计无法写入时，授予/撤销/访问一律失败关闭（AUDIT_WRITE_FAILED）；
- 任何离开本模块的数据都只含授权字段，敏感字段已去标识化，审计中也只有脱敏值。
"""
from __future__ import annotations

import logging
import uuid

from app.audit import AuditLog, AuditUnavailableError
from app.clock import Clock
from app.deidentification import is_sensitive, mask_value
from app.errors import ErrorCode, GatewayError
from app.models import (
    ALL_FIELDS,
    AccessRequest,
    AccessResponse,
    ConsentStatus,
    ConsentView,
    GrantConsentRequest,
    PatientRecord,
    RevokeConsentRequest,
)
from app.storage import Storage

logger = logging.getLogger("consent-gateway")


class ConsentGateway:
    def __init__(self, storage: Storage, audit: AuditLog, clock: Clock) -> None:
        self._storage = storage
        self._audit = audit
        self._clock = clock

    # ---- 基础工具 -------------------------------------------------------

    @staticmethod
    def _validate_fields(fields: list[str]) -> None:
        if not fields:
            raise GatewayError(ErrorCode.INVALID_REQUEST, "fields must not be empty", 400)
        unknown = sorted(set(fields) - ALL_FIELDS)
        if unknown:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, f"unknown field names: {unknown}", 400
            )

    def _effective_status(self, consent: ConsentView) -> ConsentStatus:
        """惰性把已到期同意落为 EXPIRED，保证列表与判定一致、可解释。"""
        now = self._clock.now()
        if (
            consent.status == ConsentStatus.ACTIVE
            and consent.expires_at is not None
            and consent.expires_at <= now
        ):
            consent.status = ConsentStatus.EXPIRED
            self._storage.update_consent(consent)
        return consent.status

    # ---- 患者与同意管理 -------------------------------------------------

    def seed_patient(self, patient: PatientRecord) -> PatientRecord:
        with self._storage.lock:
            if self._storage.get_patient(patient.patient_id) is not None:
                raise GatewayError(
                    ErrorCode.CONFLICT, f"patient already exists: {patient.patient_id}", 409
                )
            self._storage.put_patient(patient)
        return patient

    def grant_consent(self, req: GrantConsentRequest) -> ConsentView:
        self._validate_fields(req.fields)
        now = self._clock.now()
        if req.expires_at is not None and req.expires_at <= now:
            raise GatewayError(
                ErrorCode.INVALID_REQUEST, "expires_at must be in the future", 400
            )
        if self._storage.get_patient(req.patient_id) is None:
            raise GatewayError(ErrorCode.PATIENT_NOT_FOUND, "patient not found", 404)

        consent = ConsentView(
            consent_id=f"consent-{uuid.uuid4().hex[:12]}",
            patient_id=req.patient_id,
            recipient_id=req.recipient_id,
            purpose=req.purpose,
            fields=sorted(set(req.fields)),
            granted_at=now,
            expires_at=req.expires_at,
            status=ConsentStatus.ACTIVE,
        )
        with self._storage.lock:
            try:
                # 先写审计再落库；审计不可用时不产生任何授权
                self._audit.record_consent_granted(
                    patient_id=consent.patient_id,
                    recipient_id=consent.recipient_id,
                    purpose=consent.purpose,
                    consent_id=consent.consent_id,
                    fields=consent.fields,
                )
            except AuditUnavailableError as exc:
                raise GatewayError(
                    ErrorCode.AUDIT_WRITE_FAILED,
                    "consent grant failed closed: audit log is not writable",
                    500,
                ) from exc
            self._storage.put_consent(consent)
        return consent

    def revoke_consent(self, req: RevokeConsentRequest) -> ConsentView:
        with self._storage.lock:
            consent = self._storage.get_consent(req.consent_id)
            if consent is None or consent.recipient_id != req.recipient_id:
                # 不向调用方区分“不存在”与“不属于该接收方”，避免探测授权 ID
                raise GatewayError(ErrorCode.CONSENT_NOT_FOUND, "consent not found", 404)
            if consent.status == ConsentStatus.REVOKED:
                raise GatewayError(ErrorCode.CONFLICT, "consent already revoked", 409)
            self._effective_status(consent)
            if consent.status == ConsentStatus.EXPIRED:
                raise GatewayError(ErrorCode.CONFLICT, "consent already expired", 409)
            try:
                self._audit.record_consent_revoked(
                    patient_id=consent.patient_id,
                    recipient_id=consent.recipient_id,
                    consent_id=consent.consent_id,
                    reason=req.reason,
                )
            except AuditUnavailableError as exc:
                raise GatewayError(
                    ErrorCode.AUDIT_WRITE_FAILED,
                    "consent revoke failed closed: audit log is not writable",
                    500,
                ) from exc
            logger.info(
                "同意撤销 consent=%s patient=%s recipient=%s",
                consent.consent_id, consent.patient_id, consent.recipient_id,
            )
            consent.status = ConsentStatus.REVOKED
            consent.revoked_at = self._clock.now()
            consent.revoke_reason = "redacted" if req.reason else None
            self._storage.update_consent(consent)
            return consent

    def list_consents(self, patient_id: str) -> list[ConsentView]:
        with self._storage.lock:
            if self._storage.get_patient(patient_id) is None:
                raise GatewayError(ErrorCode.PATIENT_NOT_FOUND, "patient not found", 404)
            consents = self._storage.list_consents(patient_id)
            for consent in consents:
                self._effective_status(consent)
            return consents

    # ---- 访问决策 -------------------------------------------------------

    def access_patient_data(self, req: AccessRequest) -> AccessResponse:
        self._validate_fields(req.fields)
        requested = sorted(set(req.fields))

        with self._storage.lock:
            patient = self._storage.get_patient(req.patient_id)
            if patient is None:
                self._deny(
                    req, requested, ErrorCode.PATIENT_NOT_FOUND, "patient not found", 404
                )
            decision = self._decide(req)  # 返回 ConsentView 或抛出“拒绝”网关异常
            consent = decision

            # 最小必要：请求字段必须全部落在授权范围内
            out_of_scope = sorted(set(requested) - set(consent.fields))
            if out_of_scope:
                self._deny(
                    req,
                    requested,
                    ErrorCode.SCOPE_EXCEEDED,
                    f"requested fields outside consent scope: {out_of_scope}",
                    403,
                    consent_id=consent.consent_id,
                    authorized_fields=consent.fields,
                )

            # 组装结果：仅请求字段；敏感字段先去标识化
            output: dict[str, str | list[str]] = {}
            masked_values: dict[str, str] = {}
            masked_fields: list[str] = []
            for field in requested:
                raw = getattr(patient, field)
                if is_sensitive(field):
                    masked = mask_value(field, raw)
                    output[field] = masked
                    masked_fields.append(field)
                    masked_values[field] = masked
                else:
                    output[field] = raw

            logger.info(
                "访问允许 patient=%s recipient=%s purpose=%s consent=%s requested=%s "
                "authorized=%s masked=%s",
                req.patient_id, req.recipient_id, req.purpose.value,
                consent.consent_id, requested, consent.fields, masked_fields,
            )
            try:
                # 数据返回前必须完成审计落盘；失败则失败关闭，不返回任何数据
                self._audit.record_access(
                    patient_id=req.patient_id,
                    recipient_id=req.recipient_id,
                    purpose=req.purpose,
                    requested_fields=requested,
                    result="allowed",
                    consent_id=consent.consent_id,
                    authorized_fields=consent.fields,
                    masked_fields=masked_fields,
                    masked_values=masked_values,
                )
            except AuditUnavailableError as exc:
                raise GatewayError(
                    ErrorCode.AUDIT_WRITE_FAILED,
                    "access failed closed: audit log is not writable",
                    500,
                ) from exc

            return AccessResponse(
                patient_id=req.patient_id,
                consent_id=consent.consent_id,
                purpose=req.purpose,
                recipient_id=req.recipient_id,
                fields=output,
                masked_fields=masked_fields,
            )

    def _decide(self, req: AccessRequest) -> ConsentView:
        """在存储锁内判定 (patient, recipient, purpose) 的授权状态。"""
        all_consents = self._storage.list_consents(req.patient_id)
        for consent in all_consents:
            self._effective_status(consent)

        exact = [
            c
            for c in all_consents
            if c.recipient_id == req.recipient_id and c.purpose == req.purpose
        ]
        if exact:
            consent = max(exact, key=lambda c: c.granted_at)
            if consent.status == ConsentStatus.REVOKED:
                self._deny(
                    req,
                    sorted(set(req.fields)),
                    ErrorCode.CONSENT_REVOKED,
                    "consent has been revoked",
                    403,
                    consent_id=consent.consent_id,
                )
            if consent.status == ConsentStatus.EXPIRED:
                self._deny(
                    req,
                    sorted(set(req.fields)),
                    ErrorCode.CONSENT_EXPIRED,
                    "consent has expired",
                    403,
                    consent_id=consent.consent_id,
                )
            return consent

        # 无精确匹配时，按固定优先级给出可区分原因：
        # 1) 该用途已授权给其他接收方 -> RECIPIENT_MISMATCH
        # 2) 该接收方有其他用途授权 -> PURPOSE_MISMATCH
        # 3) 否则 -> CONSENT_NOT_FOUND
        if any(c.purpose == req.purpose for c in all_consents):
            self._deny(
                req, sorted(set(req.fields)), ErrorCode.RECIPIENT_MISMATCH,
                "no consent for this recipient under the requested purpose", 403,
            )
        if any(c.recipient_id == req.recipient_id for c in all_consents):
            self._deny(
                req, sorted(set(req.fields)), ErrorCode.PURPOSE_MISMATCH,
                "no consent for the requested purpose under this recipient", 403,
            )
        self._deny(
            req, sorted(set(req.fields)), ErrorCode.CONSENT_NOT_FOUND,
            "no consent found for patient/recipient/purpose", 404,
        )

    def _deny(
        self,
        req: AccessRequest,
        requested_fields: list[str],
        code: ErrorCode,
        message: str,
        status_code: int,
        *,
        consent_id: str | None = None,
        authorized_fields: list[str] | None = None,
    ) -> None:
        """记录拒绝审计后抛出网关异常；审计不可用时失败关闭。"""
        try:
            self._audit.record_access(
                patient_id=req.patient_id,
                recipient_id=req.recipient_id,
                purpose=req.purpose,
                requested_fields=requested_fields,
                result="denied",
                consent_id=consent_id,
                authorized_fields=authorized_fields,
                deny_reason=code.value,
            )
        except AuditUnavailableError as exc:
            raise GatewayError(
                ErrorCode.AUDIT_WRITE_FAILED,
                "access failed closed: audit log is not writable",
                500,
            ) from exc
        raise GatewayError(code, message, status_code)
