"""
用户长期记忆：抽取 / 写入 / 问答前召回。

写什么
------
- 触发：每 N 轮（Settings.user_memory_extract_every_n）或抽取器判定稳定偏好。
- 不记：寒暄、一次性文档事实、与用户无关内容。
- FAST 模型抽 JSON：should_save / category / content / scenario / confidence。

存哪里
------
1. MySQL user_memories（真相源）
2. Milvus user_memory_vectors（语义召回）
3. Redis user:{uid}:memory:recent（热列表）

读路径（问答前）
----------------
embed_query(当前问题) → Milvus 按 user_id(+unit) top-m
→ 拼进 prompt「长期记忆」段；Redis 有缓存时可先展示再决定是否向量搜。

开关：USER_MEMORY_ENABLED（默认 true）。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.embeddings import embed_query
from app.common.llm_client import Purpose, invoke_chat
from app.common import tracing as rag_trace
from app.infra import memory_repo
from app.infra import redis_client as redis_mgr
from app.infra.milvus_service import milvus_service
from app.settings import get_settings

logger = logging.getLogger(__name__)


def user_memory_enabled() -> bool:
    return bool(get_settings().user_memory_enabled)


def retrieve_for_prompt(
    *,
    user_id: int,
    unit_id: int,
    query: str,
    scenario: str = "rag_qa",
    top_m: int | None = None,
) -> List[Dict[str, Any]]:
    """
    问答前召回长期记忆条目（dict 含 content/category/score）。
    禁用或失败时返回 []，不阻断主链路。
    """
    if not user_memory_enabled():
        return []
    s = get_settings()
    k = int(top_m if top_m is not None else s.user_memory_top_m)
    t0 = time.perf_counter()
    try:
        emb = embed_query(query or " ")
        hits = milvus_service.search_user_memories(
            user_id=user_id,
            unit_id=unit_id,
            query_embedding=emb,
            top_k=k,
            scenario=scenario,
        )
        # 若向量空，回退 MySQL 最近 K 条
        if not hits:
            rows = memory_repo.list_active_by_user(
                user_id=user_id, unit_id=unit_id, scenario=scenario, limit=k
            )
            hits = [
                {
                    "content": r.get("content"),
                    "category": r.get("category"),
                    "memory_id": r.get("id"),
                    "score": 0.0,
                    "updated_at": None,
                }
                for r in rows
            ]
        rag_trace.emit(
            "user_memory_retrieve",
            latency_ms=(time.perf_counter() - t0) * 1000,
            data={"user_id": user_id, "hits": len(hits), "scenario": scenario},
        )
        return hits
    except Exception as exc:
        logger.warning("retrieve_for_prompt failed: %s", exc)
        rag_trace.emit(
            "user_memory_retrieve",
            status="error",
            latency_ms=(time.perf_counter() - t0) * 1000,
            message=str(exc),
        )
        return []


def format_memories_for_prompt(memories: List[Dict[str, Any]]) -> str:
    """把召回结果格式化为可注入 system/human 的文本块。"""
    if not memories:
        return ""
    lines = []
    for i, m in enumerate(memories, start=1):
        cat = m.get("category") or "preference"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{i}. [{cat}] {content}")
    if not lines:
        return ""
    return "用户长期记忆（请在回答风格/偏好上遵守，勿编造未列出的事实）：\n" + "\n".join(
        lines
    )


def _parse_extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出中抠 JSON 对象。"""
    text = (text or "").strip()
    if not text:
        return None
    # 直接 parse
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # 代码块或夹杂文本
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def extract_from_dialogue(
    messages: List[Dict[str, Any]],
    *,
    scenario: str = "rag_qa",
) -> Optional[Dict[str, Any]]:
    """
    FAST 抽取：输入近几轮对话，输出 should_save 等字段。
    返回 None 表示不保存或解析失败。
    """
    if not user_memory_enabled():
        return None
    # 只取 user/assistant，控制长度
    lines = []
    for msg in messages[-12:]:
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        lines.append(f"{role}: {msg.get('content') or ''}")
    if not lines:
        return None
    block = "\n".join(lines)
    system = SystemMessage(
        content=(
            "你是用户长期记忆抽取器。从对话中识别稳定、可跨会话复用的用户偏好/习惯/画像/"
            "回答格式要求。忽略寒暄、一次性事实、仅与当前文档相关的内容。\n"
            "只输出一个 JSON 对象，字段：\n"
            '  should_save: bool,\n'
            '  category: "preference"|"habit"|"profile"|"format",\n'
            "  content: 简短中文陈述（一条记忆），\n"
            f'  scenario: 默认 "{scenario}",\n'
            "  confidence: 0~1 浮点数。\n"
            "若无可记内容：{\"should_save\": false, \"category\": \"preference\", "
            "\"content\": \"\", \"scenario\": \""
            + scenario
            + "\", \"confidence\": 0}"
        )
    )
    human = HumanMessage(content=f"对话：\n{block}\n\n请输出 JSON。")
    try:
        raw = invoke_chat(
            [system, human], purpose=Purpose.FAST, temperature=0.1, reason="step"
        )
        data = _parse_extract_json(raw)
        if not data:
            return None
        if not data.get("should_save"):
            return None
        content = str(data.get("content") or "").strip()
        if not content:
            return None
        return {
            "should_save": True,
            "category": str(data.get("category") or "preference")[:32],
            "content": content[:2000],
            "scenario": str(data.get("scenario") or scenario)[:64],
            "confidence": float(data.get("confidence") or 0.8),
        }
    except Exception as exc:
        logger.warning("extract_from_dialogue failed: %s", exc)
        return None


