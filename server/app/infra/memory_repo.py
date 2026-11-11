"""
用户长期记忆仓储（MySQL `user_memories` 真相源）。

向量侧在 milvus_service（collection=`user_memory_vectors`）；
Redis 热缓存见 redis_client 的 user memory 辅助函数。
Celery 任务写入顺序：INSERT 本表 → embed+Milvus → 刷新 Redis。
"""
from __future__ import annotations

from typing import Any, Optional

from app.infra.db import execute, fetch_all, fetch_one


def insert_memory(
    *,
    user_id: int,
    unit_id: int,
    content: str,
    category: str = "preference",
    scenario: str = "rag_qa",
    agent_type: str = "rag",
    confidence: float = 0.8,
    source_session_id: str | None = None,
) -> int:
    """插入一条记忆，返回自增 id。"""
    return execute(
        "INSERT INTO user_memories "
        "(user_id, unit_id, category, content, scenario, agent_type, "
        " confidence, source_session_id, status) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'active')",
        (
            int(user_id),
            int(unit_id),
            (category or "preference")[:32],
            content,
            (scenario or "rag_qa")[:64],
            (agent_type or "rag")[:32],
            float(confidence),
            source_session_id,
        ),
    )


def set_vector_id(memory_id: int, vector_id: str) -> None:
    execute(
        "UPDATE user_memories SET vector_id = %s WHERE id = %s",
        (vector_id, int(memory_id)),
    )


def get_by_id(memory_id: int) -> Optional[dict[str, Any]]:
    return fetch_one(
        "SELECT id, user_id, unit_id, category, content, scenario, agent_type, "
        "confidence, source_session_id, vector_id, status, created_at, updated_at "
        "FROM user_memories WHERE id = %s",
        (int(memory_id),),
    )


def list_active_by_user(
    *,
    user_id: int,
    unit_id: int | None = None,
    scenario: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按更新时间倒序取活跃记忆（热画像 / 淘汰用）。"""
    sql = (
        "SELECT id, user_id, unit_id, category, content, scenario, agent_type, "
        "confidence, source_session_id, vector_id, status, created_at, updated_at "
        "FROM user_memories WHERE user_id = %s AND status = 'active' "
    )
    params: list[Any] = [int(user_id)]
    if unit_id is not None:
        sql += "AND unit_id = %s "
        params.append(int(unit_id))
    if scenario:
        sql += "AND scenario = %s "
        params.append(scenario)
    sql += "ORDER BY updated_at DESC LIMIT %s"
    params.append(int(limit))
    return fetch_all(sql, tuple(params))


def count_active(user_id: int) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS c FROM user_memories "
        "WHERE user_id = %s AND status = 'active'",
        (int(user_id),),
    )
    return int(row["c"]) if row else 0


def archive_oldest(user_id: int, keep: int) -> int:
    """
    超过 keep 条时，把最旧的 active 记为 archived。
    返回归档条数。不删向量（可后续垃圾回收）；检索只看 active。
    """
    rows = list_active_by_user(user_id=user_id, limit=keep + 100)
    if len(rows) <= keep:
        return 0
    to_archive = rows[keep:]  # 已按 updated_at DESC，尾部更旧
    n = 0
    for r in to_archive:
        execute(
            "UPDATE user_memories SET status = 'archived' WHERE id = %s",
            (int(r["id"]),),
        )
        n += 1
    return n


def touch_content(memory_id: int, content: str, confidence: float | None = None) -> None:
    """冲突合并：同偏好覆盖正文并刷新 updated_at。"""
    if confidence is None:
        execute(
            "UPDATE user_memories SET content = %s, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = %s",
            (content, int(memory_id)),
        )
    else:
        execute(
            "UPDATE user_memories SET content = %s, confidence = %s, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (content, float(confidence), int(memory_id)),
        )
