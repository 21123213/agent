"""DashScope TextReRank。"""
from __future__ import annotations

from http import HTTPStatus
from typing import Any, Dict, List, Optional, Tuple

from dashscope import TextReRank

from app.settings import get_settings


def _chunk_text(chunk: Dict[str, Any]) -> str:
    return (chunk.get("content") or chunk.get("chunk_text") or "").strip()


def _truncate_doc(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return text[:max_chars]


def dashscope_rerank(
    query: str,
    chunks: List[Dict[str, Any]],
    *,
    top_n: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    s = get_settings()
    if not chunks:
        return [], {"model": s.rerank_model, "top_n": 0, "input_docs": 0}

    answer_k = top_n if top_n is not None else s.answer_top_k
    answer_k = max(1, min(answer_k, len(chunks)))
    max_chars = s.rerank_max_doc_chars
    documents: List[str] = []
    valid_indices: List[int] = []
    for i, chunk in enumerate(chunks):
        text = _truncate_doc(_chunk_text(chunk), max_chars)
        if not text:
            continue
        documents.append(text)
        valid_indices.append(i)

    if not documents:
        return [dict(c) for c in chunks[:answer_k]], {
            "model": s.rerank_model,
            "top_n": answer_k,
            "input_docs": 0,
            "reason": "empty_documents",
        }

    # 与 embedding 同源：优先 DASHSCOPE_EMBEDDING_API_KEY，再回退主 key
    key = (s.effective_embedding_api_key or "").strip()
    if not key:
        raise RuntimeError(
            "未配置 DASHSCOPE_EMBEDDING_API_KEY / DASHSCOPE_API_KEY，无法调用 TextReRank"
        )

    model = (s.rerank_model or "gte-rerank-v2").strip()
    resp = TextReRank.call(
        model=model,
        query=query,
        documents=documents,
        top_n=len(documents),
        return_documents=False,
        api_key=key,
    )
    if getattr(resp, "status_code", None) != HTTPStatus.OK:
        raise RuntimeError(
            f"TextReRank 失败 status={getattr(resp, 'status_code', None)} "
            f"message={getattr(resp, 'message', '')}"
        )

    output = getattr(resp, "output", None) or {}
    results = (
        output.get("results")
        if isinstance(output, dict)
        else getattr(output, "results", None)
    )
    if not results:
        raise RuntimeError("TextReRank 返回空 results")

    scored: List[Tuple[float, int]] = []
    for item in results:
        if isinstance(item, dict):
            idx = int(item.get("index", -1))
            score = float(item.get("relevance_score") or 0.0)
        else:
            idx = int(getattr(item, "index", -1))
            score = float(getattr(item, "relevance_score", 0.0) or 0.0)
        if 0 <= idx < len(valid_indices):
            scored.append((score, valid_indices[idx]))
    scored.sort(key=lambda x: x[0], reverse=True)

    reranked: List[Dict[str, Any]] = []
    for score, orig_i in scored[:answer_k]:
        item = dict(chunks[orig_i])
        item["rerank_score"] = score
        item["score"] = score
        meta = dict(item.get("metadata") or {})
        meta["retrieval"] = "rerank"
        meta["rerank_model"] = model
        item["metadata"] = meta
        item["source"] = "rerank"
        reranked.append(item)

    usage = getattr(resp, "usage", None) or {}
    total_tokens = (
        usage.get("total_tokens")
        if isinstance(usage, dict)
        else getattr(usage, "total_tokens", None)
    )
    return reranked, {
        "model": model,
        "top_n": answer_k,
        "input_docs": len(documents),
        "scored_docs": len(scored),
        "total_tokens": total_tokens,
        "request_id": getattr(resp, "request_id", None),
    }
