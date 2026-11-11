"""
基于检索切片生成答案（经 llm_client）。

支持：
- 同步 generate_answer（评测 / 非流式）
- 流式 generate_answer_stream（SSE delta）
- 可选 long_term_memory_block 注入（用户长期记忆段）
"""
from __future__ import annotations

import time
from typing import Any, Dict, Generator, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import Purpose, invoke_chat, stream_chat
from app.common import tracing as rag_trace


EMPTY_REPLY = (
    "抱歉，我没有在您的文档中找到相关信息。请确认已上传并处理完成相关文件。"
)


def _build_messages(
    query: str,
    context_docs: List[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
    long_term_memory_block: str = "",
) -> list:
    context_parts: List[str] = []
    for i, doc in enumerate(context_docs, start=1):
        content = doc.get("content") or doc.get("chunk_text") or ""
        meta = doc.get("metadata") or {}
        pages = meta.get("page_numbers") or []
        page_hint = f" (页码: {pages})" if pages else ""
        context_parts.append(f"--- 文档片段 {i}{page_hint} ---\n{content[:4000]}")
    context_block = "\n\n".join(context_parts)

    # Prompt 注入顺序：长期记忆 → 会话摘要/近轮（history 已含）→ 问题 + 文档
    history_lines: List[str] = []
    if history:
        for msg in history:
            role = msg.get("role", "user")
            text = msg.get("content") or ""
            history_lines.append(f"{role}: {text}")
    history_block = "\n".join(history_lines) if history_lines else "（无）"

    memory_section = ""
    if (long_term_memory_block or "").strip():
        memory_section = long_term_memory_block.strip() + "\n\n"

    system = SystemMessage(
        content=(
            "你是基于用户上传文档的 RAG 问答助手。只能依据提供的文档片段回答；"
            "若片段不足以回答，请明确说明。回答使用与用户问题相同的语言，简洁准确。"
            "若提供了用户长期记忆，请在语气与格式偏好上遵守，但不要把记忆当成文档事实来源。"
        )
    )
    human = HumanMessage(
        content=f"""{memory_section}对话历史（供理解指代，勿当作事实来源）:
{history_block}

用户问题:
{query}

参考文档片段:
{context_block}

请给出完整回答。若文档中没有相关信息，请说明无法从已上传文档中回答。"""
    )
    return [system, human]


def generate_answer(
    query: str,
    context_docs: List[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
    long_term_memory_block: str = "",
) -> str:
    t0 = time.perf_counter()
    if not context_docs:
        rag_trace.emit(
            "generate_answer",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "query": rag_trace.truncate_text(query),
                "context_count": 0,
                "fallback": True,
                "answer_preview": rag_trace.truncate_text(EMPTY_REPLY),
            },
        )
        return EMPTY_REPLY

    messages = _build_messages(
        query, context_docs, history, long_term_memory_block=long_term_memory_block
    )
    answer = invoke_chat(
        messages, purpose=Purpose.STRONG, temperature=0.3, reason="step"
    )
    if not answer:
        answer = "抱歉，未能生成有效回答。"
    rag_trace.emit(
        "generate_answer",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "query": rag_trace.truncate_text(query),
            "context_count": len(context_docs),
            "context_chunks": rag_trace.summarize_chunks(context_docs),
            "answer_len": len(answer),
            "answer_preview": rag_trace.truncate_text(answer),
            "fallback": False,
        },
    )
    return answer


def generate_answer_stream(
    query: str,
    context_docs: List[Dict[str, Any]],
    history: Optional[List[Dict[str, Any]]] = None,
    long_term_memory_block: str = "",
) -> Generator[str, None, None]:
    """SSE 用：yield 增量文本。无文档时一次 yield EMPTY_REPLY。"""
    t0 = time.perf_counter()
    if not context_docs:
        rag_trace.emit(
            "generate_answer_stream",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"context_count": 0, "fallback": True},
        )
        yield EMPTY_REPLY
        return

    messages = _build_messages(
        query, context_docs, history, long_term_memory_block=long_term_memory_block
    )
    total = 0
    for delta in stream_chat(
        messages, purpose=Purpose.STRONG, temperature=0.3, reason="step"
    ):
        total += len(delta)
        yield delta
    rag_trace.emit(
        "generate_answer_stream",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "query": rag_trace.truncate_text(query),
            "context_count": len(context_docs),
            "answer_len": total,
            "fallback": False,
        },
    )
