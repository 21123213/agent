"""检索问句变式扩展。"""
from __future__ import annotations

import json
import re
import time
from typing import List

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import Purpose, invoke_chat
from app.common.llm_text import normalize_llm_text
from app.common import tracing as rag_trace
from app.settings import get_settings


def _parse_json_list(text: str) -> List[str]:
    text = text.strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        if isinstance(data, dict) and "queries" in data:
            return [str(x).strip() for x in data["queries"] if str(x).strip()]
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            data = json.loads(match.group(0))
            return [str(x).strip() for x in data if str(x).strip()]
    return []


def expand_queries(base_query: str, n: int | None = None) -> List[str]:
    s = get_settings()
    if n is None:
        n = s.query_expand_n
    base_query = (base_query or "").strip()
    if not base_query:
        rag_trace.emit("query_expand", data={"expanded_queries": [], "n": n})
        return []
    if n <= 1:
        result = [base_query]
        rag_trace.emit(
            "query_expand",
            data={
                "base_query": rag_trace.truncate_text(base_query),
                "expanded_queries": result,
                "n": n,
                "skipped_llm": True,
            },
        )
        return result

    t0 = time.perf_counter()
    extra = n - 1
    system = SystemMessage(
        content=(
            "你是检索查询扩展助手。根据用户问题生成适合向量/关键词检索的同义或子问题变式。"
            "只输出 JSON 数组，不要解释。"
        )
    )
    human = HumanMessage(
        content=f"""原问题:
{base_query}

请再生成 {extra} 个语义相关、适合检索的变式问句（中文或英文与原问题一致）。
输出格式: ["变式1", "变式2", ...]
不要重复原句。"""
    )

    fallback = False
    variants: List[str] = []
    try:
        text = invoke_chat(
            [system, human], purpose=Purpose.FAST, temperature=0.3, reason="step"
        )
        variants = _parse_json_list(normalize_llm_text(text))
    except Exception as e:
        fallback = True
        rag_trace.emit(
            "query_expand",
            status="error",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "base_query": rag_trace.truncate_text(base_query),
                "expanded_queries": [base_query],
                "n": n,
                "fallback": True,
            },
            message=str(e),
            error_type=type(e).__name__,
        )

    seen = {base_query.lower()}
    result = [base_query]
    for v in variants:
        key = v.lower()
        if key not in seen and len(result) < n:
            seen.add(key)
            result.append(v)
    result = result[:n]
    if not fallback:
        rag_trace.emit(
            "query_expand",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "base_query": rag_trace.truncate_text(base_query),
                "expanded_queries": [rag_trace.truncate_text(q) for q in result],
                "n": n,
                "fallback": False,
            },
        )
    return result
