"""健康检查。"""
from fastapi import APIRouter

from app.settings import get_settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    s = get_settings()
    db_ok = False
    try:
        from app.infra.db import fetch_one

        fetch_one("SELECT 1 AS ok")
        db_ok = True
    except Exception:
        db_ok = False
    return {
        "status": "healthy" if db_ok else "degraded",
        "service": s.app_name,
        "version": s.app_version,
        "database": "ok" if db_ok else "error",
    }
