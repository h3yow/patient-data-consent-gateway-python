"""线程安全的本地内存存储，模拟患者、同意与共享记录。"""
from __future__ import annotations

import threading

from app.models import ConsentView, PatientRecord


class Storage:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._patients: dict[str, PatientRecord] = {}
        self._consents: dict[str, ConsentView] = {}

    def reset(self) -> None:
        with self._lock:
            self._patients.clear()
            self._consents.clear()

    def put_patient(self, patient: PatientRecord) -> None:
        with self._lock:
            self._patients[patient.patient_id] = patient

    def get_patient(self, patient_id: str) -> PatientRecord | None:
        with self._lock:
            return self._patients.get(patient_id)

    def put_consent(self, consent: ConsentView) -> None:
        with self._lock:
            self._consents[consent.consent_id] = consent

    def get_consent(self, consent_id: str) -> ConsentView | None:
        with self._lock:
            return self._consents.get(consent_id)

    def update_consent(self, consent: ConsentView) -> None:
        with self._lock:
            if consent.consent_id not in self._consents:
                raise KeyError(consent.consent_id)
            self._consents[consent.consent_id] = consent

    def find_consent(
        self, patient_id: str, recipient_id: str, purpose: str
    ) -> ConsentView | None:
        """返回该患者/接收方/用途最新的一条同意（任意状态），无则 None。"""
        with self._lock:
            matches = [
                c
                for c in self._consents.values()
                if c.patient_id == patient_id
                and c.recipient_id == recipient_id
                and c.purpose == purpose
            ]
            if not matches:
                return None
            return max(matches, key=lambda c: c.granted_at)

    def list_consents(self, patient_id: str) -> list[ConsentView]:
        with self._lock:
            return sorted(
                (c for c in self._consents.values() if c.patient_id == patient_id),
                key=lambda c: c.granted_at,
            )

    @property
    def lock(self) -> threading.RLock:
        """复合操作（读-判定-写）需要持有的可重入锁。"""
        return self._lock
