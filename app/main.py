"""FastAPI 应用工厂与对外接口路由。

所有失败以 HTTP 403 返回可区分的拒绝原因码（审计不可用亦然），
错误体内只含原因码与说明，绝不包含患者原始敏感值。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .access import AccessService
from .audit import AuditLogger
from .consent import ConsentService
from .errors import GatewayError
from .models import (
    AccessRequest,
    AuditEvent,
    Consent,
    DataAccessResponse,
    DecisionResponse,
    GrantConsentRequest,
    NarrowConsentRequest,
    Patient,
    PatientCreate,
    Recipient,
    RecipientCreate,
    ResumeConsentRequest,
    RevokeConsentRequest,
    SuspendConsentRequest,
)
from .storage import Store

logger = logging.getLogger("consent_gateway.api")


def create_app(seed: bool = True) -> FastAPI:
    app = FastAPI(title="患者数据共享同意与访问网关")
    store = Store()
    audit = AuditLogger(store)

    app.state.store = store
    app.state.audit = audit

    if seed:
        _seed(store)

    @app.exception_handler(GatewayError)
    async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
        # 注意：响应与日志均不含患者原始敏感值
        logger.warning("gateway_denial reason=%s", exc.reason.value)
        return JSONResponse(
            status_code=403,
            content={"allowed": False, "denial_reason": exc.reason.value, "message": exc.message},
        )

    @app.post("/admin/patients", response_model=Patient, status_code=201)
    def create_patient(payload: PatientCreate) -> Patient:
        patient = Patient(**payload.model_dump())
        store.add_patient(patient)
        return patient

    @app.post("/admin/recipients", response_model=Recipient, status_code=201)
    def create_recipient(payload: RecipientCreate) -> Recipient:
        recipient = Recipient(**payload.model_dump())
        store.add_recipient(recipient)
        return recipient

    @app.post("/consents", response_model=Consent, status_code=201)
    def grant_consent(payload: GrantConsentRequest) -> Consent:
        return ConsentService(store, audit).grant(payload)

    @app.post("/consents/revoke", response_model=Consent)
    def revoke_consent(payload: RevokeConsentRequest) -> Consent:
        return ConsentService(store, audit).revoke(payload)

    @app.post("/consents/narrow", response_model=Consent)
    def narrow_consent(payload: NarrowConsentRequest) -> Consent:
        return ConsentService(store, audit).narrow(payload)

    @app.post("/consents/suspend", response_model=Consent)
    def suspend_consent(payload: SuspendConsentRequest) -> Consent:
        return ConsentService(store, audit).suspend(payload)

    @app.post("/consents/resume", response_model=Consent)
    def resume_consent(payload: ResumeConsentRequest) -> Consent:
        return ConsentService(store, audit).resume(payload)

    @app.post("/access/decision", response_model=DecisionResponse)
    def access_decision(payload: AccessRequest) -> DecisionResponse:
        # 纯决策端点：不落数据、不脱敏，仅返回授权结论与范围
        return ConsentService(store, audit).evaluate(
            patient_id=payload.patient_id,
            recipient_id=payload.recipient_id,
            purpose=payload.purpose,
            requested_fields=list(payload.requested_fields),
            now=payload.now,
        )

    @app.post("/access/data", response_model=DataAccessResponse)
    def access_data(payload: AccessRequest) -> DataAccessResponse:
        return AccessService(store, audit).access_data(payload)

    @app.get("/audit", response_model=list[AuditEvent])
    def list_audit() -> list[AuditEvent]:
        return store.list_audit()

    @app.post("/test/audit-failure", status_code=200)
    def inject_audit_failure(enabled: bool = True) -> dict:
        """测试辅助：注入/解除审计落盘故障。"""
        store.audit_failure_injected = enabled
        return {"audit_failure_injected": enabled}

    return app


def _seed(store: Store) -> None:
    """填充本地演示数据（患者、接收方），同意由调用方通过接口授予。"""
    store.add_patient(
        Patient(
            patient_id="P-001",
            name="张三丰",
            id_card="110101199001017890",
            phone="13812345678",
            email="sanfeng@example.com",
            gender="男",
            birth_date="1990-01-01",
            diagnosis="2型糖尿病",
            medications=["二甲双胍", "格列美脲"],
        )
    )
    store.add_patient(
        Patient(
            patient_id="P-002",
            name="李四",
            id_card="310101199203044567",
            phone="13987654321",
            email="lisi@example.com",
            gender="女",
            birth_date="1992-03-04",
            diagnosis="高血压",
            medications=["氨氯地平"],
        )
    )
    store.add_recipient(Recipient(recipient_id="R-HOSPITAL", name="市中心医院"))
    store.add_recipient(Recipient(recipient_id="R-RESEARCH", name="医学研究机构"))
    store.add_recipient(Recipient(recipient_id="R-INSURER", name="商业保险公司"))


app = create_app()
