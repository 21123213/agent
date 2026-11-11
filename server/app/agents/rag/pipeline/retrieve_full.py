"""
Full 检索核（评测 + 工具共用）：扩写 → 向量+BM25 → 问内/跨问 RRF → 可选重排。

设计（2026-11）
--------------
- **不再**在本模块做多轮指代改写（``query_understanding``）。
  调用方必须传入已独立的检索句：
  - Eval：``query`` 原题直传；
  - Online Agent：由模型在调 ``rag_search`` 前消解指代。
- 返回值含 ``stages``，便于评测/LangSmith 对照「每步入选切片」。
"""
from __future__ import annotations

import contextvars
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from app.agents.rag.pipeline.fusion import rrf_fuse
from app.agents.rag.pipeline.keyword_retrieval import bm25_search
from app.agents.rag.pipeline.query_expand import expand_queries
from app.agents.rag.pipeline.reranker import dashscope_rerank
from app.common.embeddings import embed_query
from app.common import tracing as rag_trace
from app.infra.milvus_service import milvus_service
from app.settings import get_settings


def _normalize_weights(
    vector_weight: Optional[float], bm25_weight: Optional[float]
) -> tuple[float, float]:
    s = get_settings()
    vw = s.rrf_vector_weight if vector_weight is None else float(vector_weight)
    bw = s.rrf_bm25_weight if bm25_weight is None else float(bm25_weight)
    vw = max(0.0, vw)
    bw = max(0.0, bw)
    total = vw + bw
    if total <= 0:
        return 0.5, 0.5
    return vw / total, bw / total


def _submit(executor: ThreadPoolExecutor, fn, *args):
    ctx = contextvars.copy_context()
    return executor.submit(ctx.run, fn, *args)


def _vector_search(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    top_k: int,
    file_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    t0 = time.perf_counter()
    if not milvus_service.enabled:
        rag_trace.emit(
            "vector_search",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"hits": 0, "milvus_enabled": False},
        )
        return []
    try:
        embedding = embed_query(query)
        hits = milvus_service.search(
            unit_id=unit_id,
            knowledge_base_ids=[knowledge_base_id],
            query_embedding=embedding,
            top_k=top_k,
            file_ids=file_ids,
        )
        formatted: List[Dict[str, Any]] = []
        for hit in hits:
            text = hit.get("chunk_text") or ""
            formatted.append(
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
                "query": rag_trace.truncate_text(query),
                "top_k": top_k,
                "hits": len(formatted),
                "chunks": rag_trace.summarize_chunks(formatted),
            },
        )
        return formatted
    except Exception as e:
        rag_trace.emit(
            "vector_search",
            status="error",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"query": rag_trace.truncate_text(query), "hits": 0},
            message=str(e),
            error_type=type(e).__name__,
        )
        raise


def _retrieve_for_single_query(
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    file_ids: Optional[List[int]],
    vw: float,
    bw: float,
) -> Dict[str, Any]:
    """单扩写问句：vector ∥ bm25 → 问内 RRF。返回 hits + 分路切片（供 stages）。"""
    s = get_settings()
    per_path = s.retrieve_top_k_per_path
    per_query = s.rrf_per_query
    rrf_k = s.rrf_k
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=2) as executor:
        vf = _submit(
            executor,
            lambda: _vector_search(
                unit_id=unit_id,
                knowledge_base_id=knowledge_base_id,
                query=query,
                top_k=per_path,
                file_ids=file_ids,
            ),
        )
        bf = _submit(
            executor,
            lambda: bm25_search(
                unit_id=unit_id,
                knowledge_base_id=knowledge_base_id,
                query=query,
                top_k=per_path,
                file_ids=file_ids,
            ),
        )
        vector_hits = vf.result()
        bm25_hits = bf.result()

    fused = rrf_fuse(
        [vector_hits, bm25_hits],
        top_n=per_query,
        k=rrf_k,
        weights=[vw, bw],
    )
    rag_trace.emit(
        "rrf_per_query",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "query": rag_trace.truncate_text(query),
            "vector_hits": len(vector_hits),
            "bm25_hits": len(bm25_hits),
            "fused_hits": len(fused),
            "chunks": rag_trace.summarize_chunks(fused),
        },
    )
    return {
        "query": query,
        "vector": vector_hits,
        "bm25": bm25_hits,
        "rrf": fused,
    }


