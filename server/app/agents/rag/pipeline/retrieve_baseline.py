"""
Baseline 检索（快速回答 / 评测 pipeline=baseline 共用）。

流程（产品约定 2026-11）
------------------------
原问题（不做扩写、不做指代改写）
  → 向量 top-15  ∥  BM25/相似度 top-15
  → 问内 RRF 融合
  → **强制** rerank，取 top-10 交给大模型

可被调用方：
- Eval：``run_rag(..., pipeline="baseline")``
- Online Agent：``rag_search`` 在 ``answer_mode=quick`` 时走本核
"""
from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.agents.rag.pipeline.fusion import rrf_fuse
from app.agents.rag.pipeline.keyword_retrieval import bm25_search
from app.agents.rag.pipeline.reranker import dashscope_rerank
from app.common.embeddings import embed_query
from app.common import tracing as rag_trace
from app.infra.milvus_service import milvus_service
from app.settings import get_settings


def _submit(executor: ThreadPoolExecutor, fn, *args):
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args)


def _vector_hits(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    top_k: int,
    file_ids: Optional[List[int]],
) -> List[Dict[str, Any]]:
    t0 = time.perf_counter()
    if not milvus_service.enabled or not milvus_service.ensure_ready():
        rag_trace.emit(
            "vector_search",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "pipeline": "baseline",
                "hits": 0,
                "milvus_ok": bool(milvus_service.enabled),
            },
        )
        return []
    embedding = embed_query(query)
    hits = milvus_service.search(
        unit_id=unit_id,
        knowledge_base_ids=[knowledge_base_id],
        query_embedding=embedding,
        top_k=top_k,
        file_ids=file_ids,
    )
    out: List[Dict[str, Any]] = []
    for hit in hits:
        text = hit.get("chunk_text") or ""
        out.append(
            {
                "id": hit.get("id"),
                "file_id": hit.get("file_id"),
                "chunk_index": hit.get("chunk_index"),
                "content": text,
                "chunk_text": text,
                "score": float(hit.get("score") or hit.get("distance") or 0),
                "metadata": {
                    **(hit.get("metadata") or {}),
                    "file_id": hit.get("file_id"),
                    "chunk_index": hit.get("chunk_index"),
                    "retrieval": "vector",
                },
                "source": "vector",
            }
        )
    rag_trace.emit(
        "vector_search",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "pipeline": "baseline",
            "query": rag_trace.truncate_text(query),
            "top_k": top_k,
            "hits": len(out),
            "chunks": rag_trace.summarize_chunks(out),
        },
    )
    return out


def retrieve_baseline(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    file_ids: Optional[List[int]] = None,
    vector_top_k: int | None = None,
    bm25_top_k: int | None = None,
    rerank_top_n: int | None = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Baseline 检索核。

    默认：向量 15 + BM25 15 → RRF → rerank 取 10。
    ``top_k`` 旧参数已废弃；请用 ``vector_top_k`` / ``rerank_top_n``。
    """
    s = get_settings()
    v_k = int(
        vector_top_k
        if vector_top_k is not None
        else getattr(s, "baseline_vector_top_k", 15) or 15
    )
    b_k = int(
        bm25_top_k
        if bm25_top_k is not None
        else getattr(s, "baseline_bm25_top_k", 15) or 15
    )
    r_n = int(
        rerank_top_n
        if rerank_top_n is not None
        else getattr(s, "baseline_rerank_top_n", 10) or 10
    )
    # 权重默认各半（快速路径不强调调权；调用方可覆盖）
    vw = 0.5 if vector_weight is None else float(vector_weight)
    bw = 0.5 if bm25_weight is None else float(bm25_weight)
    total = vw + bw
    if total <= 0:
        vw, bw = 0.5, 0.5
    else:
        vw, bw = vw / total, bw / total

    query = (query or "").strip()
    t0 = time.perf_counter()

    empty = {
        "pipeline": "baseline",
        "query": query,
        "standalone_query": query,
        "expanded_queries": [query] if query else [],
        "chunks": [],
        "stages": {},
        "skip_answer": True,
        "reply_hint": "",
        "vector_weight": vw,
        "bm25_weight": bw,
    }
    if not query:
        empty["reply_hint"] = "问题不能为空。"
        return empty

    # Lite：并行前先 ensure，避免抢锁
    milvus_service.ensure_ready()

    with ThreadPoolExecutor(max_workers=2) as executor:
        vf = _submit(
            executor,
            lambda: _vector_hits(
                unit_id=unit_id,
                knowledge_base_id=knowledge_base_id,
                query=query,
                top_k=v_k,
                file_ids=file_ids,
            ),
        )
        bf = _submit(
            executor,
            lambda: bm25_search(
                unit_id=unit_id,
                knowledge_base_id=knowledge_base_id,
                query=query,
                top_k=b_k,
                file_ids=file_ids,
            ),
        )
        vector_hits = vf.result()
        bm25_hits = bf.result()

    fused = rrf_fuse(
        [vector_hits, bm25_hits],
        top_n=max(v_k, b_k),
        k=s.rrf_k,
        weights=[vw, bw],
    )
    rag_trace.emit(
        "rrf_per_query",
        data={
            "pipeline": "baseline",
            "query": rag_trace.truncate_text(query),
            "vector_hits": len(vector_hits),
            "bm25_hits": len(bm25_hits),
            "fused_hits": len(fused),
            "chunks": rag_trace.summarize_chunks(fused),
        },
    )

    rerank_meta: Dict[str, Any] = {}
    chunks: List[Dict[str, Any]]
    if fused:
        try:
            # Baseline **始终** rerank（与 full 受 rerank_enabled 开关不同）
            chunks, rerank_meta = dashscope_rerank(query, fused, top_n=r_n)
            rag_trace.emit(
                "rerank",
                data={**rerank_meta, "pipeline": "baseline", "hits": len(chunks)},
            )
        except Exception as e:
            rag_trace.emit(
                "rerank",
                status="error",
                message=str(e),
                error_type=type(e).__name__,
                data={"pipeline": "baseline", "fallback": True},
            )
            chunks = fused[:r_n]
            rerank_meta = {"fallback": True, "error": str(e)}
    else:
        chunks = []

    skip = not chunks
    stages = {
        "expanded_queries": [query],
        "per_query": [
            {
                "query": query,
                "vector_chunks": rag_trace.summarize_chunks(vector_hits),
                "bm25_chunks": rag_trace.summarize_chunks(bm25_hits),
                "rrf_chunks": rag_trace.summarize_chunks(fused),
            }
        ],
        "rrf_final_chunks": rag_trace.summarize_chunks(fused),
        "after_rerank_chunks": rag_trace.summarize_chunks(chunks),
        "rerank": rerank_meta,
    }
    rag_trace.emit(
        "baseline_retrieve",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "pipeline": "baseline",
            "query": rag_trace.truncate_text(query),
            "vector_top_k": v_k,
            "bm25_top_k": b_k,
            "rerank_top_n": r_n,
            "hits": len(chunks),
            "chunks": rag_trace.summarize_chunks(chunks),
            "skip_answer": skip,
        },
    )
    return {
        "pipeline": "baseline",
        "query": query,
        "standalone_query": query,
        "expanded_queries": [query],
        "chunks": chunks,
        "stages": stages,
        "skip_answer": skip,
        "reply_hint": "没有相关信息。" if skip else "",
        "vector_weight": vw,
        "bm25_weight": bw,
    }
