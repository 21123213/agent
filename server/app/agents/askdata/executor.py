"""
只读执行：参数化查询 + 行数上限。

连接仍用统一 DB 池；安全性依赖 sql_guard，而非单独只读账号。
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

from app.infra.db import get_conn

logger = logging.getLogger(__name__)


def run_select(
    sql: str,
    params: Optional[dict[str, Any] | Sequence[Any]] = None,
    *,
    max_rows: int = 200,
) -> list[dict[str, Any]]:
    """
    执行 SELECT。

    - dict params：配合 %(name)s（指标模板）
    - sequence：配合 %s
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            # 无绑定参数时不要传 ()：PyMySQL 仍会 %-format，
            # LIKE '%华北%' 会触发 "not enough arguments for format string"。
            if params is None or (
                isinstance(params, (dict, list, tuple)) and len(params) == 0
            ):
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            rows = cur.fetchmany(int(max_rows)) or []
            return [dict(r) for r in rows]
