"""HTTP 接口装配：路由、错误映射与进程内单例依赖。"""
from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from app.audit import AuditLog
from app.clock import Clock
from app.errors import ErrorCode, GatewayError
from app.models import (
    AccessRequest,
    GrantConsentRequest,
    PatientRecord,
    RevokeConsentRequest,
)
from app.service import ConsentGateway
from app.storage import Storage

router = APIRouter()

clock = Clock()
storage = Storage()
audit_log = AuditLog(clock)
gateway = ConsentGateway(storage, audit_log, clock)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def _handle_gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.code.value, "message": exc.message},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 兜底响应不回传异常细节，避免把内部数据（可能含原值）暴露给调用方
        return JSONResponse(
            status_code=500,
            content={"error": "INTERNAL_ERROR", "message": "internal server error"},
        )


@router.post("/patients", status_code=201)
def create_patient(patient: PatientRecord):
    return gateway.seed_patient(patient)


@router.post("/consents", status_code=201)
def grant_consent(req: GrantConsentRequest):
    return gateway.grant_consent(req)


@router.post("/consents/revoke", status_code=200)
def revoke_consent(req: RevokeConsentRequest):
    return gateway.revoke_consent(req)


@router.get("/patients/{patient_id}/consents")
def list_consents(patient_id: str):
    return {"consents": gateway.list_consents(patient_id)}


@router.post("/access")
def access_data(req: AccessRequest):
    return gateway.access_patient_data(req)


@router.get("/audit")
def get_audit():
    return {"entries": audit_log.list_entries()}


# ---- 测试支撑接口（本地验证用；生产部署应关闭） ------------------------


@router.post("/internal/clock/advance")
def advance_clock(body: dict):
    seconds = int(body.get("seconds", 0))
    now = clock.advance(seconds)
    return {"now": now.isoformat()}


@router.post("/internal/audit/failing")
def set_audit_failing(body: dict):
    audit_log.set_failing(bool(body.get("failing", False)))
    return {"failing": audit_log.failing}


@router.post("/internal/reset")
def reset_all():
    storage.reset()
    audit_log.reset()
    clock.reset()
    return {"status": "reset"}
