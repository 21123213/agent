"""
文件处理入队。

优先 Celery（Redis broker）；本机无 Redis 时回退到进程内线程池，
保证上传后仍能跑完解析→向量化（面试演示不卡死）。

链路
----
1. 尝试写 Redis 进度 / 锁（失败只打日志）
2. process_file_task.delay(file_id)
3. 若 broker 不可达 → threading 调 process_file(file_id)
"""
from __future__ import annotations

import logging
import threading
import uuid

from app.infra import redis_client as redis_mgr

logger = logging.getLogger(__name__)


def _set_queued_progress(file_id: int, message: str) -> None:
    task_id = f"file-{file_id}-{uuid.uuid4().hex[:8]}"
    try:
        redis_mgr.acquire_file_lock(file_id, task_id, ttl=600)
        redis_mgr.set_task_progress(
            file_id,
            status="pending",
            progress=0,
            current_step="queued",
            message=message,
            processed_chunks=0,
        )
    except Exception as exc:
        logger.warning("redis progress/lock skipped: %s", exc)


def _run_in_thread(file_id: int) -> None:
    """进程内回退：不依赖 Redis/Celery。"""
    from app.agents.rag.pipeline.process import process_file

    try:
        process_file(int(file_id))
    except Exception:
        logger.exception("inline process_file failed file_id=%s", file_id)


def enqueue_process_file(file_id: int) -> str:
    """
    返回实际调度方式：\"celery\" | \"thread\"。
    """
    _set_queued_progress(file_id, "已入队，等待处理")

    try:
        from app.agents.rag.tasks import process_file_task

        async_result = process_file_task.delay(file_id)
        logger.info(
            "enqueued celery task file_id=%s task_id=%s",
            file_id,
            async_result.id,
        )
        return "celery"
    except Exception as exc:
        logger.warning(
            "Celery unavailable (%s); fallback to in-process thread for file_id=%s",
            exc,
            file_id,
        )
        _set_queued_progress(file_id, "Celery 不可用，已用进程内线程处理")
        t = threading.Thread(
            target=_run_in_thread,
            args=(file_id,),
            name=f"process-file-{file_id}",
            daemon=True,
        )
        t.start()
        return "thread"
