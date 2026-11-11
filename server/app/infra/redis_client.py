"""
Redis：文件进度 / 会话窗口 / 用户记忆热缓存 / 语料版本。

命名空间（切勿混用 clear）
--------------------------
- file:{id}:progress / lock:file:...     入库进度与防重入
- session:{sid}:context                  会话短记忆滑动窗口（TTL 可配）
- rag:qa:...                             QA 答案缓存（见 agents/rag/cache/qa_cache）
- unit:{unit_id}:kb:{kb_id}:corpus_ver   语料世代；文件入库/删除成功时 INCR
- user:{uid}:memory:recent               长期记忆热列表缓存

链路（文件）
----
上传入队前：acquire_file_lock → set_task_progress(pending)
Celery 各阶段：set_task_progress(processing, step=parsing|chunking|...)
详情页轮询：get_task_progress；若无则回落到 MySQL file_metadata.status
结束 / 失败：release_file_lock +（可选）clear_task_progress
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import redis

from app.settings import get_settings

logger = logging.getLogger(__name__)

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """懒连接；ping 失败抛错，由调用方决定是否忽略（进度非强依赖）。"""
    global _client
    if _client is None:
        s = get_settings()
        password = s.redis_password or None
        _client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=s.redis_db,
            password=password,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    # 每次轻量探活；挂了则清单例，下次重建
    try:
        _client.ping()
    except Exception:
        _client = None
        s = get_settings()
        password = s.redis_password or None
        _client = redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=s.redis_db,
            password=password,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        _client.ping()
    return _client


def redis_available() -> bool:
    try:
        get_redis()
        return True
    except Exception:
        return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def set_task_progress(
    file_id: int,
    *,
    status: str,
    progress: int,
    current_step: str,
    message: str,
    processed_chunks: int | None = None,
) -> None:
    key = f"file:{file_id}:progress"
    data: dict[str, Any] = {
        "status": status,
        "progress": str(int(progress)),
        "current_step": current_step,
        "message": message,
        "updated_at": _now_iso(),
    }
    if processed_chunks is not None:
        data["processed_chunks"] = str(int(processed_chunks))
    client = get_redis()
    # 兼容旧 Redis：逐字段 HSET（避免 mapping 形式参数报错）
    pipe = client.pipeline()
    for field, value in data.items():
        pipe.hset(key, field, value)
    pipe.expire(key, 3600)
    pipe.execute()


def get_task_progress(file_id: int) -> Optional[dict[str, Any]]:
    raw = get_redis().hgetall(f"file:{file_id}:progress")
    if not raw:
        return None
    out: dict[str, Any] = dict(raw)
    if "progress" in out:
        try:
            out["progress"] = int(out["progress"])
        except (TypeError, ValueError):
            out["progress"] = 0
    if "processed_chunks" in out:
        try:
            out["processed_chunks"] = int(out["processed_chunks"])
        except (TypeError, ValueError):
            out["processed_chunks"] = 0
    return out


def clear_task_progress(file_id: int) -> None:
    get_redis().delete(f"file:{file_id}:progress")


def acquire_file_lock(file_id: int, task_id: str, ttl: int = 600) -> bool:
    """SET NX：同一 file 同时只允许一个处理任务。"""
    key = f"lock:file:{file_id}:processing"
    return bool(get_redis().set(key, task_id, nx=True, ex=ttl))


def release_file_lock(file_id: int) -> None:
    get_redis().delete(f"lock:file:{file_id}:processing")


# ---------------------------------------------------------------------------
# 会话短记忆窗口：session:{sid}:context
# ---------------------------------------------------------------------------


def save_session_message(
    session_id: str,
    role: str,
    content: str,
    sources: list | None = None,
    *,
    enforce: bool = True,
) -> None:
    """
    追加一条消息到 Redis 窗口。

    enforce=True（默认）时跑 3 轮窗口 + 摘要压缩。
    一轮问答若先写 user 再写 assistant，**必须**对 user 传 enforce=False、
    仅在 assistant 上 enforce=True，否则窗口已满时会连续打两次压缩 LLM
    （LangSmith 可见两条「请将以下对话压缩…」）。

    Redis 失败由调用方吞掉并记日志；DB 仍为真相源。
    """
    from app.agents.rag.memory.session_memory import enforce_memory_limits

    s = get_settings()
    key = f"session:{session_id}:context"
    context = get_session_context(session_id)
    message: dict[str, Any] = {
        "role": role,
        "content": content,
        "timestamp": _now_iso(),
    }
    if sources:
        message["sources"] = sources
    context.append(message)
    if enforce:
        context = enforce_memory_limits(context)
    get_redis().setex(
        key,
        int(s.context_cache_ttl),
        json.dumps(context, ensure_ascii=False),
    )


def get_session_context(session_id: str) -> list[dict[str, Any]]:
    """读取 Redis 窗口；miss 返回 []（调用方应从 MySQL 回填）。"""
    try:
        raw = get_redis().get(f"session:{session_id}:context")
    except Exception as exc:
        logger.warning("get_session_context failed sid=%s: %s", session_id, exc)
        return []
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def clear_session_context(session_id: str) -> None:
    try:
        get_redis().delete(f"session:{session_id}:context")
    except Exception as exc:
        logger.warning("clear_session_context failed sid=%s: %s", session_id, exc)


def set_session_context(session_id: str, messages: list[dict[str, Any]]) -> None:
    """整窗覆盖写入（冷启动回填后用）。"""
    s = get_settings()
    try:
        get_redis().setex(
            f"session:{session_id}:context",
            int(s.context_cache_ttl),
            json.dumps(messages, ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("set_session_context failed sid=%s: %s", session_id, exc)


# ---------------------------------------------------------------------------
# 语料世代：unit:{uid}:kb:{kb}:corpus_ver（QA 缓存键分量）
# ---------------------------------------------------------------------------


def corpus_ver_key(unit_id: int, knowledge_base_id: int) -> str:
    return f"unit:{int(unit_id)}:kb:{int(knowledge_base_id)}:corpus_ver"


def get_corpus_version(unit_id: int, knowledge_base_id: int) -> int:
    try:
        raw = get_redis().get(corpus_ver_key(unit_id, knowledge_base_id))
    except Exception:
        return 0
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def bump_corpus_version(unit_id: int, knowledge_base_id: int) -> int:
    """
    文件入库成功 / 删除文件 / 删 KB 后调用。
    INCR 后旧 rag:qa key 因 ver 对不上自然 miss，无需 SCAN。
    """
    key = corpus_ver_key(unit_id, knowledge_base_id)
    try:
        new_ver = int(get_redis().incr(key))
        logger.info(
            "corpus_ver bumped unit=%s kb=%s -> %s",
            unit_id,
            knowledge_base_id,
            new_ver,
        )
        return new_ver
    except Exception as exc:
        logger.warning("bump_corpus_version failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# 用户长期记忆热缓存
# ---------------------------------------------------------------------------


def set_user_memory_cache(user_id: int, items: list[dict[str, Any]], ttl: int = 1800) -> None:
    try:
        get_redis().setex(
            f"user:{int(user_id)}:memory:recent",
            int(ttl),
            json.dumps(items, ensure_ascii=False),
        )
    except Exception as exc:
        logger.warning("set_user_memory_cache failed uid=%s: %s", user_id, exc)


def get_user_memory_cache(user_id: int) -> list[dict[str, Any]] | None:
    """命中返回 list；miss 返回 None（与空列表区分，便于决定是否打向量）。"""
    try:
        raw = get_redis().get(f"user:{int(user_id)}:memory:recent")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return None


def clear_user_memory_cache(user_id: int) -> None:
    try:
        get_redis().delete(f"user:{int(user_id)}:memory:recent")
    except Exception:
        pass