def retrieve_only(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    file_ids: Optional[List[int]] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
) -> Dict[str, Any]:
    """
    纯检索：``query`` 原样进入扩写与双路检索（不做指代改写）。

    返回字段：
    - ``chunks``：最终入答切片（rerank 或 RRF top-k）
    - ``stages``：逐步入选切片（评测/对照用）
      - ``expanded_queries``
      - ``per_query``: [{query, vector, bm25, rrf}, ...]
      - ``rrf_final`` / ``after_rerank``
    """
    s = get_settings()
    t0 = time.perf_counter()
    vw, bw = _normalize_weights(vector_weight, bm25_weight)
    query = (query or "").strip()

    if not query:
        return {
            "pipeline": "retrieve_only",
            "query": "",
            "standalone_query": "",
            "expanded_queries": [],
            "chunks": [],
            "stages": {},
            "skip_answer": True,
            "reply_hint": "问题不能为空。",
            "vector_weight": vw,
            "bm25_weight": bw,
        }

    # 检索句 = 入参（评测直传原题；Agent 工具侧已消解指代）
    standalone = query
    expanded = expand_queries(standalone)
    if not expanded:
        expanded = [standalone]

    # Lite：并行多 query 前先串行 connect+load，避免多线程同时起 Lite 抢文件锁
    milvus_ready = milvus_service.ensure_ready()
    if not milvus_ready:
        rag_trace.emit(
            "vector_search",
            data={
                "pipeline": "retrieve_only",
                "hits": 0,
                "milvus_ready": False,
                "note": "ensure_ready_failed_bm25_only",
            },
        )

    per_query_detail: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(4, len(expanded))) as executor:
        futures = [
            _submit(
                executor,
                _retrieve_for_single_query,
                unit_id,
                knowledge_base_id,
                q,
                file_ids,
                vw,
                bw,
            )
            for q in expanded
        ]
        for fut in futures:
            per_query_detail.append(fut.result())

    per_query_lists = [d["rrf"] for d in per_query_detail]
    fused = rrf_fuse(per_query_lists, top_n=s.rrf_final, k=s.rrf_k)

    rerank_meta: Dict[str, Any] = {}
    if s.rerank_enabled:
        try:
            chunks, rerank_meta = dashscope_rerank(standalone, fused)
            rag_trace.emit("rerank", data={**rerank_meta, "hits": len(chunks)})
        except Exception as e:
            rag_trace.emit(
                "rerank",
                status="error",
                message=str(e),
                error_type=type(e).__name__,
                data={"fallback": True},
            )
            chunks = fused[: s.answer_top_k]
            rerank_meta = {"fallback": True}
    else:
        chunks = fused[: s.answer_top_k]

    skip = not chunks
    stages = {
        "expanded_queries": list(expanded),
        "per_query": [
            {
                "query": d["query"],
                "vector_chunks": rag_trace.summarize_chunks(d["vector"]),
                "bm25_chunks": rag_trace.summarize_chunks(d["bm25"]),
                "rrf_chunks": rag_trace.summarize_chunks(d["rrf"]),
            }
            for d in per_query_detail
        ],
        "rrf_final_chunks": rag_trace.summarize_chunks(fused),
        "after_rerank_chunks": rag_trace.summarize_chunks(chunks),
        "rerank": rerank_meta,
    }

    rag_trace.emit(
        "rrf_final",
        latency_ms=(time.perf_counter() - t0) * 1000,
        data={
            "pipeline": "retrieve_only",
            "query": rag_trace.truncate_text(query),
            "standalone_query": rag_trace.truncate_text(standalone),
            "expanded_queries": [rag_trace.truncate_text(q) for q in expanded],
            "hits": len(chunks),
            "chunks": rag_trace.summarize_chunks(chunks),
            "skip_answer": skip,
            "vector_weight": vw,
            "bm25_weight": bw,
        },
    )
    return {
        "pipeline": "retrieve_only",
        "query": query,
        "standalone_query": standalone,
        "expanded_queries": expanded,
        "chunks": chunks,
        "stages": stages,
        "skip_answer": skip,
        "reply_hint": "没有相关信息。" if skip else "",
        "vector_weight": vw,
        "bm25_weight": bw,
    }


def retrieve_full(
    *,
    unit_id: int,
    knowledge_base_id: int,
    user_input: str,
    session_context: Optional[List[Dict[str, Any]]] = None,
    file_ids: Optional[List[int]] = None,
    vector_weight: Optional[float] = None,
    bm25_weight: Optional[float] = None,
    standalone_query: Optional[str] = None,
) -> Dict[str, Any]:
    """
    兼容旧调用名。

    - 优先使用 ``standalone_query``；
    - 否则使用 ``user_input``（**不再**调用 ``understand_query``）。
    - ``session_context`` 保留参数仅为兼容，已忽略。
    """
    _ = session_context  # 指代改写已上移到 Agent / 评测直传
    q = (standalone_query if standalone_query is not None else user_input) or ""
    out = retrieve_only(
        unit_id=unit_id,
        knowledge_base_id=knowledge_base_id,
        query=q.strip(),
        file_ids=file_ids,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
    )
    # 旧字段名兼容
    out["pipeline"] = "full"
    return out
