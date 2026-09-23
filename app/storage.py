"""本地内存存储（模拟数据库），带可选的审计写入故障注入。

测试可通过置位 audit_failure_injected 模拟审计落盘失败，
以验证访问在审计不可用时失败关闭。
"""

from __future__ import annotations

import threading

from .models import AuditEvent, Consent, Patient, Recipient


class Store:
    """线程内内存存储。"""

    def __init__(self) -> None:
        self._patients: dict[str, Patient] = {}
        self._recipients: dict[str, Recipient] = {}
        self._consents: dict[str, Consent] = {}
        self._audit: list[AuditEvent] = []
        self.audit_failure_injected = False
        # 单一可重入锁，保证并发撤销/访问下的决策一致性
        self.lock = threading.RLock()

    # 患者 / 接收方 -------------------------------------------------------

    def add_patient(self, patient: Patient) -> None:
        with self.lock:
            self._patients[patient.patient_id] = patient

    def get_patient(self, patient_id: str) -> Patient | None:
        with self.lock:
            return self._patients.get(patient_id)

    def add_recipient(self, recipient: Recipient) -> None:
        with self.lock:
            self._recipients[recipient.recipient_id] = recipient

    def get_recipient(self, recipient_id: str) -> Recipient | None:
        with self.lock:
            return self._recipients.get(recipient_id)

    # 同意 -----------------------------------------------------------------

    def save_consent(self, consent: Consent) -> Consent:
        with self.lock:
            self._consents[consent.consent_id] = consent
            return consent

    def get_consent(self, consent_id: str) -> Consent | None:
        with self.lock:
            return self._consents.get(consent_id)

    def find_consents(
        self, patient_id: str, recipient_id: str, purpose: str
    ) -> list[Consent]:
        """返回该三元组下的全部同意（含已撤销/过期），最近授予的在前。"""
        with self.lock:
            matches = [
                c
                for c in self._consents.values()
                if c.patient_id == patient_id
                and c.recipient_id == recipient_id
                and c.purpose.value == purpose
            ]
            return sorted(matches, key=lambda c: c.granted_at, reverse=True)

    def list_consents_for_patient(self, patient_id: str) -> list[Consent]:
        """返回某患者的全部同意，最近授予的在前。"""
        with self.lock:
            matches = [c for c in self._consents.values() if c.patient_id == patient_id]
            return sorted(matches, key=lambda c: c.granted_at, reverse=True)

    # 审计 -----------------------------------------------------------------

    def append_audit(self, event: AuditEvent) -> None:
        with self.lock:
            if self.audit_failure_injected:
                raise OSError("injected audit storage failure")
            self._audit.append(event)

    def list_audit(self) -> list[AuditEvent]:
        with self.lock:
            return list(self._audit)
