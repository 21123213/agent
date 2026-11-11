"""
Celery 应用入口。

启动 worker（仓库根，已 activate venv）::

    celery -A app.celery_app.celery_app worker --loglevel=info --pool=solo

Windows 建议 ``--pool=solo``（避免 prefork 问题）。
Worker 进程启动时设置 DB 池 purpose=celery，与 Web 进程隔离。
"""
from __future__ import annotations

import os

# HF 镜像须在导入 transformers / unstructured 之前
from app.settings import get_settings

_s = get_settings()
if _s.hf_endpoint:
    os.environ.setdefault("HF_ENDPOINT", _s.hf_endpoint)

from celery import Celery
from celery.signals import worker_process_init

celery_app = Celery(
    "agents",
    broker=_s.effective_celery_broker,
    backend=_s.effective_celery_backend,
    include=["app.agents.rag.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Asia/Shanghai",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    # 一文件一任务；失败有限次重试由任务装饰器控制
)


@worker_process_init.connect
def _on_worker_init(**_kwargs):
    from app.infra.db import set_pool_purpose

    set_pool_purpose("celery")
