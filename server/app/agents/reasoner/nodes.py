"""
Reasoner 图节点实现。

节点一览
--------
- ``load_context``：短记忆 + 长期记忆 + skills → 拼初始 messages / system
- ``agent``（ReAct）：bind_tools 调 LLM；可能产出 tool_calls 或纯文本
- ``gate_user``：若 tool_calls 缺 user_required → 填 needs_user_questions
- ``ask_user``：``interrupt`` 等人；resume 后把补充写回 HumanMessage
- ``tools_exec``：见 ``tool_exec.py``（ReAct 并行 / PE depends_on 分层）
- ``pe_gate``：执行计划前扫描 user_required
- ``plan`` / ``review``：Plan-Execute；review **按步骤对照** plan
- ``finalize``：抽出最终答案、标 run_status

注释尽量写清「为什么」，方便对照 docs/reasoner-agent-plan.md。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.types import interrupt

from app.agents.rag.memory import user_memory as user_memory_mod
from app.agents.rag.memory.session_memory import enforce_memory_limits
from app.agents.reasoner.budget import (
    add_llm_usage,
    budget_config_from_state,
    maybe_soft_nudge,
    should_force_final,
    tool_quota_exhausted,
)
from app.agents.reasoner.param_policy import (
    find_user_required_gaps,
    find_user_required_gaps_in_plan,
)
from app.agents.reasoner.skills import (
    append_skills_to_system,
    load_and_merge_skills,
    merge_session_skills,
)
from app.agents.reasoner.skills.registry import SKILL_SPECS, load_skill_markdown
from app.agents.reasoner.skills.resolve import set_session_skills
from app.agents.reasoner.state import ReasonerState, empty_budget
from app.agents.reasoner.tools import build_tools_for_skills
from app.common.llm_client import (
    Purpose,
    get_chat,
    invoke_chat,
    purpose_for_answer_mode,
    resolve_model_for_purpose,
)
from app.common import tracing as rag_trace
from app.infra import chat_repo
from app.infra import redis_client as redis_mgr
from app.settings import get_settings

logger = logging.getLogger(__name__)

_PROMPTS = Path(__file__).resolve().parents[3] / "prompts" / "reasoner"


def _read_base_system() -> str:
    path = _PROMPTS / "base_system.md"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    return (
        "你是推理 Agent（reasoner）。优先使用工具获取事实，再给出简洁中文回答。"
        "不要编造工具结果。只有用户真正没提供、且无法推断的必要信息时才追问。"
    )


def _window_to_messages(window: List[Dict[str, Any]]) -> List[BaseMessage]:
    """把 Redis/MySQL 窗口 dict 转成 LC messages（跳过已在 system 注入的摘要可保留）。"""
    out: List[BaseMessage] = []
    for m in window:
        role = m.get("role")
        content = str(m.get("content") or "")
        if role == "user":
            out.append(HumanMessage(content=content))
        elif role == "assistant":
            out.append(AIMessage(content=content))
        elif role == "system":
            out.append(SystemMessage(content=content))
    return out


def load_context(state: ReasonerState) -> dict:
    """
    图入口（仅 START → 本节点；resume 补参不会再进这里）。

    每轮重建干净 ``messages``，避免 checkpointer 跨轮堆积上轮 tool 轨迹：
      RemoveAll
      + System（人设 + skills + 长期记忆）
      + 短记忆窗口（近 N 轮全文 / 更早摘要）
      + 本轮 Human

    上下文质量靠短记忆 + 长期记忆；本轮工具轨迹仍由后续节点写入 messages。
    """
    session_id = state.get("session_id") or ""
    user_id = int(state.get("user_id") or 0)
    unit_id = int(state.get("unit_id") or 0)
    query = ""
    # 从最后一条「非用户补充」的 HumanMessage 取本轮问题（agent 入口写入）
    for m in reversed(list(state.get("messages") or [])):
        if not isinstance(m, HumanMessage):
            continue
        text = str(m.content or "")
        if text.startswith("【用户补充】"):
            continue
        query = text
        break
    if not query:
        for m in reversed(list(state.get("messages") or [])):
            if isinstance(m, HumanMessage):
                query = str(m.content or "")
                break

    with rag_trace.span("load_context", agent="reasoner") as tr:
        skill_ids = load_and_merge_skills(session_id, query)
        # 会话绑了知识库 → 强制启用 rag skill（多 skill 可并存，如 travel+rag）
        if state.get("knowledge_base_id"):
            skill_ids = merge_session_skills(skill_ids, ["rag"])
            if session_id:
                set_session_skills(session_id, skill_ids)
        # Tab 问数 / 显式 force_skills → 强制 askdata（与 rag 同模式）
        force = [str(s).strip() for s in (state.get("force_skills") or []) if str(s).strip()]
        if force:
            skill_ids = merge_session_skills(skill_ids, force)
            if session_id:
                set_session_skills(session_id, skill_ids)
        base = _read_base_system()
        system_text = append_skills_to_system(base, skill_ids)

        # 长期记忆：绑 KB 时用 rag_qa，否则 reasoner → 拼进 System
        mem_scenario = "rag_qa" if state.get("knowledge_base_id") else "reasoner"
        mem_hits = user_memory_mod.retrieve_for_prompt(
            user_id=user_id,
            unit_id=unit_id,
            query=query or " ",
            scenario=mem_scenario,
        )
        mem_block = user_memory_mod.format_memories_for_prompt(mem_hits)
        if mem_block:
            system_text = system_text + "\n\n" + mem_block

        # 短记忆窗口：Redis 热路径，空则 MySQL + enforce（近 N 轮 + 摘要）
        window: List[Dict[str, Any]] = []
        try:
            window = redis_mgr.get_session_context(session_id) or []
        except Exception:
            window = []
        if not window:
            limit = get_settings().max_context_turns * 2 + 4
            rows = chat_repo.get_recent_messages(session_id, limit=limit)
            window = [
                {"role": r["role"], "content": r.get("content") or ""} for r in rows
            ]
            window = enforce_memory_limits(window)

        history_msgs = _window_to_messages(window)
        # 窗口偶发已含本轮 user（极少）时去重，避免 Human 双份
        if (
            query
            and history_msgs
            and isinstance(history_msgs[-1], HumanMessage)
            and str(history_msgs[-1].content or "") == query
        ):
            history_msgs = history_msgs[:-1]

        clean: List[BaseMessage] = [SystemMessage(content=system_text)]
        clean.extend(history_msgs)
        if query:
            clean.append(HumanMessage(content=query))

        tr.update(
            {
                "mode": state.get("mode") or "react",
                "answer_mode": state.get("answer_mode") or "deep",
                "skills": list(skill_ids),
                "kb_id": state.get("knowledge_base_id"),
                "hist_msgs": len(history_msgs),
                "mem_hits": len(mem_hits),
                "mem_scenario": mem_scenario,
                "query_preview": rag_trace.truncate_text(query),
                "note": "load_context 重建干净 messages；不调用 LLM",
            }
        )
        tools = build_tools_for_skills(skill_ids)
        tool_names = [getattr(t, "name", "") or "" for t in tools]
        rag_trace.set_run_meta(mode=str(state.get("mode") or "react"))
        rag_trace.append_phase(
            "load_context",
            node="load_context",
            mode=state.get("mode") or "react",
            answer_mode=state.get("answer_mode") or "deep",
            skills=list(skill_ids),
            hist_msgs=len(history_msgs),
            mem_hits=len(mem_hits),
            query_preview=rag_trace.truncate_text(query),
        )
        rag_trace.append_phase(
            "skills",
            node="load_context",
            skills=list(skill_ids),
            tools=[n for n in tool_names if n],
        )
        logger.info(
            "reasoner load_context sid=%s mode=%s skills=%s hist=%s mem=%s",
            session_id,
            state.get("mode"),
            skill_ids,
            len(history_msgs),
            len(mem_hits),
        )
        return {
            "skill_ids": skill_ids,
            "budget": state.get("budget") or empty_budget(),
            # 清空 checkpoint 旧轨迹后再写入干净窗口（resume 不会进本节点）
            "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *clean],
            "run_status": "running",
            "step": int(state.get("step") or 0),
            "force_final": False,
            "soft_hint": "",
        }


def agent_node(state: ReasonerState) -> dict:
    """
    ReAct 决策节点：LLM + bind_tools。

    - 硬闸 / force_final：不绑工具，只综合已有观察作答。
    - 软闸：把 soft_hint 拼进一条临时 SystemMessage。
    """
    cfg = budget_config_from_state(state)
    budget = state.get("budget") or empty_budget()
    force, reason = should_force_final(budget, cfg)
    force = force or bool(state.get("force_final"))
    max_steps = int(getattr(get_settings(), "reasoner_max_steps", 8) or 8)
    step = int(state.get("step") or 0)
    if step >= max_steps:
        force = True
        reason = reason or "max_steps"

    budget, soft_hint = maybe_soft_nudge(budget, cfg)
    skill_ids = list(state.get("skill_ids") or [])
    tools = build_tools_for_skills(skill_ids)

    messages: List[BaseMessage] = list(state.get("messages") or [])
    if soft_hint:
        messages = messages + [SystemMessage(content=soft_hint)]
    if force:
        messages = messages + [
            SystemMessage(
                content=(
                    f"【系统收口 reason={reason}】禁止再调用工具；"
                    "基于已有对话与工具观察给出完整中文最终回答。"
                )
            )
        ]

    with rag_trace.span("agent", agent="reasoner", force=force, step=step) as tr:
        # quick → QUICK(flash, 关思考) 全程
        # deep  → 中间 FAST(开思考)；收口/硬闸 DEEP(开思考)
        am_purpose = purpose_for_answer_mode(state.get("answer_mode"))
        if am_purpose == Purpose.QUICK:
            use_purpose = Purpose.QUICK
        elif force:
            use_purpose = Purpose.DEEP
        else:
            use_purpose = Purpose.FAST
        provider, model_id = resolve_model_for_purpose(use_purpose)
        llm = get_chat(use_purpose, temperature=0.3)
        if force or not tools:
            bound = llm
        else:
            bound = llm.bind_tools(tools)

        # 同步 invoke（未走 invoke_chat，故在此补齐 trace 字段）
        ai: AIMessage = bound.invoke(list(messages))  # type: ignore[assignment]
        usage_p = 0
        usage_c = 0
        meta = getattr(ai, "response_metadata", None) or {}
        tok = meta.get("token_usage") or meta.get("usage") or {}
        if isinstance(tok, dict):
            usage_p = int(tok.get("prompt_tokens") or tok.get("input_tokens") or 0)
            usage_c = int(
                tok.get("completion_tokens") or tok.get("output_tokens") or 0
            )
        if usage_p == 0:
            # 粗估：字符近似（仅用于软硬闸，非计费）
            usage_p = max(1, sum(len(str(getattr(m, "content", "") or "")) for m in messages) // 2)
            usage_c = max(1, len(str(ai.content or "")) // 2)
        budget = add_llm_usage(budget, usage_p, usage_c)

        raw_calls = getattr(ai, "tool_calls", None) or []
        call_names: List[str] = []
        call_details: List[Dict[str, Any]] = []
        for tc in raw_calls:
            if isinstance(tc, dict):
                name = str(tc.get("name") or "")
                cid = str(tc.get("id") or "")
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            else:
                name = str(getattr(tc, "name", "") or "")
                cid = str(getattr(tc, "id", "") or "")
                raw_args = getattr(tc, "args", None)
                args = raw_args if isinstance(raw_args, dict) else {}
            if name:
                call_names.append(name)
                # 入参截断：sql 等字段保留足够诊断信息
                slim_args: Dict[str, Any] = {}
                for k, v in (args or {}).items():
                    if isinstance(v, str) and len(v) > 800:
                        slim_args[str(k)] = v[:800] + "…"
                    else:
                        slim_args[str(k)] = v
                call_details.append({"name": name, "id": cid, "args": slim_args})
        tr.update(
            {
                "purpose": use_purpose.value,
                "provider": provider,
                "model": model_id,
                "answer_mode": state.get("answer_mode") or "deep",
                "mode": state.get("mode") or "react",
                "force": force,
                "force_reason": reason or "",
                "tool_calls": call_names,
                "prompt_tokens_est": usage_p,
                "completion_tokens_est": usage_c,
                "answer_preview": rag_trace.truncate_text(str(ai.content or "")),
            }
        )
        rag_trace.append_phase(
            "react_agent",
            node="react_agent",
            step=step,
            purpose=use_purpose.value,
            provider=provider,
            model=model_id,
            force=force,
            tool_calls=call_details,
            answer_preview=rag_trace.truncate_text(str(ai.content or ""), 500),
        )
        logger.info(
            "reasoner agent step=%s purpose=%s model=%s force=%s tool_calls=%s",
            step,
            use_purpose.value,
            model_id,
            force,
            call_names,
        )
        return {
            "messages": [ai],
            "budget": budget,
            "soft_hint": soft_hint,
            "force_final": force,
            "step": step + 1,
        }


def gate_user(state: ReasonerState) -> dict:
    """
    ToolNode 之前：只拦截 user_required 缺口。

    有缺口 → 写入 needs_user_questions，边路由到 ask_user；
    无缺口 → needs 清空，边路由到 tools（ToolNode 并行执行）。
    """
    msgs = list(state.get("messages") or [])
    last = msgs[-1] if msgs else None
    questions: List[Dict[str, Any]] = []
    if isinstance(last, AIMessage) and getattr(last, "tool_calls", None):
        questions = find_user_required_gaps(last)
    if questions:
        rag_trace.append_phase(
            "gate_user",
            node="gate_user",
            needs_input=True,
            questions=[
                {
                    "tool": q.get("tool"),
                    "field": q.get("field"),
                    "prompt": rag_trace.truncate_text(q.get("prompt") or "", 120),
                }
                for q in questions
                if isinstance(q, dict)
            ],
        )
    return {"needs_user_questions": questions}


def ask_user(state: ReasonerState) -> dict:
    """
    人工节点：调用 LangGraph ``interrupt``。

    行为
    ----
    1. 首次执行到此处：抛出/挂起 interrupt，payload 含 questions；
       checkpointer 把状态存 Redis；API 转成 SSE ``needs_input``。
    2. 客户端 ``Command(resume=用户补充文本)`` 后，本节点从 interrupt()
       **返回值**拿到补充内容，追加为 HumanMessage，清空 needs，继续图。

    注意：interrupt 所在节点会在 resume 时**整节点重跑**，因此 interrupt
    放在逻辑开头，副作用（写 messages）放在拿到 resume 值之后。
    """
    questions = list(state.get("needs_user_questions") or [])
    payload = {
        "phase": "needs_input",
        "questions": questions,
        "prompt": "；".join(q.get("prompt") or "" for q in questions)
        or "请补充必要信息后继续。",
        "session_id": state.get("session_id"),
    }
    logger.info(
        "reasoner ask_user interrupt sid=%s n_questions=%s",
        state.get("session_id"),
        len(questions),
    )
    rag_trace.append_phase(
        "needs_input",
        node="ask_user",
        prompt=rag_trace.truncate_text(payload.get("prompt") or "", 200),
        question_count=len(questions),
    )
    rag_trace.set_run_meta(status="needs_input")
    # resume 后，answers 为前端传入的字符串或 dict
    answers = interrupt(payload)
    text = ""
    if isinstance(answers, str):
        text = answers.strip()
    elif isinstance(answers, dict):
        text = str(
            answers.get("answers")
            or answers.get("text")
            or answers.get("query")
            or ""
        ).strip()
    else:
        text = str(answers or "").strip()

    if not text:
        text = "（用户未提供有效补充，请基于已有信息尽量继续或再次明确缺失项。）"

    return {
        "messages": [HumanMessage(content=f"【用户补充】{text}")],
        "needs_user_questions": [],
        "run_status": "running",
    }


def plan_node(state: ReasonerState) -> dict:
    """
    Plan-Execute：按可用工具产出完整 JSON 计划（可多步、带 depends_on）。

    计划格式::
        {"steps":[{"id":"s1","tool":"get_time","input":{},"depends_on":[]}], "goal":"..."}

    若解析失败，降级为空计划并 force 走 finalize 旁路（review 会处理）。
    """
    cfg = budget_config_from_state(state)
    budget = state.get("budget") or empty_budget()
    force, _ = should_force_final(budget, cfg)
    if force or state.get("force_final"):
        return {"force_final": True, "plan": None}

    skill_ids = list(state.get("skill_ids") or [])
    tools = build_tools_for_skills(skill_ids)
    tool_lines = []
    for t in tools:
        # 带上必填参数名，减少 PE 漏写 start_date 等（否则 StructuredTool 直接 ValidationError）
        hint = f"- {t.name}: {t.description}"
        schema = getattr(t, "args_schema", None)
        if schema is not None:
            try:
                js = schema.model_json_schema()
                req = list(js.get("required") or [])
                props = js.get("properties") or {}
                parts = []
                for k in props:
                    tag = "必填" if k in req else "可选"
                    parts.append(f"{k}({tag})")
                if parts:
                    hint += " | 参数: " + ", ".join(parts)
            except Exception:
                pass
        tool_lines.append(hint)

    user_q = ""
    for m in reversed(list(state.get("messages") or [])):
        if isinstance(m, HumanMessage):
            user_q = str(m.content or "")
            break

    # 规划器单独 system，默认吃不到 load_context 里的 SKILL.md；这里显式注入
    skill_blocks: List[str] = []
    for sid in skill_ids:
        body = load_skill_markdown(sid)
        if not body:
            continue
        title = SKILL_SPECS[sid].title if sid in SKILL_SPECS else sid
        skill_blocks.append(f"### 技能：{title}\n{body}")
    skill_section = (
        "\n已启用技能说明（规划时必须遵守其推荐工具顺序与约定）：\n"
        + "\n\n".join(skill_blocks)
        + "\n"
        if skill_blocks
        else ""
    )

    sys = (
        "你是规划器。根据用户目标与「可用工具」规划出完整、可执行的多步计划。"
        "输出严格 JSON 对象，不要 Markdown。\n"
        '格式：{"goal":"...", "steps":[{"id":"s1","tool":"工具名",'
        '"input":{...},"depends_on":["s0"]}]}\n'
        "规则：\n"
        "- id 唯一；depends_on 列出必须先完成的步骤 id（无依赖则 []）。\n"
        "- 有数据先后关系时必须写 depends_on，执行器会按层串行、同层并行。\n"
        "- **depends_on 只保证顺序，不会自动把上游字段写入下游 input**；"
        "每个步骤的 input 必须自行填齐该工具「必填」参数。\n"
        "- 相对日期（明天/本周四～周日/下周一）：先 get_time，再在同一份计划的"
        "后续步骤 input 里写入推算出的具体 YYYY-MM-DD"
        "（如 get_weather/get_culture 的 start_date）。\n"
        "- 无依赖的步骤可并行，不要伪造依赖。\n"
        "- **禁止**只规划「探测 1 步」就结束：须在同一次计划里写全达成 goal 所需的"
        "工具链（例如行程：get_time → web_search/天气文化 → build_route，"
        "需要时再 search_stay/plan_transport；知识库问答：足够的 rag_search 等）。\n"
        "- 只使用下方「可用工具」中的名称；能并行的不要强行串行；"
        "不要规划与用户问题无关的工具。\n"
        "- goal 用一句话写清最终要交付给用户的结果。\n"
        f"{skill_section}"
        "可用工具：\n" + "\n".join(tool_lines) + "\n"
    )
    budget2, soft = maybe_soft_nudge(budget, cfg)
    if soft:
        sys += "\n" + soft

    plan_purpose = purpose_for_answer_mode(state.get("answer_mode"))
    provider, model_id = resolve_model_for_purpose(plan_purpose)
    with rag_trace.span(
        "plan",
        agent="reasoner",
        purpose=plan_purpose.value,
        provider=provider,
        model=model_id,
        answer_mode=state.get("answer_mode") or "deep",
        mode="plan_execute",
    ) as tr:
        raw = invoke_chat(
            [SystemMessage(content=sys), HumanMessage(content=user_q or "请规划")],
            purpose=plan_purpose,
            temperature=0.2,
            reason="reasoner_plan",
        )
        plan: Optional[dict] = None
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].strip()
            plan = json.loads(text)
        except Exception as exc:
            logger.warning("plan parse failed: %s raw=%s", exc, raw[:200])
            plan = {"goal": user_q, "steps": []}

        steps = (plan or {}).get("steps") if isinstance(plan, dict) else []
        step_tools = [
            str(s.get("tool") or "")
            for s in (steps or [])
            if isinstance(s, dict)
        ]
        rag_trace.append_phase(
            "plan",
            node="plan",
            purpose=plan_purpose.value,
            provider=provider,
            model=model_id,
            goal=rag_trace.truncate_text(
                (plan or {}).get("goal") if isinstance(plan, dict) else "", 200
            ),
            steps=len(steps or []),
            tools=[t for t in step_tools if t],
        )
        tr.update(
            {
                "goal": (plan or {}).get("goal") if isinstance(plan, dict) else "",
                "steps": len(steps or []),
                "tools": [t for t in step_tools if t],
                "query_preview": rag_trace.truncate_text(user_q),
            }
        )
        # 粗估 token
        budget2 = add_llm_usage(budget2, max(1, len(sys + user_q) // 2), max(1, len(raw) // 2))
        round_i = int(state.get("plan_round") or 0) + 1
        return {
            "plan": plan,
            "plan_round": round_i,
            "budget": budget2,
            # 新一轮计划清空上轮步骤结果，避免 review 串台
            "step_results": [],
            "step_reviews": [],
            "messages": [
                AIMessage(
                    content=f"[plan]\n{json.dumps(plan, ensure_ascii=False)}"
                )
            ],
        }


def pe_gate(state: ReasonerState) -> dict:
    """
    PE 执行前：扫描整张 plan 的 user_required 缺口。

    有缺口 → needs_user_questions，边走到 ask_user；
    无缺口 → 清空，边走到 tools_exec（分层并行）。
    """
    questions = find_user_required_gaps_in_plan(state.get("plan"))
    return {"needs_user_questions": questions}


def pe_agent_from_plan(state: ReasonerState) -> dict:
    """
    兼容保留：旧路径把 plan 拍成一次 tool_calls。

    当前图已改为 ``tools_exec`` 直接读 plan 分层执行，默认不再经过本节点。
    """
    plan = state.get("plan") or {}
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not steps:
        return {
            "messages": [
                AIMessage(content="（无工具步骤）", tool_calls=[]),
            ]
        }
    tool_calls = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        name = str(step.get("tool") or "").strip()
        if not name:
            continue
        args = step.get("input") if isinstance(step.get("input"), dict) else {}
        tool_calls.append(
            {
                "id": str(step.get("id") or f"call_{i}"),
                "name": name,
                "args": args,
            }
        )
    return {
        "messages": [
            AIMessage(content="执行计划中的工具步骤", tool_calls=tool_calls)
        ]
    }


def _format_step_table_for_review(
    plan: dict | None, step_results: List[Dict[str, Any]]
) -> str:
    """把 plan.steps 与 step_results 对齐成给评审模型看的逐步表（结果全文，不截断）。"""
    by_id = {
        str(r.get("id")): r
        for r in (step_results or [])
        if isinstance(r, dict) and r.get("id") is not None
    }
    lines: List[str] = []
    steps = (plan or {}).get("steps") if isinstance(plan, dict) else None
    if not steps:
        # 无 plan 时退回纯结果列表
        for r in step_results or []:
            lines.append(
                f"- id={r.get('id')} tool={r.get('tool')} ok={r.get('ok')} "
                f"layer={r.get('layer')} err={r.get('error')} "
                f"out={r.get('content') or ''}"
            )
        return "\n".join(lines) or "（无步骤结果）"

    lines.append(f"goal: {(plan or {}).get('goal')}")
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        sid = str(s.get("id") or f"s{i}")
        r = by_id.get(sid)
        deps = s.get("depends_on") or []
        planned = json.dumps(s.get("input") or {}, ensure_ascii=False)
        if r is None:
            status = "missing_observation"
            detail = "计划有此步但未执行/无结果"
        elif r.get("ok"):
            status = "ok"
            detail = str(r.get("content") or "")
        else:
            status = "failed"
            detail = f"error={r.get('error')} {r.get('content') or ''}"
        lines.append(
            f"- step={sid} tool={s.get('tool')} depends_on={deps} "
            f"planned_input={planned} "
            f"status={status} detail={detail}"
        )
    # 计划外多出来的结果
    planned_ids = {
        str(s.get("id") or f"s{i}")
        for i, s in enumerate(steps)
        if isinstance(s, dict)
    }
    for sid, r in by_id.items():
        if sid not in planned_ids:
            lines.append(
                f"- step={sid} (extra) tool={r.get('tool')} "
                f"status={'ok' if r.get('ok') else 'failed'} "
                f"detail={r.get('content') or ''}"
            )
    return "\n".join(lines)


def _user_question_from_messages(messages: list) -> str:
    user_q = ""
    for m in reversed(list(messages or [])):
        if isinstance(m, HumanMessage) and not str(m.content or "").startswith(
            "【用户补充】"
        ):
            user_q = str(m.content or "")
            break
    if not user_q:
        for m in reversed(list(messages or [])):
            if isinstance(m, HumanMessage):
                user_q = str(m.content or "")
                break
    return user_q


def _synthesize_forced_final_answer(
    *,
    user_q: str,
    table: str,
    reason: str,
    answer_mode: str | None,
) -> str:
    """
    评审 LLM 失败时的兜底：仍调主模型写用户可读答案。
    禁止把逐步对照表 / 工具轨迹直接回给前端。
    """
    purpose = purpose_for_answer_mode(answer_mode)
    sys = (
        "你是智能问答助手。请基于已有检索/工具结果，"
        "直接用完整中文回答用户问题。\n"
        "硬性要求：\n"
        "1. 只输出给用户看的最终回答（可用 Markdown 小标题/列表）；\n"
        "2. 禁止输出 goal、step、planned_input、expanded_queries、tool=、status=ok、"
        "收口条件、逐步对照表等内部痕迹；\n"
        "3. 依据不足时明确说明缺什么，不要编造文档没有的规则；\n"
        f"4. 内部原因仅供你知悉：{reason or 'synth_fallback'}，不要写进回答。"
    )
    try:
        text = invoke_chat(
            [
                SystemMessage(content=sys),
                HumanMessage(
                    content=(
                        f"用户问题：{user_q or '（空）'}\n\n"
                        f"可用证据（内部，勿原文照抄结构）：\n{table}"
                    )
                ),
            ],
            purpose=purpose,
            temperature=0.3,
            reason="reasoner_force_final_synth",
        ).strip()
        if text and not text.startswith("已达收口条件"):
            return text
    except Exception:
        logger.exception("force final synthesize failed reason=%s", reason)
    return (
        "抱歉，当前未能完整整理出最终答复。请换个问法再试，"
        "或把问题拆成「积分怎么算」和「退款后积分怎么扣」分开问。"
    )


def review_node(state: ReasonerState) -> dict:
    """
    PE 评审：先按拆解步骤对照，再汇总 decision=final|replan。

    工具配额用尽：仍走本节点正常逐步评审，但禁止 replan（再搜也会被拒）。
    token 硬闸 / 配额用尽 / 重规划次数用尽 → 强制 decision=final。

    输出 JSON 约定::
        {
          "step_reviews": [{"id":"s1","status":"ok|failed|missing|skipped","note":"..."}],
          "decision": "final"|"replan",
          "final_answer": "...",
          "reason": "..."
        }
    """
    cfg = budget_config_from_state(state)
    budget = state.get("budget") or empty_budget()
    force, reason = should_force_final(budget, cfg)
    quota_hit, quota_reason = tool_quota_exhausted(budget, cfg)
    max_replans = int(getattr(get_settings(), "reasoner_max_replans", 2) or 2)
    round_i = int(state.get("plan_round") or 0)
    must_final = bool(force or quota_hit or round_i > max_replans)
    must_reason = (
        reason
        or quota_reason
        or ("max_replans" if round_i > max_replans else "")
    )
    table = _format_step_table_for_review(
        state.get("plan"), list(state.get("step_results") or [])
    )
    user_q = _user_question_from_messages(list(state.get("messages") or []))

    sys = (
        "你是逐步评审器。必须先对照「计划步骤 vs 执行结果」逐条评判，再给总决策。\n"
        "输出严格 JSON（不要 Markdown）：\n"
        "{\n"
        '  "step_reviews": [{"id":"步骤id","status":"ok|failed|missing|skipped","note":"一句中文"}],\n'
        '  "decision": "final" 或 "replan",\n'
        '  "final_answer": "若 final：给用户的完整中文回答；replan 可空",\n'
        '  "reason": "总评一句"\n'
        "}\n"
        "规则：关键步骤 failed/missing 且无法用其它步骤补齐 → 倾向 replan；"
        "信息已够回答用户 → final。不要假装跑过未执行的步骤。"
        "final_answer 必须是给终端用户的白话，禁止出现 goal/step/planned_input/"
        "expanded_queries/tool= 等内部字段。"
    )
    if must_final:
        sys += (
            f"\n【强制 final reason={must_reason}】必须 decision=final，"
            "基于已有步骤结果直接给出完整 final_answer；禁止 replan。"
        )

    review_purpose = purpose_for_answer_mode(state.get("answer_mode"))
    provider, model_id = resolve_model_for_purpose(review_purpose)
    step_results = list(state.get("step_results") or [])
    ok_n = sum(1 for r in step_results if isinstance(r, dict) and r.get("ok"))
    fail_n = sum(
        1 for r in step_results if isinstance(r, dict) and not r.get("ok")
    )
    with rag_trace.span(
        "review",
        agent="reasoner",
        purpose=review_purpose.value,
        provider=provider,
        model=model_id,
        answer_mode=state.get("answer_mode") or "deep",
        mode="plan_execute",
        ok_steps=ok_n,
        fail_steps=fail_n,
        must_final=must_final,
        must_reason=must_reason or "",
    ) as tr:
        try:
            raw = invoke_chat(
                [
                    SystemMessage(content=sys),
                    HumanMessage(
                        content=(
                            f"用户目标：{user_q}\n\n"
                            f"逐步对照表：\n{table}"
                        )
                    ),
                ],
                purpose=review_purpose,
                temperature=0.2,
                reason="reasoner_review",
            )
        except Exception:
            logger.exception("review invoke failed; fallback synthesize")
            final_answer = _synthesize_forced_final_answer(
                user_q=user_q,
                table=table,
                reason=must_reason or "review_invoke_failed",
                answer_mode=state.get("answer_mode"),
            )
            return {
                "force_final": True,
                "run_status": "ok",
                "final_answer": final_answer,
            }

        decision = "final"
        final_answer = ""
        step_reviews: List[Dict[str, Any]] = []
        try:
            text = raw.strip()
            if text.startswith("```"):
                text = text.strip("`")
                if text.startswith("json"):
                    text = text[4:].strip()
            data = json.loads(text)
            decision = str(data.get("decision") or "final").lower()
            final_answer = str(data.get("final_answer") or "").strip()
            sr = data.get("step_reviews")
            if isinstance(sr, list):
                step_reviews = [x for x in sr if isinstance(x, dict)]
        except Exception:
            decision = "final"
            final_answer = raw.strip()
            # 规则兜底：按 ok 生成逐步表
            for r in step_results:
                if not isinstance(r, dict):
                    continue
                step_reviews.append(
                    {
                        "id": r.get("id"),
                        "status": "ok" if r.get("ok") else "failed",
                        "note": "parse_fallback",
                    }
                )

        if must_final:
            decision = "final"

        tr.update(
            {
                "decision": decision,
                "step_reviews": len(step_reviews),
                "answer_preview": rag_trace.truncate_text(final_answer or raw),
            }
        )
        rag_trace.append_phase(
            "review",
            node="review",
            decision=decision,
            purpose=review_purpose.value,
            model=model_id,
            ok_steps=ok_n,
            fail_steps=fail_n,
            answer_preview=rag_trace.truncate_text(final_answer or raw),
        )
        budget = add_llm_usage(budget, 200, max(1, len(raw) // 2))
        logger.info(
            "review decision=%s model=%s step_reviews=%s ok=%s fail=%s must_final=%s reason=%s",
            decision,
            model_id,
            len(step_reviews),
            ok_n,
            fail_n,
            must_final,
            must_reason or "-",
        )

        if decision == "replan" and round_i <= max_replans and not must_final:
            return {
                "budget": budget,
                "force_final": False,
                "step_reviews": step_reviews,
                "messages": [
                    AIMessage(content=f"[review:replan] {raw[:400]}")
                ],
            }

        if not final_answer:
            final_answer = raw.strip() or "已根据现有步骤结果完成推理。"
        return {
            "budget": budget,
            "force_final": True,
            "final_answer": final_answer,
            "step_reviews": step_reviews,
            "messages": [AIMessage(content=final_answer)],
            "run_status": "ok",
        }


def extract_turn_final_answer(messages: list | None) -> str:
    """
    只从「最后一条 HumanMessage 之后」取本轮最终答。

    短记忆会把上轮 assistant 以 AIMessage 注入窗口；若本轮只有 tool_calls、
    没有正文答，旧逻辑会误把上轮答案当成 final（旅游问出积分答）。
    """
    msgs = list(messages or [])
    cut = 0
    for i, m in enumerate(msgs):
        if isinstance(m, HumanMessage):
            cut = i + 1
    for m in reversed(msgs[cut:]):
        if isinstance(m, AIMessage) and not (getattr(m, "tool_calls", None) or []):
            answer = str(m.content or "").strip()
            if (
                answer
                and not answer.startswith("[plan]")
                and not answer.startswith("[review")
            ):
                return answer
    return ""


def finalize(state: ReasonerState) -> dict:
    """从本轮最终 AIMessage（无 tool_calls）提取答案；不回落到短记忆里的上轮答。"""
    answer = (state.get("final_answer") or "").strip()
    if not answer:
        answer = extract_turn_final_answer(list(state.get("messages") or []))
    if not answer:
        answer = "（未能生成有效回答）"
    rag_trace.append_phase(
        "finalize",
        node="finalize",
        answer_preview=rag_trace.truncate_text(answer),
        answer_len=len(answer),
    )
    rag_trace.set_run_meta(status="ok")
    logger.info(
        "reasoner finalize sid=%s answer_len=%s",
        state.get("session_id"),
        len(answer),
    )
    return {"final_answer": answer, "run_status": "ok"}
