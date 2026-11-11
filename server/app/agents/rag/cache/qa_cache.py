"""
RAG 问答结果缓存（**按 unit，不按 user**）。

与 session 窗口严格分离
-----------------------
- session:{sid}:context     多轮对话；清会话只删这个
- rag:qa:{unit}:{kb}:...    规范化问句 → 答案/sources
- unit:{u}:kb:{kb}:corpus_ver  语料世代；入库/删文件 INCR

键维度（见 docs/rag-chat-memory-plan.md）
----------------------------------------
rag:qa:{unit_id}:{kb_id}:{corpus_ver}:{weights_fp}:{file_ids_fp}:{query_hash}

- query 用「真正检索用的规范化问句」（原文或 standalone_query）
- 命中后仍须双写当前会话消息，保证历史不断档
- 首版不做语义近邻缓存
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.common import tracing as rag_trace
from app.infra import redis_client as redis_mgr
from app.settings import get_settings

logger = logging.getLogger(__name__)

_QA_PREFIX = "rag:qa"


def cache_enabled() -> bool:
    return bool(get_settings().rag_qa_cache_enabled)


def cache_ttl_seconds() -> int:
    return max(60, int(get_settings().rag_qa_cache_ttl))


def normalize_query(text: str) -> str:
    """strip、统一空白、英文小写（中文保持）。"""
    text = (text or "").strip().lower()
    text = re.sub(r"\s+", " ", text)
    return text


def weights_fingerprint(vector_weight: float, bm25_weight: float) -> str:
    """归一化后的权重指纹，如 v40_b60。"""
    vw = max(0.0, float(vector_weight))
    bw = max(0.0, float(bm25_weight))
    total = vw + bw
    if total <= 0:
        vw, bw = 0.5, 0.5
    else:
        vw, bw = vw / total, bw / total
    return f"v{int(round(vw * 100))}_b{int(round(bw * 100))}"


def file_ids_fingerprint(file_ids: Optional[List[int]]) -> str:
    """空 = 全库 all；否则有序 id 列表哈希前缀。"""
    if not file_ids:
        return "all"
    ordered = ",".join(str(int(x)) for x in sorted(set(int(i) for i in file_ids)))
    return "f_" + hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:16]


def build_cache_key(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query_for_cache: str,
    vector_weight: float,
    bm25_weight: float,
    file_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """组装完整 key 及可观测元数据（读 corpus_ver，不写）。"""
    corpus_ver = redis_mgr.get_corpus_version(unit_id, knowledge_base_id)
    w_fp = weights_fingerprint(vector_weight, bm25_weight)
    f_fp = file_ids_fingerprint(file_ids)
    norm_q = normalize_query(query_for_cache)
    query_hash = hashlib.sha256(norm_q.encode("utf-8")).hexdigest()[:24]
    key = (
        f"{_QA_PREFIX}:{int(unit_id)}:{int(knowledge_base_id)}:"
        f"{corpus_ver}:{w_fp}:{f_fp}:{query_hash}"
    )
    return {
        "key": key,
        "unit_id": int(unit_id),
        "kb_id": int(knowledge_base_id),
        "corpus_ver": corpus_ver,
        "weights_fp": w_fp,
        "file_ids_fp": f_fp,
        "query_hash": query_hash,
        "query_norm": norm_q,
    }


def get_cached_answer(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query_for_cache: str,
    vector_weight: float,
    bm25_weight: float,
    file_ids: Optional[List[int]] = None,
) -> Optional[Dict[str, Any]]:
    """命中返回 {answer, sources, ...}；未命中 / 禁用 / Redis 挂 → None。"""
    if not cache_enabled():
        rag_trace.emit("qa_cache", data={"hit": False, "reason": "disabled"})
        return None
    if not redis_mgr.redis_available():
        rag_trace.emit("qa_cache", data={"hit": False, "reason": "redis_down"})
        return None

    meta = build_cache_key(
        unit_id=unit_id,
        knowledge_base_id=knowledge_base_id,
        query_for_cache=query_for_cache,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        file_ids=file_ids,
    )
    try:
        raw = redis_mgr.get_redis().get(meta["key"])
    except Exception as exc:
        logger.warning("qa_cache get failed: %s", exc)
        return None

    if not raw:
        logger.info(
            "qa_cache MISS unit=%s kb=%s ver=%s w=%s q=%s",
            unit_id,
            knowledge_base_id,
            meta["corpus_ver"],
            meta["weights_fp"],
            meta["query_norm"][:80],
        )
        rag_trace.emit(
            "qa_cache",
            data={
                "hit": False,
                "unit_id": unit_id,
                "kb_id": knowledge_base_id,
                "corpus_ver": meta["corpus_ver"],
                "weights_fp": meta["weights_fp"],
                "file_ids_fp": meta["file_ids_fp"],
                "query_hash": meta["query_hash"],
                "query": rag_trace.truncate_text(query_for_cache),
            },
        )
        return None

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("qa_cache corrupt key=%s", meta["key"])
        try:
            redis_mgr.get_redis().delete(meta["key"])
        except Exception:
            pass
        return None

    logger.info(
        "qa_cache HIT unit=%s kb=%s ver=%s w=%s q=%s",
        unit_id,
        knowledge_base_id,
        meta["corpus_ver"],
        meta["weights_fp"],
        meta["query_norm"][:80],
    )
    rag_trace.emit(
        "qa_cache",
        data={
            "hit": True,
            "unit_id": unit_id,
            "kb_id": knowledge_base_id,
            "corpus_ver": meta["corpus_ver"],
            "weights_fp": meta["weights_fp"],
            "answer_len": len(payload.get("answer") or ""),
            "sources_count": len(payload.get("sources") or []),
        },
    )
    return payload


def set_cached_answer(
    *,
    unit_id: int,
    knowledge_base_id: int,
    query_for_cache: str,
    answer: str,
    sources: List[Dict[str, Any]],
    vector_weight: float,
    bm25_weight: float,
    file_ids: Optional[List[int]] = None,
) -> None:
    """写入缓存；仅成功生成且非空答案时调用。"""
    if not cache_enabled() or not redis_mgr.redis_available():
        return
    answer = (answer or "").strip()
    if not answer:
        return

    meta = build_cache_key(
        unit_id=unit_id,
        knowledge_base_id=knowledge_base_id,
        query_for_cache=query_for_cache,
        vector_weight=vector_weight,
        bm25_weight=bm25_weight,
        file_ids=file_ids,
    )
    payload = {
        "answer": answer,
        "sources": sources or [],
        "query_for_cache": query_for_cache,
    }
    ttl = cache_ttl_seconds()
    try:
        redis_mgr.get_redis().setex(
            meta["key"], ttl, json.dumps(payload, ensure_ascii=False)
        )
    except Exception as exc:
        logger.warning("qa_cache set failed: %s", exc)
        return

    logger.info(
        "qa_cache SET unit=%s kb=%s ver=%s ttl=%s q=%s",
        unit_id,
        knowledge_base_id,
        meta["corpus_ver"],
        ttl,
        meta["query_norm"][:80],
    )
    rag_trace.emit(
        "qa_cache",
        data={
            "action": "set",
            "unit_id": unit_id,
            "kb_id": knowledge_base_id,
            "corpus_ver": meta["corpus_ver"],
            "ttl": ttl,
            "answer_len": len(answer),
            "sources_count": len(sources or []),
        },
    )
