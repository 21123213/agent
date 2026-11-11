"""
``rag_search`` 工具执行时的会话上下文（LangGraph state → tool 无 InjectedState）。

由 ``tool_exec`` 在进入工具批之前 ``bind``，工具内 ``get``；批结束 ``reset``。
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RagToolContext:
    unit_id: int
    knowledge_base_id: int
    file_ids: Optional[List[int]] = None
    vector_weight: Optional[float] = None
    bm25_weight: Optional[float] = None
    # quick → retrieve_baseline；deep → retrieve_only（full）
    retrieve_pipeline: str = "full"
    # 本轮已成功检索次数 / 上限（默认 2，与 settings.reasoner_rag_search_max 对齐）
    call_count: int = 0
    max_calls: int = 2
    # 本轮累计召回切片（供 SSE sources / 落库）
    accumulated_chunks: List[Dict[str, Any]] = field(default_factory=list)


_ctx: contextvars.ContextVar[Optional[RagToolContext]] = contextvars.ContextVar(
    "rag_tool_ctx", default=None
)


def bind(ctx: RagToolContext) -> contextvars.Token:
    return _ctx.set(ctx)


def reset(token: contextvars.Token) -> None:
    _ctx.reset(token)


def get() -> Optional[RagToolContext]:
    return _ctx.get()
