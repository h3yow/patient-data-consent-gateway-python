"""领域类型与对外接口的数据模型。"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel


class Purpose(str, Enum):
    TREATMENT = "treatment"
    RESEARCH = "research"
    MARKETING = "marketing"
    PAYMENT = "payment"


class ConsentStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


# 患者数据字段名（返回结果的 key 与请求范围使用同一套名称）
FIELD_FULL_NAME = "full_name"
FIELD_PHONE = "phone"
FIELD_ID_CARD = "id_card"
FIELD_EMAIL = "email"
FIELD_ADDRESS = "address"
FIELD_DATE_OF_BIRTH = "date_of_birth"
FIELD_DIAGNOSIS = "diagnosis"
FIELD_MEDICATIONS = "medications"
FIELD_BLOOD_TYPE = "blood_type"

SENSITIVE_FIELDS = frozenset(
    {
        FIELD_FULL_NAME,
        FIELD_PHONE,
        FIELD_ID_CARD,
        FIELD_EMAIL,
        FIELD_ADDRESS,
        FIELD_DATE_OF_BIRTH,
    }
)
ALL_FIELDS = frozenset(
    SENSITIVE_FIELDS
    | {FIELD_DIAGNOSIS, FIELD_MEDICATIONS, FIELD_BLOOD_TYPE}
)


class PatientRecord(BaseModel):
    patient_id: str
    full_name: str
    phone: str
    id_card: str
    email: str
    address: str
    date_of_birth: date
    diagnosis: str
    medications: list[str]
    blood_type: str


class GrantConsentRequest(BaseModel):
    patient_id: str
    recipient_id: str
    purpose: Purpose
    fields: list[str]
    expires_at: datetime | None = None


class RevokeConsentRequest(BaseModel):
    consent_id: str
    recipient_id: str
    reason: str | None = None


class AccessRequest(BaseModel):
    patient_id: str
    recipient_id: str
    purpose: Purpose
    fields: list[str]


class ConsentView(BaseModel):
    consent_id: str
    patient_id: str
    recipient_id: str
    purpose: Purpose
    fields: list[str]
    granted_at: datetime
    expires_at: datetime | None
    status: ConsentStatus
    revoked_at: datetime | None = None
    revoke_reason: str | None = None


class AuditEntry(BaseModel):
    seq: int
    timestamp: datetime
    action: str  # consent_granted / consent_revoked / access_allowed / access_denied
    patient_id: str
    recipient_id: str
    purpose: Purpose | None = None
    consent_id: str | None = None
    requested_fields: list[str] = []
    authorized_fields: list[str] = []
    masked_fields: list[str] = []
    # 仅保存去标识化后的结果，审计记录中永不出现原始敏感值
    masked_values: dict[str, str] = {}
    result: str  # allowed / denied
    deny_reason: str | None = None
    detail: str | None = None


class AccessResponse(BaseModel):
    patient_id: str
    consent_id: str
    purpose: Purpose
    recipient_id: str
    fields: dict[str, str | list[str]]
    masked_fields: list[str]


class ErrorResponse(BaseModel):
    error: str
    message: str
