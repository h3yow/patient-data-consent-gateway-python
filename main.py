from fastapi import FastAPI

from app.api import register_exception_handlers, router

app = FastAPI(title="Patient Data Consent Gateway")
app.include_router(router)
register_exception_handlers(app)


@app.get("/")
def read_root() -> dict[str, str]:
    return {"service": "patient-data-consent-gateway"}