def persist_memory(
    *,
    user_id: int,
    unit_id: int,
    content: str,
    category: str = "preference",
    scenario: str = "rag_qa",
    agent_type: str = "rag",
    confidence: float = 0.8,
    source_session_id: str | None = None,
) -> Optional[int]:
    """
    双写：MySQL → embed+Milvus → 刷新 Redis；超限归档旧条。
    返回 memory_id；任一步失败尽量保留 DB 行。
    """
    if not user_memory_enabled():
        return None
    s = get_settings()
    # 简单冲突：同用户同 scenario 下 content 完全相同 → touch 已有
    existing = memory_repo.list_active_by_user(
        user_id=user_id, unit_id=unit_id, scenario=scenario, limit=50
    )
    for row in existing:
        if (row.get("content") or "").strip() == content.strip():
            memory_repo.touch_content(int(row["id"]), content, confidence)
            mid = int(row["id"])
            _upsert_vector(
                memory_id=mid,
                user_id=user_id,
                unit_id=unit_id,
                content=content,
                scenario=scenario,
                category=category,
            )
            _refresh_redis_cache(user_id, unit_id, scenario)
            return mid

    mid = memory_repo.insert_memory(
        user_id=user_id,
        unit_id=unit_id,
        content=content,
        category=category,
        scenario=scenario,
        agent_type=agent_type,
        confidence=confidence,
        source_session_id=source_session_id,
    )
    _upsert_vector(
        memory_id=mid,
        user_id=user_id,
        unit_id=unit_id,
        content=content,
        scenario=scenario,
        category=category,
    )
    memory_repo.archive_oldest(user_id, keep=int(s.user_memory_max_per_user))
    _refresh_redis_cache(user_id, unit_id, scenario)
    rag_trace.emit(
        "user_memory_persist",
        data={
            "user_id": user_id,
            "memory_id": mid,
            "category": category,
            "scenario": scenario,
        },
    )
    return mid


def _upsert_vector(
    *,
    memory_id: int,
    user_id: int,
    unit_id: int,
    content: str,
    scenario: str,
    category: str,
) -> None:
    try:
        emb = embed_query(content)
        vid = milvus_service.insert_user_memory_vector(
            memory_id=memory_id,
            user_id=user_id,
            unit_id=unit_id,
            content=content,
            embedding=emb,
            scenario=scenario,
            category=category,
            updated_at=int(time.time()),
        )
        if vid:
            memory_repo.set_vector_id(memory_id, vid)
    except Exception as exc:
        logger.warning(
            "memory vector upsert failed memory_id=%s: %s", memory_id, exc
        )


def _refresh_redis_cache(user_id: int, unit_id: int, scenario: str) -> None:
    try:
        rows = memory_repo.list_active_by_user(
            user_id=user_id, unit_id=unit_id, scenario=scenario, limit=20
        )
        items = [
            {
                "id": r["id"],
                "category": r.get("category"),
                "content": r.get("content"),
                "scenario": r.get("scenario"),
            }
            for r in rows
        ]
        redis_mgr.set_user_memory_cache(user_id, items)
    except Exception as exc:
        logger.warning("refresh memory redis failed: %s", exc)


def maybe_enqueue_extract(
    *,
    session_id: str,
    user_id: int,
    unit_id: int,
    turn_count: int,
) -> bool:
    """
    请求路径调用：每 N 轮投递 Celery；返回是否已投递。
    不阻塞 SSE。
    """
    if not user_memory_enabled():
        return False
    n = max(1, int(get_settings().user_memory_extract_every_n))
    if turn_count <= 0 or turn_count % n != 0:
        return False
    try:
        from app.agents.rag.tasks import extract_user_memory_task

        extract_user_memory_task.delay(
            session_id=session_id,
            user_id=int(user_id),
            unit_id=int(unit_id),
        )
        return True
    except Exception as exc:
        logger.warning("enqueue extract_user_memory failed: %s", exc)
        # Celery 不可达时可选同步抽（不阻塞太久）；此处仅记日志，避免拖慢 SSE
        return False
