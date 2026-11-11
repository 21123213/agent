"""
知识库检索工具：Agent 按需调用；入参为**已消解指代**的独立检索句。

实现走 ``retrieve_only``（与 Eval 共用检索核）。切片写入 ``rag_tool_context`` 供落库。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated, Any, Dict, List

from langchain_core.tools import StructuredTool, tool

from app.agents.rag.pipeline.rag_tool_context import get as get_rag_ctx
from app.agents.rag.pipeline.retrieve_baseline import retrieve_baseline
from app.agents.rag.pipeline.retrieve_full import retrieve_only
from app.agents.reasoner.tools.core import _wrap_async
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 给模型看的单片预览长度（完整文本已在 ctx.accumulated_chunks）
_PREVIEW_CHARS = 600


def _format_observation(retrieved: Dict[str, Any]) -> str:
    chunks: List[Dict[str, Any]] = list(retrieved.get("chunks") or [])
    if not chunks:
        return json.dumps(
            {
                "ok": True,
                "hits": 0,
                "message": "知识库中未检索到相关片段。可换一种问法再搜（本轮最多 2 次），或如实告知用户无依据。",
                "query": retrieved.get("standalone_query") or "",
            },
            ensure_ascii=False,
        )
    previews = []
    for i, c in enumerate(chunks, start=1):
        text = (c.get("content") or c.get("chunk_text") or "")[:_PREVIEW_CHARS]
        meta = c.get("metadata") or {}
        previews.append(
            {
                "rank": i,
                "file_id": c.get("file_id") or meta.get("file_id"),
                "chunk_index": c.get("chunk_index"),
                "score": c.get("score") or c.get("rerank_score") or c.get("rrf_score"),
                "preview": text,
            }
        )
    return json.dumps(
        {
            "ok": True,
            "hits": len(chunks),
            "query": retrieved.get("standalone_query") or "",
            "expanded_queries": retrieved.get("expanded_queries") or [],
            "chunks": previews,
            "hint": "请仅依据上述片段回答；不足则说明文档未覆盖，勿编造。",
        },
        ensure_ascii=False,
    )


async def _rag_search_impl(query: str) -> str:
    ctx = get_rag_ctx()
    if ctx is None or not ctx.knowledge_base_id:
        return json.dumps(
            {
                "ok": False,
                "error": "当前会话未绑定知识库，无法检索。请用户先选择知识库。",
            },
            ensure_ascii=False,
        )
    if int(ctx.call_count) >= int(ctx.max_calls):
        return json.dumps(
            {
                "ok": False,
                "error": f"本轮知识库检索已达上限（{ctx.max_calls} 次），请基于已有片段作答。",
            },
            ensure_ascii=False,
        )
    q = (query or "").strip()
    if not q:
        return json.dumps({"ok": False, "error": "query 不能为空"}, ensure_ascii=False)

    def _run() -> Dict[str, Any]:
        # quick → baseline（向量15+BM25 15→rerank10）；deep → full 混合检索
        pipe = (ctx.retrieve_pipeline or "full").strip().lower()
        if pipe in ("baseline", "quick"):
            return retrieve_baseline(
                unit_id=int(ctx.unit_id),
                knowledge_base_id=int(ctx.knowledge_base_id),
                query=q,
                file_ids=ctx.file_ids,
                vector_weight=ctx.vector_weight,
                bm25_weight=ctx.bm25_weight,
            )
        return retrieve_only(
            unit_id=int(ctx.unit_id),
            knowledge_base_id=int(ctx.knowledge_base_id),
            query=q,
            file_ids=ctx.file_ids,
            vector_weight=ctx.vector_weight,
            bm25_weight=ctx.bm25_weight,
        )

    retrieved = await asyncio.to_thread(_run)
    ctx.call_count = int(ctx.call_count) + 1
    chunks = list(retrieved.get("chunks") or [])
    # 累积完整切片（落库 / SSE sources）；观察文本只给预览以免撑爆上下文
    ctx.accumulated_chunks.extend(chunks)
    return _format_observation(retrieved)


def build_rag_tools() -> list[StructuredTool]:
    max_n = int(getattr(get_settings(), "reasoner_rag_search_max", 2) or 2)

    @tool("rag_search")
    async def rag_search(
        query: Annotated[
            str,
            "独立检索问句：尽量接近用户原话，仅补全指代/省略；不要改写成另一题，不要回答问题本身",
        ],
    ) -> str:
        """在当前会话绑定的知识库中检索文档片段（需文档依据时再调用）。"""
        return await _wrap_async("rag_search", lambda: _rag_search_impl(query))

    rag_search.description = (
        "在当前会话绑定的知识库中检索文档片段。"
        "需要文档依据时再调用；闲聊/澄清/总结上轮内容不要搜。"
        "调用前自行消解「它/上面/第一条」等指代，问句尽量接近原句。"
        f"本轮最多 {max_n} 次检索。"
    )
    return [rag_search]
