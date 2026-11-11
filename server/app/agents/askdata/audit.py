"""写入 biz_query_audit（失败不影响主流程）。"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.infra.db import execute

logger = logging.getLogger(__name__)


def write_audit(
    *,
    user_id: int,
    unit_id: int,
    role_id: int,
    session_id: Optional[str],
    question: str,
    route: str,
    sql_text: Optional[str],
    success: bool,
    denied: bool,
    error_message: Optional[str] = None,
    row_count: Optional[int] = None,
    latency_ms: Optional[int] = None,
) -> None:
    try:
        execute(
            "INSERT INTO biz_query_audit "
            "(user_id, unit_id, role_id, session_id, question, route, sql_text, "
            " success, denied, error_message, row_count, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                int(user_id),
                int(unit_id),
                int(role_id),
                session_id,
                question[:4000],
                route[:32],
                (sql_text or "")[:8000] or None,
                1 if success else 0,
                1 if denied else 0,
                (error_message or "")[:512] or None,
                row_count,
                latency_ms,
            ),
        )
    except Exception:
        # 审计表可能尚未迁移；不能阻断问答
        logger.exception("askdata audit write failed")
