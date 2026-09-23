"""本地 API 服务入口：uvicorn main:app。"""

from app.main import app

__all__ = ["app"]
