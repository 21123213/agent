"""
RAG HTTP：会话 CRUD + SSE 流式问答。

前缀 ``/api/rag``。Online 走 Agent（``rag_search`` 按需检索）；Eval 走 ``run_rag``。
换知识库 = 换会话（``ensure_or_create_session``）。
"""
from __future__ import annotations

import logging
from typing import AsyncIterator, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.agents.rag import chain as rag_chain
from app.api.schemas_rag import (
    MessageResponse,
    RagChatStreamRequest,
    RagHistoryOut,
    RagMessageOut,
    RagSessionCreate,
    RagSessionOut,
)
from app.common.permissions import require_kb_access
from app.common.security import get_current_user
from app.infra import chat_repo, kb_repo
from app.infra import redis_client as redis_mgr
from app.infra.db import run_in_thread

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rag", tags=["RAG 问答"])

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _get_kb_or_404(kb_id: int) -> dict:
    kb = kb_repo.get_by_id(kb_id)
    if not kb or (kb.get("status") and kb["status"] != "active"):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb


# ---------------------------------------------------------------------------
# 会话 CRUD
# ---------------------------------------------------------------------------


@router.get("/sessions", response_model=list[RagSessionOut])
async def list_sessions(
    knowledge_base_id: int = Query(..., description="按 KB 过滤"),
    current_user: dict = Depends(get_current_user),
):
    """当前用户在该知识库下的会话列表。"""

    def _work():
        kb = _get_kb_or_404(knowledge_base_id)
        require_kb_access(current_user, kb)
        rows = chat_repo.list_sessions(
            user_id=int(current_user["id"]),
            knowledge_base_id=knowledge_base_id,
            agent_type="rag",
        )
        return [
            RagSessionOut(
                session_id=r["session_id"],
                knowledge_base_id=r.get("knowledge_base_id"),
                title=r.get("title"),
                status=r.get("status"),
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
            for r in rows
        ]

    return await run_in_thread(_work)


@router.post(
    "/sessions", response_model=RagSessionOut, status_code=status.HTTP_201_CREATED
)
async def create_session(
    body: RagSessionCreate,
    current_user: dict = Depends(get_current_user),
):
    """新建会话；返回 welcome_hint（当前知识库名称提示）。"""

    def _work():
        kb = _get_kb_or_404(body.knowledge_base_id)
        require_kb_access(current_user, kb)
        title = (body.title or "新会话").strip() or "新会话"
        sid = chat_repo.create_session(
            user_id=int(current_user["id"]),
            unit_id=int(current_user["unit_id"]),
            knowledge_base_id=int(body.knowledge_base_id),
            title=title,
            agent_type="rag",
        )
        hint = rag_chain.welcome_hint_for_kb(int(body.knowledge_base_id))
        # 助手侧欢迎语写入历史，便于前端一打开就能看到
        chat_repo.insert_message(
            session_id=sid, role="assistant", content=hint
        )
        try:
            redis_mgr.save_session_message(sid, "assistant", hint)
        except Exception:
            pass
        row = chat_repo.get_session(sid)
        return RagSessionOut(
            session_id=sid,
            knowledge_base_id=body.knowledge_base_id,
            title=title,
            status="active",
            created_at=(row or {}).get("created_at"),
            updated_at=(row or {}).get("updated_at"),
            welcome_hint=hint,
        )

    return await run_in_thread(_work)


@router.get("/history/{session_id}", response_model=RagHistoryOut)
async def get_history(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = chat_repo.get_session(session_id)
        if not row:
            raise HTTPException(status_code=404, detail="会话不存在")
        if int(row["user_id"]) != int(current_user["id"]):
            raise HTTPException(status_code=403, detail="无权访问该会话")
        msgs = chat_repo.list_messages(session_id)
        return RagHistoryOut(
            session_id=session_id,
            messages=[
                RagMessageOut(
                    id=m.get("id"),
                    role=m["role"],
                    content=m.get("content") or "",
                    sources=m.get("sources"),
                    created_at=m.get("created_at"),
                )
                for m in msgs
            ],
        )

    return await run_in_thread(_work)


@router.delete("/session/{session_id}", response_model=MessageResponse)
async def delete_session(
    session_id: str,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = chat_repo.get_session(session_id)
        if not row:
            raise HTTPException(status_code=404, detail="会话不存在")
        if int(row["user_id"]) != int(current_user["id"]):
            raise HTTPException(status_code=403, detail="无权访问该会话")
        chat_repo.delete_session(session_id)
        redis_mgr.clear_session_context(session_id)
        return MessageResponse(
            success=True, message="会话已删除", data={"session_id": session_id}
        )

    return await run_in_thread(_work)


@router.delete("/clear", response_model=MessageResponse)
async def clear_sessions(
    knowledge_base_id: int = Query(...),
    current_user: dict = Depends(get_current_user),
):
    """清空当前用户在某 KB 下全部会话（DB CASCADE + 清 Redis）。"""

    def _work():
        kb = _get_kb_or_404(knowledge_base_id)
        require_kb_access(current_user, kb)
        sids = chat_repo.clear_sessions_for_kb(
            user_id=int(current_user["id"]),
            knowledge_base_id=knowledge_base_id,
            agent_type="rag",
        )
        for sid in sids:
            redis_mgr.clear_session_context(sid)
        return MessageResponse(
            success=True,
            message=f"已清空 {len(sids)} 个会话",
            data={"count": len(sids)},
        )

    return await run_in_thread(_work)


# ---------------------------------------------------------------------------
# SSE 流式问答
# ---------------------------------------------------------------------------


@router.post("/chat/stream")
async def chat_stream(
    body: RagChatStreamRequest,
    current_user: dict = Depends(get_current_user),
):
    """
    SSE：event = meta | status | delta | done | error（及 needs_input）。

    Online：LangGraph Agent + ``rag_search``（按需，最多 2 次）；切片在 ``done.sources``。
    """
    query = (body.query or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="问题不能为空")

    user_id = int(current_user["id"])
    unit_id = int(current_user["unit_id"])
    kb_id = int(body.knowledge_base_id)

    def _prepare() -> str:
        kb = _get_kb_or_404(kb_id)
        require_kb_access(current_user, kb)
        try:
            return rag_chain.ensure_or_create_session(
                user_id=user_id,
                unit_id=unit_id,
                knowledge_base_id=kb_id,
                session_id=body.session_id,
                title_hint=query,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    session_id = await run_in_thread(_prepare)

    vw = body.vector_weight
    bw = body.bm25_weight
    if vw is not None and bw is None:
        bw = max(0.0, 1.0 - float(vw))
    elif bw is not None and vw is None:
        vw = max(0.0, 1.0 - float(bw))

    am = (body.answer_mode or "deep").strip().lower()
    if am not in ("quick", "deep"):
        am = "deep"

    async def _gen() -> AsyncIterator[str]:
        async for frame in rag_chain.stream_rag_agent_events(
            user_id=user_id,
            unit_id=unit_id,
            query=query,
            knowledge_base_id=kb_id,
            session_id=session_id,
            file_ids=body.file_ids,
            vector_weight=vw,
            bm25_weight=bw,
            answer_mode=am,
        ):
            yield frame

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
