"""
工具执行运行时：ReAct 并行 + Plan-Execute ``depends_on`` 分层。

为何不用裸 ToolNode 撑全部场景
--------------------------------
官方 ``ToolNode`` 会对同一条 AIMessage 的 tool_calls 做 ``asyncio.gather`` 并行，但：
1. 没有 ``max_parallel`` 分批上限（与参考侧对齐）；
2. **不懂** plan 的 ``depends_on``（会把有依赖的步骤一次全并行）。

本模块 ``tools_exec``：
- **ReAct**：最后一条 AIMessage.tool_calls → 分批并行；
- **PE**：按 depends_on **同层并行、层间串行**；上游成功输出注入下游。

user_required 问人放在图节点 ``gate_user`` / ``pe_gate``（可 interrupt），
本文件只负责执行，避免节点中途 interrupt 导致 step 进度难落盘。

``step_results``：供 ``review_node`` 按步骤对照。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from langchain_core.messages import AIMessage, ToolMessage

from app.agents.askdata.tool_context import (
    AskdataToolContext,
    bind as bind_askdata_ctx,
    get as get_askdata_ctx,
)
from app.agents.rag.pipeline.rag_tool_context import (
    RagToolContext,
    bind as bind_rag_ctx,
    get as get_rag_ctx,
)
from app.agents.reasoner.budget import add_rag_search, add_web_search
from app.agents.reasoner.state import ReasonerState, empty_budget
from app.agents.reasoner.tools import build_tools_for_skills
from app.common import tracing as rag_trace
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _bind_rag_tool_context(state: ReasonerState):
    """把会话 KB / 权重注入 contextvar，供 rag_search 读取。"""
    kb = state.get("knowledge_base_id")
    if not kb:
        return None
    existing = get_rag_ctx()
    # 同轮多次 tools_exec：复用已有 call_count / accumulated_chunks
    if existing is not None and int(existing.knowledge_base_id) == int(kb):
        return None
    max_calls = int(getattr(get_settings(), "reasoner_rag_search_max", 2) or 2)
    prior_calls = int((state.get("budget") or {}).get("rag_search_calls") or 0)
    prior_hits = list(state.get("rag_chunk_hits") or [])
    # quick → baseline；deep / 默认 → full
    am = str(state.get("answer_mode") or "deep").strip().lower()
    retrieve_pipeline = "baseline" if am == "quick" else "full"
    ctx = RagToolContext(
        unit_id=int(state.get("unit_id") or 0),
        knowledge_base_id=int(kb),
        file_ids=list(state.get("file_ids") or []) or None,
        vector_weight=state.get("vector_weight"),
        bm25_weight=state.get("bm25_weight"),
        retrieve_pipeline=retrieve_pipeline,
        call_count=prior_calls,
        max_calls=max_calls,
        accumulated_chunks=prior_hits,
    )
    return bind_rag_ctx(ctx)


def _bind_askdata_tool_context(state: ReasonerState):
    """
    把 user/role/unit 注入 askdata contextvar，供问数工具读取。

    仅当本轮 skill 含 askdata 时绑定；同轮多跳复用已有 ctx（保留 events）。
    """
    skills = {str(s) for s in (state.get("skill_ids") or [])}
    force = {str(s) for s in (state.get("force_skills") or [])}
    if "askdata" not in skills and "askdata" not in force:
        return None
    existing = get_askdata_ctx()
    uid = int(state.get("user_id") or 0)
    if existing is not None and int(existing.user.get("id") or 0) == uid:
        return None
    max_rows = int(getattr(get_settings(), "askdata_max_rows", 200) or 200)
    user = {
        "id": uid,
        "unit_id": int(state.get("unit_id") or 0),
        "role_id": int(state.get("role_id") or 0),
    }
    ctx = AskdataToolContext(
        user=user,
        session_id=state.get("session_id"),
        max_rows=max_rows,
        events=list(existing.events) if existing is not None else [],
    )
    return bind_askdata_ctx(ctx)


def _bind_tool_contexts(state: ReasonerState) -> None:
    """进入工具批前绑定 rag / askdata 上下文（可并存）。"""
    _bind_rag_tool_context(state)
    _bind_askdata_tool_context(state)


def _budget_and_chunks_from_tools(
    state: ReasonerState, step_results: List[Dict[str, Any]]
) -> dict:
    budget = dict(state.get("budget") or empty_budget())
    for meta in step_results:
        if not meta.get("ok"):
            continue
        if meta.get("tool") == "web_search":
            budget = add_web_search(budget)  # type: ignore[arg-type]
        elif meta.get("tool") == "rag_search":
            budget = add_rag_search(budget)  # type: ignore[arg-type]
    out: dict = {"budget": budget}
    ctx = get_rag_ctx()
    if ctx is not None:
        out["rag_chunk_hits"] = list(ctx.accumulated_chunks)
    return out


def _max_parallel() -> int:
    return max(1, int(getattr(get_settings(), "reasoner_max_parallel_tools", 5) or 5))


def _chunked(items: List[Any], size: int) -> List[List[Any]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def _tool_map(skill_ids: Optional[List[str]]) -> Dict[str, Any]:
    tools = build_tools_for_skills(skill_ids or [])
    return {t.name: t for t in tools}


def _looks_failed(content: str) -> bool:
    c = (content or "").lower()
    if '"ok": false' in c or '"ok":false' in c:
        return True
    return any(
        x in c
        for x in ("工具执行失败", "工具暂时不可用", "熔断", "circuit", "未知工具")
    )


async def _run_one_tool(
    tool_obj: Any, *, name: str, call_id: str, args: dict
) -> ToolMessage:
    try:
        if hasattr(tool_obj, "ainvoke"):
            raw = await tool_obj.ainvoke(args or {})
        else:
            raw = await asyncio.to_thread(tool_obj.invoke, args or {})
        content = raw if isinstance(raw, str) else str(raw)
    except Exception as exc:
        logger.exception("tool_exec failed name=%s id=%s", name, call_id)
        content = f"工具执行失败：{type(exc).__name__}: {exc}"
    return ToolMessage(content=content, tool_call_id=call_id, name=name)


def inject_upstream_args(
    step_input: dict,
    depends_on: Sequence[str],
    done: Dict[str, Dict[str, Any]],
) -> dict:
    """depends_on 保序；把上游成功 content 写入 ``_upstream`` / 必要时 ``context``。"""
    out = dict(step_input or {})
    upstream: Dict[str, str] = {}
    last_ok = ""
    for dep in depends_on:
        r = done.get(dep) or {}
        if r.get("ok"):
            text = str(r.get("content") or "")[:2000]
            upstream[dep] = text
            last_ok = text
    if upstream:
        out["_upstream"] = upstream
        business_keys = [k for k in out if not str(k).startswith("_")]
        if not business_keys and last_ok:
            out["context"] = last_ok
    return out


# 这些工具 schema 要求 start_date；规划器常漏写，依赖 get_time 却不填 input
_TOOLS_NEED_START_DATE = frozenset(
    {"get_weather", "get_culture", "search_stay", "plan_transport"}
)


def _is_empty_arg(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    return False


def _parse_local_date_from_get_time(content: str) -> Optional[str]:
    """从 get_time 返回串解析锚定日：local=YYYY-MM-DD... / utc=..."""
    text = content or ""
    m = re.search(r"local=(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"utc=(\d{4}-\d{2}-\d{2})", text)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else None


def _anchor_date_from_deps(
    depends_on: Sequence[str], done: Dict[str, Dict[str, Any]]
) -> Optional[str]:
    """优先取 depends_on 里成功的 get_time；否则扫全部已完成 get_time。"""
    for dep in depends_on:
        r = done.get(dep) or {}
        if r.get("ok") and str(r.get("tool") or "") == "get_time":
            d = _parse_local_date_from_get_time(str(r.get("content") or ""))
            if d:
                return d
    for r in done.values():
        if r.get("ok") and str(r.get("tool") or "") == "get_time":
            d = _parse_local_date_from_get_time(str(r.get("content") or ""))
            if d:
                return d
    return None


def enrich_args_from_upstream(
    tool_name: str,
    args: dict,
    depends_on: Sequence[str],
    done: Dict[str, Dict[str, Any]],
) -> dict:
    """
    调用 StructuredTool 前补齐常见缺口，避免 ValidationError。

    当前：缺 start_date 时从上游 get_time 的 local 日期回填（「今天」锚点）。
    相对日期（下周一等）仍应由规划器算进 input；此处只兜底防崩。
    """
    out = dict(args or {})
    name = (tool_name or "").strip()
    if name in _TOOLS_NEED_START_DATE and _is_empty_arg(out.get("start_date")):
        anchor = _anchor_date_from_deps(depends_on, done)
        if anchor:
            out["start_date"] = anchor
            logger.info(
                "enrich start_date tool=%s from get_time -> %s", name, anchor
            )
    # StructuredTool 不认内部字段；调用前去掉，避免个别 schema extra=forbid
    out.pop("_upstream", None)
    return out


def topological_layers(steps: List[dict]) -> List[List[dict]]:
    """按 depends_on 分层；环依赖时剩余步骤作为最后一层交给执行侧标失败。"""
    remaining = {
        str(s.get("id") or f"s{i}"): dict(s, id=str(s.get("id") or f"s{i}"))
        for i, s in enumerate(steps)
        if isinstance(s, dict)
    }
    for s in remaining.values():
        deps = s.get("depends_on") or s.get("deps") or []
        if not isinstance(deps, list):
            deps = []
        s["depends_on"] = [str(d) for d in deps]

    layers: List[List[dict]] = []
    done_ids: set[str] = set()
    safety = 0
    n = len(remaining)
    while remaining:
        safety += 1
        if safety > n + 3:
            layers.append(list(remaining.values()))
            break
        ready = [
            s
            for s in remaining.values()
            if all(d in done_ids for d in (s.get("depends_on") or []))
        ]
        if not ready:
            layers.append(list(remaining.values()))
            break
        layers.append(ready)
        for s in ready:
            done_ids.add(s["id"])
            remaining.pop(s["id"], None)
    return layers


async def _run_calls_parallel(
    calls: List[Tuple[str, str, dict]],
    tool_by_name: Dict[str, Any],
    *,
    layer_idx: int,
) -> Tuple[List[ToolMessage], List[Dict[str, Any]]]:
    messages: List[ToolMessage] = []
    results: List[Dict[str, Any]] = []
    mp = _max_parallel()

    async def _one(cid: str, name: str, args: dict) -> Tuple[ToolMessage, dict]:
        tool_obj = tool_by_name.get(name)
        if tool_obj is None:
            tm = ToolMessage(
                content=f"未知工具: {name}", tool_call_id=cid, name=name
            )
            return tm, {
                "id": cid,
                "tool": name,
                "ok": False,
                "content": tm.content,
                "layer": layer_idx,
                "error": "UnknownTool",
                "input": args,
            }
        tm = await _run_one_tool(tool_obj, name=name, call_id=cid, args=args)
        ok = not _looks_failed(str(tm.content or ""))
        return tm, {
            "id": cid,
            "tool": name,
            "ok": ok,
            # 全文留给 review 逐步表；截断会丢掉 chunks 正文
            "content": str(tm.content or ""),
            "layer": layer_idx,
            "error": "" if ok else "ToolFailed",
            "input": args,
        }

    def _emit_done(meta: dict) -> None:
        from app.agents.reasoner.progress_text import (
            emit_custom_progress,
            summarize_tool_observation,
        )

        text = summarize_tool_observation(
            str(meta.get("tool") or ""), str(meta.get("content") or "")
        )
        emit_custom_progress(
            text, node="tools_exec", tool=str(meta.get("tool") or "")
        )

    tool_names = [name for _, name, _ in calls]
    with rag_trace.span(
        "tools.parallel",
        agent="reasoner",
        count=len(calls),
        layer=layer_idx,
        tools=tool_names,
    ) as tr:
        logger.info(
            "tools_parallel layer=%s calls=%s tools=%s max_parallel=%s",
            layer_idx,
            len(calls),
            tool_names,
            mp,
        )
        for batch in _chunked(calls, mp):
            # as_completed：哪个先完先推 progress，避免整批结束才一次蹦出
            futs = [
                asyncio.create_task(_one(cid, name, args))
                for cid, name, args in batch
            ]
            for fut in asyncio.as_completed(futs):
                tm, meta = await fut
                messages.append(tm)
                results.append(meta)
                try:
                    _emit_done(meta)
                except Exception:
                    logger.debug("emit tool progress failed", exc_info=True)
        ok_n = sum(1 for m in results if m.get("ok"))
        fail_n = len(results) - ok_n
        tr["ok_steps"] = ok_n
        tr["fail_steps"] = fail_n
        # 失败工具名，便于一眼看出 ValidationError 是哪几个
        tr["failed_tools"] = [
            str(m.get("tool") or "")
            for m in results
            if not m.get("ok")
        ]
        def _result_for_transcript(m: dict) -> dict:
            content = str(m.get("content") or "")
            circuit = ("熔断" in content) or ("circuit" in content.lower())
            args = m.get("input") if isinstance(m.get("input"), dict) else {}
            slim_args: dict = {}
            for k, v in (args or {}).items():
                if isinstance(v, str) and len(v) > 800:
                    slim_args[str(k)] = v[:800] + "…"
                else:
                    slim_args[str(k)] = v
            # transcript 保留更长结果；结构化摘要优先
            preview = rag_trace.truncate_text(content, 2000)
            item = {
                "id": m.get("id"),
                "tool": m.get("tool"),
                "ok": bool(m.get("ok")),
                "args": slim_args,
                "content": preview,
            }
            if circuit:
                item["circuit"] = True
            # askdata 常见字段：从 JSON 抽出便于扫读
            if content.lstrip().startswith("{"):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict):
                        for key in (
                            "ok",
                            "denied",
                            "row_count",
                            "has_data",
                            "next",
                            "error",
                            "stage",
                            "route",
                        ):
                            if key in parsed:
                                item[key] = parsed[key]
                        rows = parsed.get("rows")
                        if isinstance(rows, list):
                            item["rows_preview"] = rows[:5]
                            item["row_count"] = parsed.get("row_count", len(rows))
                except Exception:
                    pass
            return item

        rag_trace.append_phase(
            "tools_exec",
            node="tools_exec",
            layer=layer_idx,
            tools=tool_names,
            ok_steps=ok_n,
            fail_steps=fail_n,
            failed_tools=list(tr["failed_tools"]),
            results=[_result_for_transcript(m) for m in results],
        )
    return messages, results


async def run_react_tools(state: ReasonerState) -> dict:
    """ReAct：tool_calls 分批并行（带 max_parallel）。"""
    msgs = list(state.get("messages") or [])
    last = msgs[-1] if msgs else None
    if not isinstance(last, AIMessage):
        return {"messages": []}
    raw_calls = getattr(last, "tool_calls", None) or []
    calls: List[Tuple[str, str, dict]] = []
    for i, tc in enumerate(raw_calls):
        if isinstance(tc, dict):
            name = str(tc.get("name") or "")
            cid = str(tc.get("id") or f"call_{i}")
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
        else:
            name = str(getattr(tc, "name", "") or "")
            cid = str(getattr(tc, "id", "") or f"call_{i}")
            args = getattr(tc, "args", None) or {}
            if not isinstance(args, dict):
                args = {}
        if name:
            calls.append((cid, name, args))
    if not calls:
        return {"messages": []}

    _bind_tool_contexts(state)
    tool_by_name = _tool_map(list(state.get("skill_ids") or []))
    tool_msgs, step_results = await _run_calls_parallel(
        calls, tool_by_name, layer_idx=0
    )
    extra = _budget_and_chunks_from_tools(state, step_results)
    return {
        "messages": tool_msgs,
        "step_results": step_results,
        **extra,
    }


async def run_plan_layered(state: ReasonerState) -> dict:
    """PE：depends_on 分层；同层并行；层间串行 + 上游注入。"""
    plan = state.get("plan") or {}
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not steps:
        return {"messages": [], "step_results": []}

    done: Dict[str, Dict[str, Any]] = {}
    tool_by_name = _tool_map(list(state.get("skill_ids") or []))
    layers = topological_layers([s for s in steps if isinstance(s, dict)])
    new_msgs: List[ToolMessage] = []
    _bind_tool_contexts(state)

    with rag_trace.span(
        "tools.plan", agent="reasoner", steps=len(steps), layers=len(layers)
    ):
        logger.info(
            "plan_layered layers=%s step_ids=%s",
            len(layers),
            [s.get("id") for s in steps if isinstance(s, dict)],
        )
        for layer_idx, layer in enumerate(layers):
            calls: List[Tuple[str, str, dict]] = []
            for s in layer:
                sid = s["id"]
                deps = list(s.get("depends_on") or [])
                failed_deps = [
                    d for d in deps if d in done and not done[d].get("ok")
                ]
                # 拓扑保证：进本层前依赖应已在 done；否则为环/坏依赖残留
                unresolved = [d for d in deps if d not in done]
                if failed_deps or unresolved:
                    meta = {
                        "id": sid,
                        "tool": str(s.get("tool") or ""),
                        "ok": False,
                        "content": (
                            f"依赖未就绪/失败: failed={failed_deps} "
                            f"unresolved={unresolved}"
                        ),
                        "layer": layer_idx,
                        "error": "DependencyError",
                        "input": s.get("input") or {},
                    }
                    done[sid] = meta
                    new_msgs.append(
                        ToolMessage(
                            content=meta["content"],
                            tool_call_id=sid,
                            name=meta["tool"] or "unknown",
                        )
                    )
                    continue

                raw_input = (
                    s.get("input") if isinstance(s.get("input"), dict) else {}
                )
                args = inject_upstream_args(raw_input, deps, done)
                name = str(s.get("tool") or "").strip()
                if not name:
                    meta = {
                        "id": sid,
                        "tool": "",
                        "ok": False,
                        "content": "步骤缺少 tool 名",
                        "layer": layer_idx,
                        "error": "InvalidStep",
                        "input": args,
                    }
                    done[sid] = meta
                    new_msgs.append(
                        ToolMessage(
                            content=meta["content"],
                            tool_call_id=sid,
                            name="unknown",
                        )
                    )
                    continue
                # 规划器常漏 start_date：从 get_time 上游回填，避免 Pydantic Field required
                args = enrich_args_from_upstream(name, args, deps, done)
                calls.append((sid, name, args))

            if not calls:
                continue

            tool_msgs, step_results = await _run_calls_parallel(
                calls, tool_by_name, layer_idx=layer_idx
            )
            new_msgs.extend(tool_msgs)
            for meta in step_results:
                done[meta["id"]] = meta

    extra = _budget_and_chunks_from_tools(state, list(done.values()))
    return {
        "messages": new_msgs,
        "step_results": list(done.values()),
        **extra,
    }


async def tools_exec(state: ReasonerState) -> dict:
    """图节点：按 mode 分流；进入前绑定 rag / askdata 工具上下文。"""
    _bind_tool_contexts(state)
    mode = (state.get("mode") or "react").strip().lower()
    if mode in ("plan_execute", "plan", "pe"):
        return await run_plan_layered(state)
    return await run_react_tools(state)
    # 不在此处 reset：同轮可能 agent→tools→agent→tools；ctx 需跨跳累计。
    # 新用户提问在 astream 入口清空 askdata/rag contextvar。
