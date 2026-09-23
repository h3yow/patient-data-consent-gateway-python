"""敏感字段去标识化规则。

规则均为单向、确定性的本地掩码，掩码后的值不含可还原的原始信息。
"""
from __future__ import annotations

from datetime import date

from app.models import SENSITIVE_FIELDS


def is_sensitive(field: str) -> bool:
    return field in SENSITIVE_FIELDS


def _mask_middle(value: str, keep_head: int, keep_tail: int) -> str:
    if len(value) <= keep_head + keep_tail:
        # 过短值无法保留首尾，整体掩码，避免泄露长度以外的信息
        return "*" * len(value)
    hidden = len(value) - keep_head - keep_tail
    return value[:keep_head] + "*" * hidden + value[len(value) - keep_tail :]


def mask_value(field: str, value):  # type: ignore[no-untyped-def]
    """返回字段的去标识化表示；未知敏感字段整体掩码。"""
    if value is None:
        return None
    if field == "full_name":
        text = str(value)
        if len(text) <= 1:
            return "*"
        return text[0] + "*" * (len(text) - 1)
    if field == "phone":
        digits = "".join(ch for ch in str(value) if ch.isdigit())
        return _mask_middle(digits, 3, 4)
    if field == "id_card":
        return _mask_middle(str(value), 6, 4)
    if field == "email":
        text = str(value)
        if "@" not in text:
            return "*" * len(text)
        local, domain = text.split("@", 1)
        if len(local) <= 1:
            masked_local = "*"
        else:
            masked_local = local[0] + "*" * (len(local) - 1)
        return f"{masked_local}@{domain}"
    if field == "address":
        text = str(value)
        keep = min(6, len(text))
        return text[:keep] + "****"
    if field == "date_of_birth":
        if isinstance(value, date):
            return f"{value.year:04d}-**-**"
        text = str(value)
        if len(text) >= 4 and text[:4].isdigit():
            return f"{text[:4]}-**-**"
        return "****-**-**"
    # 兜底：任何被标记为敏感但未配置规则的字段一律整体掩码
    return "*" * len(str(value))
