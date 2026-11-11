"""
推理 Agent HTTP：会话 CRUD + SSE。

前缀 ``/api/reasoner``（见 docs/reasoner-agent-plan.md）。
无 log 事件；人工等待靠 needs_input + resume。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.agents.reasoner import agent as reasoner_agent
from app.api.schemas_reasoner import (
    MessageResponse,
    ReasonerChatStreamRequest,
    ReasonerHistoryOut,
    ReasonerMessageOut,
    ReasonerSessionCreate,
    ReasonerSessionOut,
)
from app.common.security import get_current_user
from app.infra import chat_repo
from app.infra.db import run_in_thread

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reasoner", tags=["推理 Agent"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _sse_pack(event: str, data: dict) -> str:
    """SSE 帧：event + data JSON。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.get("/sessions", response_model=list[ReasonerSessionOut])
async def list_sessions(current_user: dict = Depends(get_current_user)):
    def _work():
        rows = chat_repo.list_sessions_by_agent(
            user_id=int(current_user["id"]),
            agent_type="reasoner",
        )
        return [
            ReasonerSessionOut(
                session_id=r["session_id"],
                title=r.get("title"),
                status=r.get("status"),
                agent_type=r.get("agent_type") or "reasoner",
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
            for r in rows
        ]

    return await run_in_thread(_work)


@router.post(
    "/sessions",
    response_model=ReasonerSessionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: ReasonerSessionCreate,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        sid = chat_repo.create_session(
            user_id=int(current_user["id"]),
            unit_id=int(current_user["unit_id"]),
            knowledge_base_id=None,
            title=(body.title or "推理会话").strip() or "推理会话",
            agent_type="reasoner",
        )
        hint = "当前为推理 Agent：可切换 ReAct / Plan-Execute，需要时会向你确认必要信息。"
        chat_repo.insert_message(session_id=sid, role="assistant", content=hint)
        try:
            from app.infra import redis_client as redis_mgr

            redis_mgr.save_session_message(sid, "assistant", hint)
        except Exception:
            pass
        row = chat_repo.get_session(sid) or {}
        return ReasonerSessionOut(
            session_id=sid,
            title=row.get("title") or body.title or "推理会话",
            status="active",
            agent_type="reasoner",
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )

    return await run_in_thread(_work)


@router.get("/history/{session_id}", response_model=ReasonerHistoryOut)
async def history(
    session_id: str, current_user: dict = Depends(get_current_user)
):
    def _work():
        row = chat_repo.get_session(session_id)
        if not row or int(row["user_id"]) != int(current_user["id"]):
            raise HTTPException(404, "会话不存在")
        if (row.get("agent_type") or "") != "reasoner":
            raise HTTPException(400, "非推理 Agent 会话")
        msgs = chat_repo.list_messages(session_id)
        return ReasonerHistoryOut(
            session_id=session_id,
            messages=[
                ReasonerMessageOut(
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
        reasoner_agent.clear_session(session_id)
        chat_repo.delete_session(session_id)
        return MessageResponse(message="已删除")

    return await run_in_thread(_work)


@router.post("/chat/stream")
async def chat_stream(
    body: ReasonerChatStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    """SSE：meta / status / delta / needs_input / done / error。"""

    async def _gen() -> AsyncIterator[str]:
        resume_dict = None
        if body.resume is not None:
            resume_dict = {
                "resume_token": body.resume.resume_token,
                "answers": body.resume.answers,
            }
        async for ev in reasoner_agent.astream_reasoner(
            user_id=int(current_user["id"]),
            unit_id=int(current_user["unit_id"]),
            query=body.query or "",
            session_id=body.session_id,
            mode=body.mode,
            resume=resume_dict,
            answer_mode=body.answer_mode or "deep",
        ):
            yield _sse_pack(ev["event"], ev.get("data") or {})
            if ev.get("event") in ("status", "progress"):
                await asyncio.sleep(0)

    return StreamingResponse(_gen(), media_type="text/event-stream", headers=SSE_HEADERS)
