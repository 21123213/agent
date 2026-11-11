"""
RAG 会话与 SSE 编排。

双轨（2026-11）
--------------
- **Online**：``stream_rag_agent_events`` → Reasoner LangGraph + ``rag_search``（按需检索，最多 2 次）。
- **Eval**：``rag.agent.run_rag`` + ``retrieve_only``（原题直传，返回 ``stages``）。
- 本文件仍保留旧 LCEL ``stream_rag_events`` 仅作对照/回滚，路由默认走 Agent。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Generator, List, Optional

from langchain_core.runnables import RunnableLambda

from app.agents.rag.cache.qa_cache import get_cached_answer, set_cached_answer
from app.agents.rag.memory import user_memory as user_memory_mod
from app.agents.rag.memory.session_memory import enforce_memory_limits, max_context_turns
from app.agents.rag.pipeline.answer import EMPTY_REPLY, generate_answer_stream
from app.agents.rag.pipeline.query_understanding import understand_query
from app.agents.rag.pipeline.retrieve_full import retrieve_full, _normalize_weights
from app.common import tracing as rag_trace
from app.infra import chat_repo, kb_repo
from app.infra import redis_client as redis_mgr

logger = logging.getLogger(__name__)


def normalize_weights(
    vector_weight: Optional[float], bm25_weight: Optional[float]
) -> tuple[float, float]:
    return _normalize_weights(vector_weight, bm25_weight)


def load_session_window(session_id: str) -> List[Dict[str, Any]]:
    """
    热路径读 Redis；miss 则从 MySQL 取最近窗口并回填 Redis。
    use_history 业务忽略：有会话即返回可拼上下文。
    """
    ctx = redis_mgr.get_session_context(session_id)
    if ctx:
        return ctx
    limit = max_context_turns() * 2
    # 多取一点，便于 enforce 时保留摘要空间
    messages = chat_repo.get_recent_messages(session_id, limit=limit + 4)
    slim = [
        {
            "role": m["role"],
            "content": m.get("content") or "",
            **({"sources": m["sources"]} if m.get("sources") else {}),
        }
        for m in messages
    ]
    slim = enforce_memory_limits(slim)
    if slim:
        try:
            redis_mgr.set_session_context(session_id, slim)
        except Exception as exc:
            logger.warning("backfill redis session failed: %s", exc)
    return slim


def persist_turn(
    *,
    session_id: str,
    query: str,
    answer: str,
    sources: List[Dict[str, Any]],
    metadata: Dict[str, Any] | None = None,
) -> None:
    """
    双写一轮：
    1) INSERT user  2) Redis append user
    3) INSERT assistant(+sources)  4) Redis append assistant
    5) touch session
    DB 失败应向上抛；Redis 失败只记日志。
    """
    chat_repo.insert_message(session_id=session_id, role="user", content=query)
    try:
        # 整轮写完再压缩一次，避免 user/assistant 各 enforce 导致双次摘要 LLM
        redis_mgr.save_session_message(
            session_id, "user", query, enforce=False
        )
    except Exception as exc:
        logger.warning("redis append user failed sid=%s: %s", session_id, exc)

    chat_repo.insert_message(
        session_id=session_id,
        role="assistant",
        content=answer,
        sources=sources,
        metadata=metadata,
    )
    try:
        redis_mgr.save_session_message(
            session_id, "assistant", answer, sources=sources, enforce=True
        )
    except Exception as exc:
        logger.warning("redis append assistant failed sid=%s: %s", session_id, exc)

    chat_repo.touch_session(session_id)


def chunks_to_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    sources: List[Dict[str, Any]] = []
    for doc in chunks or []:
        meta = dict(doc.get("metadata") or {})
        if doc.get("file_id") is not None:
            meta.setdefault("file_id", doc.get("file_id"))
        if doc.get("chunk_index") is not None:
            meta.setdefault("chunk_index", doc.get("chunk_index"))
        sources.append(
            {
                "content": (doc.get("content") or doc.get("chunk_text") or "")[:2000],
                "score": float(
                    doc.get("rrf_score") or doc.get("score") or 0
                ),
                "metadata": meta,
            }
        )
    return sources


# --- LCEL 具名步骤（LangSmith 可见）-----------------------------------------


def _step_understand(payload: Dict[str, Any]) -> Dict[str, Any]:
    query = payload["query"]
    context = payload.get("session_context") or []
    understood = understand_query(query, context)
    standalone = (understood.get("standalone_query") or query).strip()
    payload = dict(payload)
    payload["standalone_query"] = standalone
    payload["understood"] = understood
    return payload


def _step_retrieve(payload: Dict[str, Any]) -> Dict[str, Any]:
    retrieval = retrieve_full(
        unit_id=int(payload["unit_id"]),
        knowledge_base_id=int(payload["knowledge_base_id"]),
        user_input=payload["query"],
        session_context=payload.get("session_context"),
        file_ids=payload.get("file_ids"),
        vector_weight=payload.get("vector_weight"),
        bm25_weight=payload.get("bm25_weight"),
        standalone_query=payload.get("standalone_query"),
    )
    payload = dict(payload)
    payload["retrieval"] = retrieval
    return payload


understand_runnable = RunnableLambda(_step_understand).with_config(run_name="rewrite")
retrieve_runnable = RunnableLambda(_step_retrieve).with_config(run_name="retrieve_full")


def _event(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_rag_agent_events(
    *,
    user_id: int,
    unit_id: int,
    query: str,
    knowledge_base_id: int,
    session_id: str,
    file_ids: Optional[List[int]] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
    answer_mode: str = "deep",
) -> AsyncIterator[str]:
    """
    Online 正式路径：SSE 文本帧（meta / status / delta / done / error）。

    - ``answer_mode=quick``：baseline 检索 + 4 万 token + ReAct（QUICK 模型）
    - ``answer_mode=deep``：full 检索 + 8 万 token + ReAct（中间 FAST、收口 DEEP）
    """
    from app.agents.reasoner.agent import astream_reasoner

    vw, bw = normalize_weights(vector_weight, bm25_weight)
    am = (answer_mode or "deep").strip().lower()
    if am not in ("quick", "deep"):
        am = "deep"
    async for item in astream_reasoner(
        user_id=user_id,
        unit_id=unit_id,
        query=query,
        session_id=session_id,
        mode="react",
        knowledge_base_id=knowledge_base_id,
        file_ids=file_ids,
        vector_weight=vw,
        bm25_weight=bw,
        agent_type="rag",
        skip_ensure=True,
        answer_mode=am,
    ):
        ev = item.get("event") or "message"
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        yield _event(ev, data)
        if ev in ("status", "progress"):
            await asyncio.sleep(0)


_PLACEHOLDER_TITLES = frozenset({"新会话", "推理会话", "知识库会话", "问数会话"})


def ensure_or_create_session(
    *,
    user_id: int,
    unit_id: int,
    knowledge_base_id: int,
    session_id: Optional[str],
    title_hint: str = "",
) -> str:
    """
    session_id 空 → 新建并绑 KB。
    非空 → 校验归属用户且 knowledge_base_id 一致（换 KB 必须换会话）。
    已有会话若仍是占位标题，首问到来时用问题句改名。
    """
    if not session_id:
        title = (title_hint or "新会话")[:30] or "新会话"
        return chat_repo.create_session(
            user_id=user_id,
            unit_id=unit_id,
            knowledge_base_id=knowledge_base_id,
            title=title,
            agent_type="rag",
        )

    row = chat_repo.get_session(session_id)
    if not row:
        raise ValueError("会话不存在")
    if int(row["user_id"]) != int(user_id):
        raise PermissionError("无权访问该会话")
    if int(row.get("knowledge_base_id") or 0) != int(knowledge_base_id):
        raise ValueError("会话与知识库不匹配（换知识库请新建会话）")
    if row.get("status") and row["status"] != "active":
        raise ValueError("会话已归档")
    hint = (title_hint or "").strip()
    cur_title = (row.get("title") or "").strip()
    if hint and cur_title in _PLACEHOLDER_TITLES:
        chat_repo.update_title(session_id, hint[:30])
    return session_id


def stream_rag_events(
    *,
    user_id: int,
    unit_id: int,
    query: str,
    knowledge_base_id: int,
    session_id: str,
    file_ids: Optional[List[int]] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
) -> Generator[str, None, None]:
    """
    产出 SSE 文本帧：meta / status / delta / done / error。

    主链路：
    JWT 已在路由层校验 → 本函数假定 session 已 ensure
    → 规范化权重 → 可选改写 → QA cache
         HIT  → 双写会话 → done(cache_hit=true)
         MISS → 长期记忆 + 会话窗 → retrieve_full → stream generate
              → 写 cache → 双写 → Celery 记忆抽取
    """
    request_id = rag_trace.new_request_id()
    vw, bw = normalize_weights(vector_weight, bm25_weight)

    def _bind() -> None:
        # SSE generator 可能跨线程 resume，关键步骤前重绑 trace
        rag_trace.set_trace_context(
            request_id, user_id=user_id, session_id=session_id
        )

    def _event(event: str, data: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    try:
        _bind()
        rag_trace.emit(
            "request_start",
            data={
                "query": rag_trace.truncate_text(query),
                "session_id": session_id,
                "kb_id": knowledge_base_id,
                "unit_id": unit_id,
                "file_ids": file_ids,
                "vector_weight": vw,
                "bm25_weight": bw,
                "mode": "stream",
            },
        )

        # meta 先发；cache_hit 在查完缓存后再补一条或写在 done
        yield _event(
            "meta",
            {
                "session_id": session_id,
                "request_id": request_id,
                "knowledge_base_id": knowledge_base_id,
                "cache_hit": False,
            },
        )
        yield _event("status", {"phase": "retrieving"})

        _bind()
        session_context = load_session_window(session_id)

        # 改写（指代不清时）→ 得到缓存键与检索用问句
        payload = {
            "query": query,
            "session_context": session_context,
            "unit_id": unit_id,
            "knowledge_base_id": knowledge_base_id,
            "file_ids": file_ids,
            "vector_weight": vw,
            "bm25_weight": bw,
        }
        payload = understand_runnable.invoke(payload)
        standalone = payload["standalone_query"]

        cached = get_cached_answer(
            unit_id=unit_id,
            knowledge_base_id=knowledge_base_id,
            query_for_cache=standalone,
            vector_weight=vw,
            bm25_weight=bw,
            file_ids=file_ids,
        )
        if cached:
            answer = (cached.get("answer") or "").strip() or EMPTY_REPLY
            sources = list(cached.get("sources") or [])
            yield _event(
                "meta",
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "knowledge_base_id": knowledge_base_id,
                    "cache_hit": True,
                },
            )
            yield _event("status", {"phase": "generating", "cache_hit": True})
            yield _event("delta", {"content": answer})
            persist_turn(
                session_id=session_id,
                query=query,
                answer=answer,
                sources=sources,
                metadata={"cache_hit": True, "request_id": request_id},
            )
            turns = chat_repo.count_user_assistant_turns(session_id)
            user_memory_mod.maybe_enqueue_extract(
                session_id=session_id,
                user_id=user_id,
                unit_id=unit_id,
                turn_count=turns,
            )
            rag_trace.finish(
                status="ok",
                cache_hit=True,
                answer_preview=rag_trace.truncate_text(answer),
            )
            yield _event(
                "done",
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "answer": answer,
                    "sources": sources,
                    "cache_hit": True,
                    "knowledge_base_id": knowledge_base_id,
                },
            )
            return

        # --- MISS：长期记忆 + 检索 + 流式生成 ---
        _bind()
        memories = user_memory_mod.retrieve_for_prompt(
            user_id=user_id,
            unit_id=unit_id,
            query=query,
            scenario="rag_qa",
        )
        memory_block = user_memory_mod.format_memories_for_prompt(memories)

        payload = retrieve_runnable.invoke(payload)
        retrieval = payload["retrieval"]
        skip = bool(retrieval.get("skip_answer"))
        chunks = retrieval.get("chunks") or []
        sources = [] if skip else chunks_to_sources(chunks)

        yield _event("status", {"phase": "generating", "cache_hit": False})
        _bind()

        answer_parts: List[str] = []
        if skip:
            answer = retrieval.get("reply_hint") or EMPTY_REPLY
            answer_parts.append(answer)
            yield _event("delta", {"content": answer})
        else:
            for delta in generate_answer_stream(
                retrieval.get("standalone_query") or query,
                chunks,
                history=session_context,
                long_term_memory_block=memory_block,
            ):
                answer_parts.append(delta)
                yield _event("delta", {"content": delta})

        answer = "".join(answer_parts).strip() or "抱歉，未能生成有效回答。"
        persist_turn(
            session_id=session_id,
            query=query,
            answer=answer,
            sources=sources,
            metadata={
                "cache_hit": False,
                "request_id": request_id,
                "standalone_query": retrieval.get("standalone_query") or standalone,
            },
        )

        if not skip:
            set_cached_answer(
                unit_id=unit_id,
                knowledge_base_id=knowledge_base_id,
                query_for_cache=retrieval.get("standalone_query") or standalone,
                answer=answer,
                sources=sources,
                vector_weight=vw,
                bm25_weight=bw,
                file_ids=file_ids,
            )

        turns = chat_repo.count_user_assistant_turns(session_id)
        user_memory_mod.maybe_enqueue_extract(
            session_id=session_id,
            user_id=user_id,
            unit_id=unit_id,
            turn_count=turns,
        )

        latency_ms = None
        rag_trace.finish(
            status="ok",
            cache_hit=False,
            skip_answer=skip,
            answer_preview=rag_trace.truncate_text(answer),
            hits=len(chunks),
        )
        yield _event(
            "done",
            {
                "session_id": session_id,
                "request_id": request_id,
                "answer": answer,
                "sources": sources,
                "cache_hit": False,
                "knowledge_base_id": knowledge_base_id,
                "latency_ms": latency_ms,
            },
        )
    except Exception as exc:
        from app.common.user_errors import log_and_public

        _bind()
        public_msg = log_and_public(
            logger,
            exc,
            where="stream_rag_events",
            request_id=request_id,
        )
        rag_trace.finish(
            status="error",
            message=str(exc),
            error_type=type(exc).__name__,
        )
        yield _event(
            "error",
            {"message": public_msg, "request_id": request_id},
        )
    finally:
        rag_trace.clear_trace_context()


def welcome_hint_for_kb(knowledge_base_id: int) -> str:
    """新建会话后助手侧固定提示。"""
    row = kb_repo.get_by_id(knowledge_base_id)
    name = (row or {}).get("name") or f"#{knowledge_base_id}"
    return f"当前知识库：**{name}**，后续问答将基于该库检索。"
