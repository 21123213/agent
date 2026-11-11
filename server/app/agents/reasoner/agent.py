"""
Reasoner / 知识库 Agent 对外门面：建会话、跑图、SSE 事件、resume、持久化。

分工（2026-11）
--------------
- **Eval RAG**：``rag.agent.run_rag`` + ``retrieve_only``（无图、无改写）。
- **Online**：本模块 LangGraph；绑 ``knowledge_base_id`` 时挂 ``rag_search``（最多 2 次）。
- 旅游等其它 skill 与 rag 可并存。

线程模型：``thread_id`` ≡ ``session_id``（Redis checkpointer）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, List, Literal, Optional

from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import Command

from app.agents.rag.memory import user_memory as user_memory_mod
from app.agents.reasoner.graph import get_compiled_graph
from app.agents.reasoner.skills import clear_session_skills
from app.agents.reasoner.checkpoint_redis import get_checkpointer
from app.agents.reasoner.state import empty_budget
from app.common import metrics
from app.common import tracing as rag_trace
from app.infra import chat_repo
from app.infra import redis_client as redis_mgr
from app.settings import get_settings

logger = logging.getLogger(__name__)

RunMode = Literal["react", "plan_execute"]


def ensure_session(
    *,
    user_id: int,
    unit_id: int,
    session_id: Optional[str],
    title: Optional[str] = None,
    knowledge_base_id: Optional[int] = None,
    agent_type: str = "reasoner",
) -> str:
    """
    确保会话存在。

    - 默认 ``agent_type=reasoner``、KB 可空（纯工具 Agent）。
    - RAG Tab：``agent_type=rag`` + 必填 ``knowledge_base_id``（换 KB = 换会话）。
    """
    at = (agent_type or "reasoner").strip() or "reasoner"
    _placeholder = frozenset({"新会话", "推理会话", "知识库会话", "问数会话"})
    if session_id:
        row = chat_repo.get_session(session_id)
        if not row:
            raise ValueError("会话不存在")
        if int(row["user_id"]) != int(user_id):
            raise ValueError("无权访问该会话")
        if (row.get("agent_type") or "") != at:
            raise ValueError(f"会话类型不是 {at}")
        if knowledge_base_id is not None:
            row_kb = int(row.get("knowledge_base_id") or 0)
            if row_kb != int(knowledge_base_id):
                raise ValueError("会话与知识库不匹配（换知识库请新建会话）")
        hint = (title or "").strip()
        cur_title = (row.get("title") or "").strip()
        if hint and cur_title in _placeholder:
            chat_repo.update_title(session_id, hint[:30])
        return session_id

    return chat_repo.create_session(
        user_id=user_id,
        unit_id=unit_id,
        knowledge_base_id=knowledge_base_id,  # type: ignore[arg-type]
        title=(title or ("知识库会话" if knowledge_base_id else "推理会话")).strip()
        or "新会话",
        agent_type=at,
    )


def persist_turn(
    *,
    session_id: str,
    query: str,
    answer: str,
    sources: Optional[List[Dict[str, Any]]] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """MySQL + Redis 双写一轮；RAG 时 ``sources`` 存检索切片。"""
    chat_repo.insert_message(session_id=session_id, role="user", content=query)
    try:
        # 整轮写完再压缩：user 不 enforce，assistant 再 enforce，否则会打两次摘要 LLM
        redis_mgr.save_session_message(
            session_id, "user", query, enforce=False
        )
    except Exception as exc:
        logger.warning("redis append user failed: %s", exc)

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
        logger.warning("redis append assistant failed: %s", exc)

    chat_repo.touch_session(session_id)


def maybe_enqueue_memory(
    *,
    session_id: str,
    user_id: int,
    unit_id: int,
    scenario: str = "reasoner",
) -> None:
    """每 N 轮投递长期记忆抽取。"""
    if not user_memory_mod.user_memory_enabled():
        return
    n = int(get_settings().user_memory_extract_every_n or 3)
    turns = chat_repo.count_user_assistant_turns(session_id)
    if n <= 0 or turns % n != 0:
        return
    try:
        from app.agents.rag.tasks import extract_user_memory_task

        extract_user_memory_task.delay(
            session_id, user_id, unit_id, scenario
        )
        logger.info(
            "enqueued extract_user_memory reasoner sid=%s turns=%s",
            session_id,
            turns,
        )
    except Exception as exc:
        logger.warning("enqueue memory failed: %s", exc)


def clear_session(session_id: str) -> None:
    """清短记忆 / skills / checkpointer 线程；DB 会话由路由层删除。"""
    try:
        redis_mgr.clear_session_context(session_id)
    except Exception:
        pass
    clear_session_skills(session_id)
    try:
        get_checkpointer().delete_thread(session_id)
    except Exception as exc:
        logger.warning("delete checkpoint thread failed: %s", exc)


def _thread_config(session_id: str) -> dict:
    return {"configurable": {"thread_id": session_id}}


def _chunks_to_sources(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
                    doc.get("rrf_score")
                    or doc.get("rerank_score")
                    or doc.get("score")
                    or 0
                ),
                "metadata": meta,
            }
        )
    return sources


def _extract_interrupt_payload(result: Any) -> Optional[dict]:
    """从 graph 返回值里取出 interrupt payload（不同版本字段略有差异）。"""
    if result is None:
        return None
    # 常见：{"__interrupt__": [Interrupt(value=...)]}
    if isinstance(result, dict):
        inter = result.get("__interrupt__")
        if inter:
            first = inter[0]
            val = getattr(first, "value", None)
            if val is None and isinstance(first, dict):
                val = first.get("value")
            if isinstance(val, dict):
                return val
            return {"prompt": str(val), "questions": []}
    return None


# 图节点 → SSE status.phase（gate_user / pe_gate 过快，不推）
_NODE_PHASE: Dict[str, str] = {
    "load_context": "preparing",
    "react_agent": "thinking",
    "plan": "planning",
    "tools_exec": "acting",
    "review": "reviewing",
    "finalize": "generating",
    "ask_user": "waiting_user",
}


def _tool_names_from_state(state: Any) -> List[str]:
    """从即将进入 tools_exec 的 state 里抽出本批工具名（ReAct tool_calls 或 PE plan）。"""
    if not isinstance(state, dict):
        return []
    names: List[str] = []
    msgs = list(state.get("messages") or [])
    if msgs:
        from langchain_core.messages import AIMessage

        last = msgs[-1]
        if isinstance(last, AIMessage):
            for tc in getattr(last, "tool_calls", None) or []:
                n = ""
                if isinstance(tc, dict):
                    n = str(tc.get("name") or "")
                else:
                    n = str(getattr(tc, "name", "") or "")
                if n and n not in names:
                    names.append(n)
    if names:
        return names
    plan = state.get("plan")
    if isinstance(plan, dict):
        for step in plan.get("steps") or []:
            if not isinstance(step, dict):
                continue
            n = str(step.get("tool") or "").strip()
            if n and n not in names:
                names.append(n)
    return names


def _status_from_task_start(task: dict) -> Optional[Dict[str, Any]]:
    """tasks 流「节点开始」→ status 事件 data；结束事件返回 None。"""
    # start: {id, name, input, triggers}；end: {id, name, error, result, interrupts}
    if "result" in task or "error" in task:
        return None
    name = str(task.get("name") or "")
    phase = _NODE_PHASE.get(name)
    if not phase:
        return None
    data: Dict[str, Any] = {"phase": phase, "node": name}
    if name == "tools_exec":
        tools = _tool_names_from_state(task.get("input"))
        if tools:
            data["tools"] = tools
            if "rag_search" in tools:
                data["phase"] = "retrieving"
            detail = _tool_intent_detail(task.get("input"))
            if detail:
                data["detail"] = detail
    return data


def _msg_content(m: Any) -> str:
    c = getattr(m, "content", None)
    if c is None and isinstance(m, dict):
        c = m.get("content")
    if isinstance(c, list):
        # 部分网关返回 content blocks
        parts = []
        for block in c:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(block))
        return "".join(parts).strip()
    return str(c or "").strip()


def _tool_intent_detail(state: Any) -> str:
    """工具开始前：写出即将调用的工具与检索句。"""
    if not isinstance(state, dict):
        return ""
    from langchain_core.messages import AIMessage

    msgs = list(state.get("messages") or [])
    last = msgs[-1] if msgs else None
    parts: List[str] = []
    if isinstance(last, AIMessage):
        for tc in getattr(last, "tool_calls", None) or []:
            if isinstance(tc, dict):
                name = str(tc.get("name") or "")
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            else:
                name = str(getattr(tc, "name", "") or "")
                args = getattr(tc, "args", None)
                args = args if isinstance(args, dict) else {}
            if name == "rag_search":
                q = str(args.get("query") or "").strip()
                parts.append(f"正在检索：{q}" if q else "正在检索知识库…")
            elif name:
                parts.append(f"正在调用：{name}")
    return "\n".join(parts)


def _progress_from_task_end(task: dict) -> Optional[Dict[str, Any]]:
    """节点结束后推可读中间文字（思考摘要 / 检索预览），非终答 token 流。"""
    if "result" not in task:
        return None
    name = str(task.get("name") or "")
    update = task.get("result")
    if not isinstance(update, dict):
        return None

    from langchain_core.messages import AIMessage

    text = ""
    if name == "react_agent":
        parts: List[str] = []
        for m in list(update.get("messages") or []):
            if not isinstance(m, AIMessage):
                continue
            body = _msg_content(m)
            if body and not body.startswith("["):
                parts.append(body[:1200])
            for tc in getattr(m, "tool_calls", None) or []:
                if isinstance(tc, dict):
                    tname = str(tc.get("name") or "")
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                else:
                    tname = str(getattr(tc, "name", "") or "")
                    raw_args = getattr(tc, "args", None)
                    args = raw_args if isinstance(raw_args, dict) else {}
                if tname == "rag_search":
                    q = str(args.get("query") or "").strip()
                    parts.append(f"准备检索：{q}" if q else "准备检索知识库")
                elif tname:
                    parts.append(f"准备调用工具：{tname}")
        text = "\n".join(p for p in parts if p).strip()
    elif name == "tools_exec":
        # 单工具结果已在 tools_exec 内通过 StreamWriter 逐条推送；这里不再整包复读 JSON
        n = len(list(update.get("messages") or []) or list(update.get("step_results") or []))
        text = f"本批工具已完成（{n}）" if n else ""
    elif name == "plan":
        plan = update.get("plan")
        if isinstance(plan, dict):
            goal = str(plan.get("goal") or "").strip()
            n = len(plan.get("steps") or [])
            text = f"已规划 {n} 步" + (f"：{goal}" if goal else "")
    elif name == "review":
        fa = str(update.get("final_answer") or "").strip()
        if fa:
            text = f"复核完成，准备输出答案（约 {len(fa)} 字）"
        else:
            text = "复核中，可能需要重规划…"

    if not text:
        return None
    return {"text": text, "node": name}


async def _run_graph_with_progress(
    graph: Any,
    input_or_cmd: Any,
    config: dict,
) -> AsyncIterator[Dict[str, Any]]:
    """
    astream(tasks+values+custom)：
    - 节点开始 → status
    - 工具完成（StreamWriter）→ progress（逐条）
    - 节点结束 → progress（思考摘要等）
    - 最后 → _result
    """
    result: Any = None
    async for mode, chunk in graph.astream(
        input_or_cmd,
        config=config,
        stream_mode=["tasks", "values", "custom"],
    ):
        if mode == "values":
            result = chunk
            continue
        if mode == "custom":
            if isinstance(chunk, dict) and chunk.get("type") == "progress":
                text = str(chunk.get("text") or "").strip()
                if text:
                    yield {
                        "event": "progress",
                        "data": {
                            "text": text,
                            "node": chunk.get("node"),
                            "tool": chunk.get("tool"),
                        },
                    }
                    await asyncio.sleep(0)
            continue
        if mode != "tasks" or not isinstance(chunk, dict):
            continue
        start = _status_from_task_start(chunk)
        if start:
            yield {"event": "status", "data": start}
            await asyncio.sleep(0)
            # 工具开始时把检索句也写入 progress，避免长时间空白
            detail = start.get("detail")
            if detail:
                yield {
                    "event": "progress",
                    "data": {"text": str(detail), "node": start.get("node")},
                }
                await asyncio.sleep(0)
            continue
        prog = _progress_from_task_end(chunk)
        if prog:
            yield {"event": "progress", "data": prog}
            await asyncio.sleep(0)
    yield {"event": "_result", "data": result}


async def astream_reasoner(
    *,
    user_id: int,
    unit_id: int,
    query: str,
    session_id: Optional[str] = None,
    mode: RunMode = "react",
    resume: Optional[dict] = None,
    knowledge_base_id: Optional[int] = None,
    file_ids: Optional[List[int]] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
    agent_type: str = "reasoner",
    skip_ensure: bool = False,
    role_id: Optional[int] = None,
    force_skills: Optional[List[str]] = None,
    answer_mode: str = "deep",
) -> AsyncIterator[Dict[str, Any]]:
    """
    异步生成 SSE 事件字典：meta / status / delta / needs_input / done / error。

    - ``knowledge_base_id`` 非空 → 启用 ``rag_search``（本轮最多 2 次），``done.sources`` 带回切片。
    - ``answer_mode``：``quick`` 快速（baseline 检索 + 4 万 token，ReAct/QUICK 模型）；
      ``deep`` 深度（full 检索 + 8 万 token，ReAct；中间 FAST、收口 DEEP）。
    - ``force_skills``：强制并入技能（问数 Tab 传 ``["askdata"]``）。
    - ``role_id``：askdata 列权限需要；未传时工具侧按 viewer 兜底。
    - ``resume`` 非空时走 ``Command(resume=...)``。
    - ``skip_ensure=True``：调用方已建好会话（RAG / AskData router）。
    """
    from app.settings import get_settings as _gs

    request_id = rag_trace.new_request_id()
    t_metrics = __import__("time").perf_counter()
    forced = [str(s).strip() for s in (force_skills or []) if str(s).strip()]
    if agent_type == "askdata" and "askdata" not in forced:
        forced = ["askdata", *forced]
    am = (answer_mode or "deep").strip().lower()
    if am not in ("quick", "deep"):
        am = "deep"
    s_cfg = _gs()
    token_budget = (
        int(getattr(s_cfg, "reasoner_token_budget_quick", 40_000) or 40_000)
        if am == "quick"
        else int(getattr(s_cfg, "reasoner_token_budget", 80_000) or 80_000)
    )
    pipeline = (
        "rag_agent"
        if knowledge_base_id
        else ("askdata" if "askdata" in forced else "reasoner")
    )

    try:
        if skip_ensure:
            if not session_id:
                raise ValueError("skip_ensure 时必须提供 session_id")
            sid = session_id
        else:
            sid = ensure_session(
                user_id=user_id,
                unit_id=unit_id,
                session_id=session_id,
                knowledge_base_id=knowledge_base_id,
                agent_type=agent_type,
                title=(query or "")[:30] or None,
            )
    except ValueError as exc:
        from app.common.user_errors import public_error_message

        yield {
            "event": "error",
            "data": {"message": public_error_message(exc)},
        }
        return

    # 从会话行回填 KB（reasoner 续跑未传 kb 时）
    kb_id = knowledge_base_id
    if kb_id is None:
        row = chat_repo.get_session(sid)
        if row and row.get("knowledge_base_id"):
            kb_id = int(row["knowledge_base_id"])

    # 每轮请求清空工具上下文，避免 worker 复用污染
    from app.agents.askdata import tool_context as _ask_ctx
    from app.agents.rag.pipeline import rag_tool_context as _rag_ctx

    _rag_ctx._ctx.set(None)
    _ask_ctx._ctx.set(None)

    with rag_trace.rag_request(
        user_id=user_id, session_id=sid, request_id=request_id, pipeline=pipeline
    ):
        graph = get_compiled_graph()
        config = _thread_config(sid)
        # 显式 mode 优先；默认 deep/quick 均走 ReAct（deep 靠强弱模型 + full 检索区分）
        mode_l = str(mode or "react").lower()
        if mode_l in ("plan_execute", "plan", "pe"):
            run_mode: RunMode = "plan_execute"
        else:
            run_mode = "react"

        yield {
            "event": "meta",
            "data": {
                "session_id": sid,
                "mode": run_mode,
                "answer_mode": am,
                "token_budget": token_budget,
                "thread_id": sid,
                "request_id": request_id,
                "knowledge_base_id": kb_id,
                "agent_type": agent_type,
                "force_skills": forced,
                "cache_hit": False,
            },
        }

        user_query_for_persist = (query or "").strip()
        try:
            if resume:
                answers = resume.get("answers")
                if answers is None or answers == "":
                    answers = query
                user_query_for_persist = str(answers or "")
                rag_trace.set_run_meta(
                    mode=run_mode,
                    user_input=user_query_for_persist,
                    status="running",
                )
                graph_input: Any = Command(resume=answers)
            else:
                if not user_query_for_persist:
                    yield {
                        "event": "error",
                        "data": {"message": "query 不能为空"},
                    }
                    return
                rag_trace.set_run_meta(
                    mode=run_mode,
                    user_input=user_query_for_persist,
                    status="running",
                )
                init_skills: List[str] = []
                if kb_id:
                    init_skills.append("rag")
                for s in forced:
                    if s not in init_skills:
                        init_skills.append(s)
                # 新轮：先清掉同 thread 上轮 tool 轨迹；resume 走 Command，不经此分支。
                # 干净 System+短记忆+Human 由 load_context 重建。
                graph_input = {
                    "messages": [
                        RemoveMessage(id=REMOVE_ALL_MESSAGES),
                        HumanMessage(content=user_query_for_persist),
                    ],
                    "mode": run_mode,
                    "session_id": sid,
                    "user_id": user_id,
                    "unit_id": unit_id,
                    "role_id": int(role_id or 0),
                    "answer_mode": am,
                    "token_budget": token_budget,
                    "knowledge_base_id": kb_id,
                    "file_ids": file_ids,
                    "vector_weight": vector_weight,
                    "bm25_weight": bm25_weight,
                    "rag_chunk_hits": [],
                    "budget": empty_budget(),
                    "skill_ids": init_skills,
                    "force_skills": forced,
                    "step": 0,
                    "plan_round": 0,
                    "force_final": False,
                    "run_status": "running",
                    "final_answer": "",
                    "needs_user_questions": [],
                    "step_results": [],
                    "step_reviews": [],
                    "plan": None,
                }

            result: Any = None
            async for ev in _run_graph_with_progress(graph, graph_input, config):
                if ev.get("event") == "_result":
                    result = ev.get("data")
                else:
                    yield ev

            # interrupt？
            payload = _extract_interrupt_payload(result)
            if payload is None and isinstance(result, dict):
                # 有的版本 interrupt 时 result 仍是 state，另需 get_state
                snap = await graph.aget_state(config)
                tasks = getattr(snap, "tasks", None) or ()
                for task in tasks:
                    inters = getattr(task, "interrupts", None) or ()
                    for it in inters:
                        val = getattr(it, "value", None)
                        if isinstance(val, dict):
                            payload = val
                            break
                    if payload:
                        break

            if payload is not None:
                yield {"event": "status", "data": {"phase": "waiting_user"}}
                yield {
                    "event": "needs_input",
                    "data": {
                        "session_id": sid,
                        "questions": payload.get("questions") or [],
                        "prompt": payload.get("prompt")
                        or "请补充必要信息后继续。",
                        # resume_token：用 session_id 即可（checkpointer 线程）
                        "resume_token": sid,
                    },
                }
                yield {
                    "event": "done",
                    "data": {
                        "session_id": sid,
                        "status": "needs_input",
                        "answer": payload.get("prompt") or "",
                        "skill_ids": (result or {}).get("skill_ids")
                        if isinstance(result, dict)
                        else [],
                    },
                }
                metrics.incr("reasoner_requests_total", status="needs_input")
                return

            answer = ""
            skill_ids: List[str] = []
            rag_hits: List[Dict[str, Any]] = []
            if isinstance(result, dict):
                answer = str(result.get("final_answer") or "").strip()
                skill_ids = list(result.get("skill_ids") or [])
                rag_hits = list(result.get("rag_chunk_hits") or [])
                if not answer:
                    # 只取本轮 Human 之后的答，避免短记忆里的上轮 AI 冒充 final
                    from app.agents.reasoner.nodes import extract_turn_final_answer

                    answer = extract_turn_final_answer(
                        list(result.get("messages") or [])
                    )
            if not answer:
                answer = "（未能生成有效回答）"

            # 切片 → 前端 sources（与旧 RAG SSE 对齐；避免 import chain 循环）
            sources = _chunks_to_sources(rag_hits)

            yield {"event": "status", "data": {"phase": "generating"}}
            yield {"event": "delta", "data": {"content": answer}}
            # 先 done（含 sources），再双写：避免 MySQL/Redis 拖住前端 sending 解锁
            yield {
                "event": "done",
                "data": {
                    "session_id": sid,
                    "status": "ok",
                    "answer": answer,
                    "sources": sources,
                    "skill_ids": skill_ids,
                    "mode": run_mode,
                    "knowledge_base_id": kb_id,
                    "cache_hit": False,
                    "request_id": request_id,
                },
            }

            persist_query = user_query_for_persist or query or ""
            mem_scenario = "rag_qa" if kb_id else "reasoner"
            if persist_query:
                try:
                    # 双写含会话压缩 LLM：必须进线程，否则同步 HTTP 会堵死 uvicorn 事件循环
                    await asyncio.to_thread(
                        persist_turn,
                        session_id=sid,
                        query=persist_query,
                        answer=answer,
                        sources=sources or None,
                        metadata={
                            "request_id": request_id,
                            "skill_ids": skill_ids,
                            "rag_search_calls": int(
                                (result or {}).get("budget", {}).get(
                                    "rag_search_calls"
                                )
                                or 0
                            )
                            if isinstance(result, dict)
                            else 0,
                        },
                    )
                    maybe_enqueue_memory(
                        session_id=sid,
                        user_id=user_id,
                        unit_id=unit_id,
                        scenario=mem_scenario,
                    )
                except Exception:
                    logger.exception(
                        "persist after done failed sid=%s (answer already streamed)",
                        sid,
                    )

            metrics.incr("reasoner_requests_total", status="ok")
            metrics.observe_latency(
                "reasoner_latency_ms",
                (__import__("time").perf_counter() - t_metrics) * 1000,
                mode=run_mode,
            )
        except Exception as exc:
            from app.common.user_errors import log_and_public

            public_msg = log_and_public(
                logger,
                exc,
                where="reasoner_astream",
                extra={"session_id": sid},
            )
            rag_trace.set_run_meta(
                status="error", error=f"{type(exc).__name__}: {exc}"
            )
            metrics.incr("reasoner_requests_total", status="error")
            yield {
                "event": "error",
                "data": {"message": public_msg, "session_id": sid},
            }
