"""
Skill 解析 + Redis 会话粘滞。

键：``reasoner:session:{sid}:skills`` → JSON list
本地与上线一律 Redis，不落进程 dict。
"""
from __future__ import annotations

import json
import logging
from typing import List

from app.agents.reasoner.skills.registry import match_skills, merge_session_skills
from app.infra import redis_client as redis_mgr
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _key(session_id: str) -> str:
    return f"reasoner:session:{session_id}:skills"


def _ttl() -> int:
    s = get_settings()
    return int(getattr(s, "reasoner_skills_ttl", 86400) or 86400)


def get_session_skills(session_id: str) -> List[str]:
    try:
        r = redis_mgr.get_redis()
        raw = r.get(_key(session_id))
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data if str(x).strip()]
    except Exception as exc:
        logger.warning("get_session_skills failed sid=%s: %s", session_id, exc)
    return []


def set_session_skills(session_id: str, skill_ids: List[str]) -> None:
    try:
        r = redis_mgr.get_redis()
        r.setex(_key(session_id), _ttl(), json.dumps(skill_ids, ensure_ascii=False))
    except Exception as exc:
        logger.warning("set_session_skills failed sid=%s: %s", session_id, exc)


def clear_session_skills(session_id: str) -> None:
    try:
        redis_mgr.get_redis().delete(_key(session_id))
    except Exception as exc:
        logger.warning("clear_session_skills failed sid=%s: %s", session_id, exc)


def load_and_merge_skills(session_id: str, user_input: str) -> List[str]:
    """
    本轮正则命中 ∪ Redis 历史粘滞，并写回 Redis。

    Redis 不可用时：仍返回本轮 match（降级），并打警告——
    不静默改用进程内存表（避免多 worker 不一致）。
    """
    current = match_skills(user_input)
    previous = get_session_skills(session_id)
    merged = merge_session_skills(previous, current)
    if merged:
        set_session_skills(session_id, merged)
    return merged
