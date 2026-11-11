"""BM25 稀疏检索。"""
from __future__ import annotations

import re
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from rank_bm25 import BM25Okapi

from app.common import tracing as rag_trace
from app.infra import file_repo
from app.settings import get_settings


def _use_jieba() -> bool:
    return bool(get_settings().bm25_use_jieba)


@lru_cache(maxsize=4096)
def _jieba_cut(text: str) -> Tuple[str, ...]:
    import jieba

    return tuple(t.strip().lower() for t in jieba.cut(text) if t.strip())


def tokenize(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    tokens: List[str] = []
    if _use_jieba():
        try:
            for seg in _jieba_cut(text):
                if re.match(r"^[\u4e00-\u9fff]+$", seg):
                    if len(seg) >= 1:
                        tokens.append(seg)
                elif re.match(r"^[a-z0-9]+$", seg) and len(seg) >= 2:
                    tokens.append(seg)
            if tokens:
                return tokens
        except Exception:
            pass
    for part in re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower()):
        if re.match(r"[a-z0-9]+", part):
            if len(part) >= 2:
                tokens.append(part)
        else:
            tokens.append(part)
    return tokens


def bm25_search(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query: str,
    top_k: int = 20,
    file_ids: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    t0 = time.perf_counter()
    s = get_settings()
    t_tok_q = time.perf_counter()
    query_tokens = tokenize(query)
    query_tokenize_ms = (time.perf_counter() - t_tok_q) * 1000
    if not query_tokens:
        rag_trace.emit(
            "bm25_search",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "query": rag_trace.truncate_text(query),
                "top_k": top_k,
                "file_ids": file_ids,
                "hits": 0,
                "chunks": [],
                "reason": "empty_query_tokens",
                "query_tokenize_ms": round(query_tokenize_ms, 2),
            },
        )
        return []

    try:
        t_db = time.perf_counter()
        chunks = file_repo.search_chunks_for_bm25(
            unit_id=unit_id,
            knowledge_base_id=knowledge_base_id,
            file_ids=file_ids,
            limit=s.bm25_candidate_limit,
        )
        db_ms = (time.perf_counter() - t_db) * 1000
        if not chunks:
            rag_trace.emit(
                "bm25_search",
                latency_ms=(time.perf_counter() - t0) * 1000,
                data={
                    "query": rag_trace.truncate_text(query),
                    "top_k": top_k,
                    "hits": 0,
                    "chunks": [],
                    "reason": "no_candidates",
                    "query_tokenize_ms": round(query_tokenize_ms, 2),
                    "db_ms": round(db_ms, 2),
                },
            )
            return []

        t_corp = time.perf_counter()
        corpus_tokens: List[List[str]] = []
        valid_chunks: List[Dict[str, Any]] = []
        for chunk in chunks:
            text = chunk.get("chunk_text") or ""
            toks = tokenize(text)
            if not toks:
                continue
            corpus_tokens.append(toks)
            valid_chunks.append(chunk)
        corpus_tokenize_ms = (time.perf_counter() - t_corp) * 1000

        if not corpus_tokens:
            return []

        t_score = time.perf_counter()
        bm25 = BM25Okapi(corpus_tokens)
        scores = bm25.get_scores(query_tokens)
        scored: List[Dict[str, Any]] = []
        for idx, chunk in enumerate(valid_chunks):
            score = float(scores[idx])
            if score <= 0:
                continue
            file_id = chunk["file_id"]
            chunk_index = chunk["chunk_index"]
            text = chunk.get("chunk_text") or ""
            vid = chunk.get("vector_id") or f"kb{knowledge_base_id}_f{file_id}_c{chunk_index}"
            scored.append(
                {
                    "id": vid,
                    "file_id": file_id,
                    "chunk_index": chunk_index,
                    "content": text,
                    "chunk_text": text,
                    "score": score,
                    "metadata": {
                        "file_id": file_id,
                        "chunk_index": chunk_index,
                        "page_numbers": chunk.get("page_numbers") or [],
                        "retrieval": "bm25",
                    },
                    "source": "bm25",
                }
            )
        scored.sort(key=lambda x: x["score"], reverse=True)
        result = scored[:top_k]
        score_ms = (time.perf_counter() - t_score) * 1000
        rag_trace.emit(
            "bm25_search",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={
                "query": rag_trace.truncate_text(query),
                "top_k": top_k,
                "file_ids": file_ids,
                "candidate_count": len(valid_chunks),
                "hits": len(result),
                "chunks": rag_trace.summarize_chunks(result),
                "query_tokenize_ms": round(query_tokenize_ms, 2),
                "db_ms": round(db_ms, 2),
                "corpus_tokenize_ms": round(corpus_tokenize_ms, 2),
                "score_ms": round(score_ms, 2),
            },
        )
        return result
    except Exception as e:
        rag_trace.emit(
            "bm25_search",
            status="error",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"query": rag_trace.truncate_text(query), "hits": 0},
            message=str(e),
            error_type=type(e).__name__,
        )
        raise
