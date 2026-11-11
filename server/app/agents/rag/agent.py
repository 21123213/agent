"""
RAG 评测编排（Eval 轨）：baseline | full，无会话、无 Agent。

Online 产品对话走 Reasoner + ``rag_search``（见 ``chain.stream_rag_agent_events``）。
本模块只服务 ``test_artifacts/`` / eval 脚本：入参原题直传检索核，返回逐步切片 ``stages``。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Literal, Optional

from app.agents.rag.pipeline.answer import generate_answer
from app.agents.rag.pipeline.retrieve_baseline import retrieve_baseline
from app.agents.rag.pipeline.retrieve_full import retrieve_only
from app.common import metrics
from app.common import tracing as rag_trace

PipelineName = Literal["baseline", "full"]


def run_rag(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    pipeline: PipelineName = "full",
    file_ids: Optional[List[int]] = None,
    history: Optional[List[Dict[str, Any]]] = None,
    user_id: Optional[int] = None,
    request_id: Optional[str] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """
    评测入口：检索 + 生成。

    - ``full``：``retrieve_only(query)`` —— **不做** query_understanding；原题即检索句。
    - 返回 ``stages``：每步入选切片摘要（vector / bm25 / rrf / rerank），便于对照 jsonl。
    - ``history`` 仅注入生成 prompt，不参与改写。
    """
    t0 = time.perf_counter()
    rid = request_id or rag_trace.new_request_id()
    with rag_trace.rag_request(
        user_id=user_id,
        session_id=f"rag-eval-{rid}",
        request_id=rid,
        pipeline=pipeline,
    ):
        rag_trace.emit(
            "request_start",
            data={
                "query": rag_trace.truncate_text(query),
                "pipeline": pipeline,
                "kb_id": knowledge_base_id,
                "unit_id": unit_id,
                "file_ids": file_ids,
                "vector_weight": vector_weight,
                "bm25_weight": bm25_weight,
                "track": "eval",
            },
        )
        try:
            if pipeline == "baseline":
                retrieved = retrieve_baseline(
                    unit_id=unit_id,
                    knowledge_base_id=knowledge_base_id,
                    query=query,
                    file_ids=file_ids,
                    vector_weight=vector_weight,
                    bm25_weight=bm25_weight,
                )
                stages = dict(retrieved.get("stages") or {})
            else:
                retrieved = retrieve_only(
                    unit_id=unit_id,
                    knowledge_base_id=knowledge_base_id,
                    query=query,
                    file_ids=file_ids,
                    vector_weight=vector_weight,
                    bm25_weight=bm25_weight,
                )
                stages = dict(retrieved.get("stages") or {})

            chunks = retrieved.get("chunks") or []
            if retrieved.get("skip_answer"):
                answer = retrieved.get("reply_hint") or "没有相关信息。"
            else:
                answer = generate_answer(
                    retrieved.get("standalone_query") or query,
                    chunks,
                    history=history,
                )

            latency_ms = (time.perf_counter() - t0) * 1000
            metrics.incr("rag_requests_total", pipeline=pipeline, status="ok")
            metrics.observe_latency("rag_latency_ms", latency_ms, pipeline=pipeline)
            result = {
                "request_id": rid,
                "pipeline": pipeline,
                "query": query,
                "standalone_query": retrieved.get("standalone_query") or query,
                "expanded_queries": retrieved.get("expanded_queries") or [],
                "chunks": chunks,
                "stages": stages,
                "skip_answer": bool(retrieved.get("skip_answer")),
                "answer": answer,
                "latency_ms": round(latency_ms, 2),
                "vector_weight": retrieved.get("vector_weight"),
                "bm25_weight": retrieved.get("bm25_weight"),
            }
            rag_trace.finish(
                status="ok",
                pipeline=pipeline,
                answer_preview=rag_trace.truncate_text(answer),
                hits=len(chunks),
            )
            return result
        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            metrics.incr("rag_requests_total", pipeline=pipeline, status="error")
            metrics.observe_latency("rag_latency_ms", latency_ms, pipeline=pipeline)
            rag_trace.finish(
                status="error", message=str(e), error_type=type(e).__name__
            )
            raise
