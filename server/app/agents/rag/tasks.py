"""
Celery 任务：

1. process_file / embed_file — 一文件一任务入库向量化
2. extract_user_memory — 异步抽取用户长期记忆（不阻塞 SSE）

HTTP 上传合法文件后::
    process_file_task.delay(file_id)

问答每 N 轮::
    extract_user_memory_task.delay(session_id, user_id, unit_id)
"""
from __future__ import annotations

import logging

from app.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="agents.rag.process_file",
    max_retries=2,
    default_retry_delay=30,
    acks_late=True,
)
def process_file_task(self, file_id: int):
    """解析 → 切片 →（可选增强）→ 向量化。"""
    from app.agents.rag.pipeline.process import process_file
    from app.infra.db import set_pool_purpose

    set_pool_purpose("celery")
    try:
        return process_file(int(file_id))
    except Exception as exc:
        logger.exception("celery process_file_task file_id=%s", file_id)
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            raise


@celery_app.task(name="agents.rag.embed_file", bind=True, max_retries=1)
def embed_file_task(self, file_id: int):
    """
    仅重跑向量化（解析失败以外的场景可扩展）。
    当前直接复用完整 process_file，保证切片与向量一致。
    """
    return process_file_task(file_id)


@celery_app.task(
    bind=True,
    name="agents.rag.extract_user_memory",
    max_retries=2,
    default_retry_delay=20,
    acks_late=True,
)
def extract_user_memory_task(
    self,
    session_id: str,
    user_id: int,
    unit_id: int,
    scenario: str = "rag_qa",
):
    """
    从会话近几轮对话抽取长期记忆 → MySQL + Milvus + Redis。

    请求路径只 delay 本任务；失败可重试，以 DB 行为准。
    """
    from app.agents.rag.memory import user_memory as user_memory_mod
    from app.infra import chat_repo
    from app.infra.db import set_pool_purpose

    set_pool_purpose("celery")
    try:
        # 取足够轮次供抽取器判断「稳定偏好」
        messages = chat_repo.get_recent_messages(session_id, limit=12)
        slim = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in messages
            if m.get("role") in ("user", "assistant")
        ]
        extracted = user_memory_mod.extract_from_dialogue(
            slim, scenario=scenario
        )
        if not extracted:
            logger.info(
                "extract_user_memory skip session=%s user=%s (nothing to save)",
                session_id,
                user_id,
            )
            return {"saved": False, "session_id": session_id}

        mid = user_memory_mod.persist_memory(
            user_id=int(user_id),
            unit_id=int(unit_id),
            content=extracted["content"],
            category=extracted.get("category") or "preference",
            scenario=extracted.get("scenario") or scenario,
            agent_type="rag",
            confidence=float(extracted.get("confidence") or 0.8),
            source_session_id=session_id,
        )
        logger.info(
            "extract_user_memory saved memory_id=%s user=%s session=%s",
            mid,
            user_id,
            session_id,
        )
        return {"saved": True, "memory_id": mid, "session_id": session_id}
    except Exception as exc:
        logger.exception(
            "extract_user_memory_task failed session=%s user=%s",
            session_id,
            user_id,
        )
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            raise
