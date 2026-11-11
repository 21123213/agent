"""尚未实现的业务接口：统一 501（已实现的 /api/files、/api/kb 勿挂在此）。"""
from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(tags=["stub"])


async def _stub(request: Request):
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"{request.url.path} 接口尚未实现",
    )


# /api/rag 已由 rag_router 实现，勿再挂 stub
for prefix in ("/api/chat", "/api/agent"):
    router.add_api_route(prefix, _stub, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    router.add_api_route(
        f"{prefix}/{{path:path}}",
        _stub,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
