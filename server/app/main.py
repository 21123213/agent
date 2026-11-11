"""FastAPI 入口：Agents 后端。"""
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.admin_router import router as admin_router
from app.api.askdata_router import router as askdata_router
from app.api.auth_router import router as auth_router
from app.api.files_router import router as files_router
from app.api.health_router import router as health_router
from app.api.kb_router import router as kb_router
from app.api.rag_router import router as rag_router
from app.api.reasoner_router import router as reasoner_router
from app.api.stub_router import router as stub_router
from app.common.observability import bootstrap_observability
from app.settings import get_settings

settings = get_settings()
bootstrap_observability()

# 轮询接口不刷 access 日志（仍正常处理请求）
_QUIET_ACCESS_SUBSTR = (
    "/status HTTP",
    "/chunks HTTP",
    "/api/files/list HTTP",
)


class _QuietPollAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(s in msg for s in _QUIET_ACCESS_SUBSTR)


def _install_quiet_access_log() -> None:
    for name in ("uvicorn.access", "uvicorn.access.asgi"):
        logging.getLogger(name).addFilter(_QuietPollAccessFilter())


_install_quiet_access_log()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Agents 多 Agent 平台后端",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 已实现业务路由须在 stub 之前注册
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(kb_router)
app.include_router(files_router)
app.include_router(rag_router)
app.include_router(reasoner_router)
app.include_router(askdata_router)
app.include_router(admin_router)
# 未实现：旧 /api/chat、/api/agent 仍 501；新业务走 /api/rag、/api/reasoner、/api/askdata
app.include_router(stub_router)


@app.get("/")
async def root():
    return {
        "message": "Agents API",
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
    }
