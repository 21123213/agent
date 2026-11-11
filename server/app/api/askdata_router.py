"""
智能问数 HTTP：会话 + SSE。

前缀 ``/api/askdata``（见 docs/askdata-plan.md）。
"""
from __future__ import annotations

import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents.askdata import agent as askdata_agent
from app.api.schemas_askdata import (
    AskdataChatStreamRequest,
    AskdataHistoryOut,
    AskdataMessageOut,
    AskdataSessionCreate,
    AskdataSessionOut,
    MessageResponse,
)
from app.common.security import get_current_user
from app.infra import chat_repo
from app.infra.db import run_in_thread

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/askdata", tags=["智能问数"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_pack(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/sessions", response_model=list[AskdataSessionOut])
async def list_sessions(current_user: dict = Depends(get_current_user)):
    def _work():
        rows = chat_repo.list_sessions_by_agent(
            user_id=int(current_user["id"]),
            agent_type="askdata",
        )
        return [
            AskdataSessionOut(
                session_id=r["session_id"],
                title=r.get("title"),
                status=r.get("status"),
                agent_type=r.get("agent_type") or "askdata",
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
            for r in rows
        ]

    return await run_in_thread(_work)


@router.post(
    "/sessions",
    response_model=AskdataSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: AskdataSessionCreate,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        sid = askdata_agent.ensure_session(
            user_id=int(current_user["id"]),
            unit_id=int(current_user["unit_id"]),
            session_id=None,
            title=(body.title or "问数会话").strip() or "问数会话",
        )
        row = chat_repo.get_session(sid) or {}
        return AskdataSessionOut(
            session_id=sid,
            title=row.get("title") or body.title,
            status="active",
            agent_type="askdata",
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    return await run_in_thread(_work)


@router.get("/history/{session_id}", response_model=AskdataHistoryOut)
async def history(session_id: str, current_user: dict = Depends(get_current_user)):
    def _work():
        row = chat_repo.get_session(session_id)
        if not row or int(row["user_id"]) != int(current_user["id"]):
            raise HTTPException(404, "会话不存在")
        if (row.get("agent_type") or "") != "askdata":
            raise HTTPException(400, "非智能问数会话")
        msgs = chat_repo.list_messages(session_id)
        return AskdataHistoryOut(
            session_id=session_id,
            messages=[
                AskdataMessageOut(
                    id=m.get("id"),
                    role=m["role"],
                    content=m.get("content") or "",
                    created_at=m.get("created_at"),
                )
                for m in msgs
            ],
        )

    return await run_in_thread(_work)


@router.delete("/session/{session_id}", response_model=MessageResponse)
async def delete_session(
    session_id: str, current_user: dict = Depends(get_current_user)
):
    def _work():
        row = chat_repo.get_session(session_id)
        if not row or int(row["user_id"]) != int(current_user["id"]):
            raise HTTPException(404, "会话不存在")
        chat_repo.delete_session(session_id)
        return MessageResponse(message="已删除")

    return await run_in_thread(_work)


@router.post("/chat/stream")
async def chat_stream(
    body: AskdataChatStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    async def _gen() -> AsyncIterator[str]:
        async for ev in askdata_agent.astream_askdata(
            user=current_user,
            query=body.query,
            session_id=body.session_id,
        ):
            yield _sse_pack(ev["event"], ev.get("data") or {})

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)
