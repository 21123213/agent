"""
聊天会话 / 消息仓储（MySQL 真相源）。

职责
----
- chat_sessions：按用户 + knowledge_base_id 管理 RAG 会话（换 KB = 换会话）。
- chat_messages：全量消息；Redis 窗口见 redis_client / session_memory，冷启动从本表回填。
- use_history 列：业务层忽略（有会话即拼记忆），本模块读写时不依赖该字段。

约定
----
- 所有 SQL 经 db.execute / fetch_*（连接池 + 参数化）。
- 删会话依赖 FK CASCADE 清 messages；调用方另清 Redis 窗口。
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Optional

from app.infra.db import execute, fetch_all, fetch_one


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def create_session(
    *,
    user_id: int,
    unit_id: int,
    knowledge_base_id: int | None = None,
    title: str = "新会话",
    agent_type: str = "rag",
    session_id: str | None = None,
) -> str:
    """
    新建会话并返回业务 session_id（UUID）。

    ``knowledge_base_id``：RAG 必填；reasoner 等非 KB agent 传 ``None``。
    """
    sid = session_id or str(uuid.uuid4())
    kb_val = int(knowledge_base_id) if knowledge_base_id is not None else None
    execute(
        "INSERT INTO chat_sessions "
        "(session_id, user_id, unit_id, agent_type, knowledge_base_id, "
        " use_history, status, title) "
        "VALUES (%s, %s, %s, %s, %s, 1, 'active', %s)",
        (
            sid,
            int(user_id),
            int(unit_id),
            agent_type,
            kb_val,
            (title or "新会话")[:255],
        ),
    )
    return sid


def get_session(session_id: str) -> Optional[dict[str, Any]]:
    return fetch_one(
        "SELECT id, session_id, user_id, unit_id, agent_type, knowledge_base_id, "
        "use_history, status, title, created_at, updated_at "
        "FROM chat_sessions WHERE session_id = %s",
        (session_id,),
    )


def list_sessions(
    *,
    user_id: int,
    knowledge_base_id: int,
    agent_type: str = "rag",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """当前用户在指定 KB 下的会话列表（最近更新优先）。"""
    return fetch_all(
        "SELECT session_id, user_id, unit_id, agent_type, knowledge_base_id, "
        "status, title, created_at, updated_at "
        "FROM chat_sessions "
        "WHERE user_id = %s AND knowledge_base_id = %s AND agent_type = %s "
        "AND status = 'active' "
        "ORDER BY updated_at DESC LIMIT %s",
        (int(user_id), int(knowledge_base_id), agent_type, int(limit)),
    )


def list_sessions_by_agent(
    *,
    user_id: int,
    agent_type: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    按 agent_type 列会话（不按 KB）。

    用于 reasoner 等 ``knowledge_base_id`` 可空的 agent。
    """
    return fetch_all(
        "SELECT session_id, user_id, unit_id, agent_type, knowledge_base_id, "
        "status, title, created_at, updated_at "
        "FROM chat_sessions "
        "WHERE user_id = %s AND agent_type = %s AND status = 'active' "
        "ORDER BY updated_at DESC LIMIT %s",
        (int(user_id), agent_type, int(limit)),
    )


def touch_session(session_id: str) -> None:
    """刷新 updated_at（双写消息后调用）。"""
    execute(
        "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP "
        "WHERE session_id = %s",
        (session_id,),
    )


def update_title(session_id: str, title: str) -> None:
    execute(
        "UPDATE chat_sessions SET title = %s WHERE session_id = %s",
        ((title or "新会话")[:255], session_id),
    )


def delete_session(session_id: str) -> bool:
    """物理删除会话；messages 靠 ON DELETE CASCADE。返回是否删到行。"""
    # execute 返回 lastrowid；用 rowcount 更准确——封装里 execute 可能不暴露。
    # 这里先查再删，保证幂等语义。
    row = get_session(session_id)
    if not row:
        return False
    execute("DELETE FROM chat_sessions WHERE session_id = %s", (session_id,))
    return True


def clear_sessions_for_kb(
    *,
    user_id: int,
    knowledge_base_id: int,
    agent_type: str = "rag",
) -> list[str]:
    """清空某用户在某 KB 下全部会话；返回被删 session_id 列表（供清 Redis）。"""
    rows = list_sessions(
        user_id=user_id,
        knowledge_base_id=knowledge_base_id,
        agent_type=agent_type,
        limit=500,
    )
    sids = [str(r["session_id"]) for r in rows]
    for sid in sids:
        execute("DELETE FROM chat_sessions WHERE session_id = %s", (sid,))
    return sids


# ---------------------------------------------------------------------------
# messages
# ---------------------------------------------------------------------------


def insert_message(
    *,
    session_id: str,
    role: str,
    content: str,
    sources: list | dict | None = None,
    metadata: dict | None = None,
) -> int:
    """写入一条消息；sources/metadata 存 JSON。返回自增 id。"""
    sources_json = (
        json.dumps(sources, ensure_ascii=False) if sources is not None else None
    )
    meta_json = (
        json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
    )
    return execute(
        "INSERT INTO chat_messages (session_id, role, content, sources, metadata) "
        "VALUES (%s, %s, %s, %s, %s)",
        (session_id, role, content, sources_json, meta_json),
    )


def list_messages(session_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """按时间正序取历史（含 sources）。"""
    rows = fetch_all(
        "SELECT id, session_id, role, content, sources, metadata, created_at "
        "FROM chat_messages WHERE session_id = %s "
        "ORDER BY id ASC LIMIT %s",
        (session_id, int(limit)),
    )
    return [_normalize_message(r) for r in rows]


def get_recent_messages(session_id: str, limit: int = 6) -> list[dict[str, Any]]:
    """
    最近 N 条（用于 Redis 冷启动回填）。
    按 id 倒序取再反转为时间正序。
    """
    rows = fetch_all(
        "SELECT id, session_id, role, content, sources, metadata, created_at "
        "FROM chat_messages WHERE session_id = %s "
        "ORDER BY id DESC LIMIT %s",
        (session_id, int(limit)),
    )
    rows = list(reversed(rows))
    return [_normalize_message(r) for r in rows]


def count_user_assistant_turns(session_id: str) -> int:
    """统计 user 消息条数，近似「对话轮数」（用于长期记忆每 N 轮触发）。"""
    row = fetch_one(
        "SELECT COUNT(*) AS c FROM chat_messages "
        "WHERE session_id = %s AND role = 'user'",
        (session_id,),
    )
    return int(row["c"]) if row else 0


def _normalize_message(row: dict[str, Any]) -> dict[str, Any]:
    """把 DB 行里的 JSON 字段解成 Python 对象，供 API / Redis 使用。"""
    out = dict(row)
    for key in ("sources", "metadata"):
        val = out.get(key)
        if isinstance(val, (bytes, bytearray)):
            val = val.decode("utf-8", errors="ignore")
        if isinstance(val, str):
            try:
                out[key] = json.loads(val)
            except json.JSONDecodeError:
                out[key] = None
    return out
