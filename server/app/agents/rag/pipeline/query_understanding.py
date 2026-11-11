"""多轮对话：独立检索问句改写（经 llm_client）。

原则：问句已完整、无指代不清时，不改原问题（尤其单轮评测/完整题干）。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import Purpose, invoke_chat
from app.common.llm_text import normalize_llm_text
from app.common import tracing as rag_trace

# 强依赖上文的指代/省略（即使无历史也可能需要「保持原样或保守处理」）
_STRONG_ANAPHORA = re.compile(
    r"(?i)"
    r"(上面|刚才|前述|上述|同上|后者|前者|"
    r"\b(as above|same as before|the previous (one|question|answer))\b|"
    r"^(那|这)(个|么|样|呢)?[？?！!。.]*$|"
    r"^\s*(what about|how about)\s+(it|that|this)\b|"
    r"\b(it|that|this)\s+(one|document|agreement|file)\b|"
    r"^(它|这个|那个)(呢|呢？|？|\?)?$)"
)


def _format_history(context: List[Dict[str, Any]], max_turns: int = 5) -> str:
    if not context:
        return "（无历史对话）"
    lines: List[str] = []
    for msg in context[-max_turns * 2 :]:
        role = msg.get("role", "user")
        content = (msg.get("content") or "")[:800]
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _parse_json_block(text: str) -> Dict[str, Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            return json.loads(match.group(0))
        raise


def _looks_complete_without_history(user_input: str) -> bool:
    """
    无多轮历史时：已是完整问句且无明显「依赖上文」指代 → 不改写。
    ContractNLI 类「Consider X; Does the document ...?」会走这里。
    """
    text = (user_input or "").strip()
    if not text:
        return True
    if _STRONG_ANAPHORA.search(text):
        return False
    # 带问号，或 Consider/关于…; 双段题干，或足够长的陈述问句
    if "?" in text or "？" in text:
        return len(text) >= 12
    if ";" in text or "；" in text:
        return len(text) >= 20
    if re.match(r"(?i)^(consider|regarding|about|根据|关于|针对)\b", text):
        return len(text) >= 20
    return len(text) >= 40


def _should_skip_rewrite(
    user_input: str, context: Optional[List[Dict[str, Any]]]
) -> bool:
    if context:
        return False
    return _looks_complete_without_history(user_input)


def understand_query(
    user_input: str,
    context: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    context = context or []
    user_input = (user_input or "").strip()
    t0 = time.perf_counter()

    if _should_skip_rewrite(user_input, context):
        result = {
            "intent": "qa",
            "standalone_query": user_input,
            "reply_hint": "",
        }
        rag_trace.emit(
            "query_understanding",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "user_input": rag_trace.truncate_text(user_input),
                "standalone_query": rag_trace.truncate_text(user_input),
                "rewrote": False,
                "skipped_llm": True,
                "reason": "already_complete",
                "fallback": False,
            },
        )
        return result

    history = _format_history(context)
    system = SystemMessage(
        content=(
            "你是 RAG 问答系统的查询改写模块。"
            "仅当当前输入依赖对话历史、存在指代不清或省略时，才改写成一句可独立检索的完整问句。"
            "若当前输入本身已是完整、可独立理解的问题，必须原样返回，禁止润色、合并分句或改写措辞。"
            "只输出 JSON，不要 markdown。不要判断意图，不要拒绝回答。"
        )
    )
    human = HumanMessage(
        content=f"""最近对话:
{history}

当前用户输入:
{user_input}

请输出 JSON:
{{
  "needs_rewrite": true或false,
  "standalone_query": "最终用于检索的问句"
}}

规则:
1. needs_rewrite=false：原文已完整、无指代不清 → standalone_query 必须与「当前用户输入」逐字相同
2. needs_rewrite=true：仅补全「它/这个/上面/刚才」等指代或明显省略；不要改变原意
3. 不要添加用户未提及的约束；不要回答问题本身
4. 不要把 "Consider X; Does the document ...?" 改成另一句同义问法
5. 保持与用户相同的语言（中文/英文）
"""
    )

    try:
        text = invoke_chat(
            [system, human], purpose=Purpose.FAST, temperature=0.1, reason="step"
        )
        data = _parse_json_block(normalize_llm_text(text))
        needs_rewrite = bool(data.get("needs_rewrite"))
        standalone = (data.get("standalone_query") or user_input).strip()
        # 模型声称不需要改写，或输出空 → 强制原文
        if not needs_rewrite or not standalone:
            standalone = user_input
        result = {
            "intent": "qa",
            "standalone_query": standalone or user_input,
            "reply_hint": "",
        }
        rag_trace.emit(
            "query_understanding",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "user_input": rag_trace.truncate_text(user_input),
                "standalone_query": rag_trace.truncate_text(result["standalone_query"]),
                "rewrote": result["standalone_query"] != user_input,
                "needs_rewrite": needs_rewrite,
                "skipped_llm": False,
                "fallback": False,
            },
        )
        return result
    except Exception as e:
        result = {
            "intent": "qa",
            "standalone_query": user_input,
            "reply_hint": "",
        }
        rag_trace.emit(
            "query_understanding",
            status="error",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "user_input": rag_trace.truncate_text(user_input),
                "standalone_query": rag_trace.truncate_text(result["standalone_query"]),
                "fallback": True,
            },
            message=str(e),
            error_type=type(e).__name__,
        )
        return result
