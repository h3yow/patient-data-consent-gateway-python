"""去标识化：按最小必要范围投影字段并对敏感字段脱敏。

字段类别：
- identity（敏感）: name, id_card
- contact （敏感）: phone, email
- profile （非敏感）: gender, birth_date
- medical （非敏感）: diagnosis, medications

敏感字段即使在授权范围内，返回前也必须掩码处理；
审计中只记录字段名与脱敏标记，不记录原值。
"""

from __future__ import annotations

from .models import FieldCategory, Patient

# 字段名 -> 字段类别
FIELD_CATEGORIES: dict[str, FieldCategory] = {
    "name": FieldCategory.IDENTITY,
    "id_card": FieldCategory.IDENTITY,
    "phone": FieldCategory.CONTACT,
    "email": FieldCategory.CONTACT,
    "gender": FieldCategory.PROFILE,
    "birth_date": FieldCategory.PROFILE,
    "diagnosis": FieldCategory.MEDICAL,
    "medications": FieldCategory.MEDICAL,
}

# 敏感字段（返回前必须脱敏）
SENSITIVE_FIELDS: frozenset[str] = frozenset({"name", "id_card", "phone", "email"})


def mask_value(field_name: str, value: object) -> str:
    """对单个敏感值脱敏，只返回不可逆的掩码形式。"""
    text = "" if value is None else str(value)
    if field_name == "name":
        if len(text) <= 1:
            return "*"
        return text[0] + "*" * (len(text) - 1)
    if field_name == "id_card":
        if len(text) <= 4:
            return "*" * len(text)
        return "*" * (len(text) - 4) + text[-4:]
    if field_name == "phone":
        if len(text) <= 4:
            return "*" * len(text)
        return text[:3] + "*" * (len(text) - 7) + text[-4:]
    if field_name == "email":
        if "@" not in text:
            return "***"
        local, _, domain = text.partition("@")
        head = local[:1] if local else "*"
        return f"{head}***@{domain}"
    return "***"


def project_patient(
    patient: Patient, fields: list[FieldCategory]
) -> tuple[dict[str, object], list[str], list[str]]:
    """按授权类别投影患者数据。

    返回 (脱敏后的数据, 返回字段名列表, 被脱敏字段名列表)。
    非敏感类别按原值返回，敏感类别一律先掩码再返回。
    """
    wanted: set[FieldCategory] = set(fields)
    data: dict[str, object] = {}
    returned: list[str] = []
    masked: list[str] = []

    for field_name, category in FIELD_CATEGORIES.items():
        if category not in wanted:
            continue
        raw = getattr(patient, field_name)
        if field_name in SENSITIVE_FIELDS:
            data[field_name] = mask_value(field_name, raw)
            masked.append(field_name)
        else:
            data[field_name] = raw
        returned.append(field_name)

    return data, returned, masked
