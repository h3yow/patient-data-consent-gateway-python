"""领域模型与类型定义。

字段分类（用于最小必要范围控制）：
- identity: 身份标识字段（敏感，返回前需去标识化）
- contact: 联系方式字段（敏感，返回前需去标识化）
- profile: 人口学资料字段（非敏感）
- medical: 病历字段（非敏感）
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


# ---- 枚举 ----------------------------------------------------------------


class Purpose(str, Enum):
    """共享用途。"""

    TREATMENT = "treatment"
    RESEARCH = "research"
    PAYMENT = "payment"
    MARKETING = "marketing"


class FieldCategory(str, Enum):
    """字段范围类别（最小必要粒度）。"""

    IDENTITY = "identity"
    CONTACT = "contact"
    PROFILE = "profile"
    MEDICAL = "medical"


class DenialReason(str, Enum):
    """可区分的拒绝原因。"""

    CONSENT_NOT_FOUND = "consent_not_found"
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_EXPIRED = "consent_expired"
    PURPOSE_MISMATCH = "purpose_mismatch"
    RECIPIENT_MISMATCH = "recipient_mismatch"
    SCOPE_EXCEEDED = "scope_exceeded"
    AUDIT_WRITE_FAILED = "audit_write_failed"


class AuditEventType(str, Enum):
    """审计事件类型。"""

    CONSENT_GRANTED = "consent_granted"
    CONSENT_REVOKED = "consent_revoked"
    ACCESS_GRANTED = "access_granted"
    ACCESS_DENIED = "access_denied"


# ---- 实体 ----------------------------------------------------------------


class Consent(BaseModel):
    """同意授权实体。"""

    consent_id: str
    patient_id: str
    recipient_id: str
    purpose: Purpose
    allowed_fields: list[FieldCategory]
    granted_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    active: bool = True


class Patient(BaseModel):
    """患者档案。"""

    patient_id: str
    name: str
    id_card: str
    phone: str
    email: str
    gender: str
    birth_date: str
    diagnosis: str
    medications: list[str]


class Recipient(BaseModel):
    """数据接收方。"""

    recipient_id: str
    name: str


class AuditEvent(BaseModel):
    """可追溯审计事件（不得包含原始敏感值）。"""

    event_id: str
    event_type: AuditEventType
    timestamp: datetime
    patient_id: str
    recipient_id: str
    purpose: Purpose | None = None
    consent_id: str | None = None
    requested_fields: list[FieldCategory] | None = None
    allowed_fields: list[FieldCategory] | None = None
    returned_fields: list[str] | None = None
    masked_fields: list[str] | None = None
    denial_reason: DenialReason | None = None
    detail: str | None = None


# ---- 请求 / 响应 DTO ------------------------------------------------------


class GrantConsentRequest(BaseModel):
    patient_id: str
    recipient_id: str
    purpose: Purpose
    allowed_fields: list[FieldCategory]
    expires_at: datetime | None = None
    now: datetime | None = None


class RevokeConsentRequest(BaseModel):
    consent_id: str
    now: datetime | None = None


class AccessRequest(BaseModel):
    patient_id: str
    recipient_id: str
    purpose: Purpose
    requested_fields: list[FieldCategory]
    now: datetime | None = None


class DecisionResponse(BaseModel):
    allowed: bool
    consent_id: str | None = None
    allowed_fields: list[FieldCategory] = Field(default_factory=list)
    denial_reason: DenialReason | None = None


class DataAccessResponse(BaseModel):
    allowed: bool
    consent_id: str | None = None
    data: dict | None = None
    returned_fields: list[str] = Field(default_factory=list)
    masked_fields: list[str] = Field(default_factory=list)
    denial_reason: DenialReason | None = None


class PatientCreate(BaseModel):
    patient_id: str
    name: str
    id_card: str
    phone: str
    email: str
    gender: str
    birth_date: str
    diagnosis: str
    medications: list[str]


class RecipientCreate(BaseModel):
    recipient_id: str
    name: str
