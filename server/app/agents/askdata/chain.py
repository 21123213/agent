"""
AskData 同步编排（评测 / 兼容保留）。

线上 SSE 已改走 Reasoner + askdata skill（见 ``agent.astream_askdata``）。
本模块 ``answer_question`` 仍供 ``test_code/askdata_eval.py`` 与单测直调；
稳定后可删除，工具层直接复用 permissions / sql_guard / executor。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from app.agents.askdata import audit as ask_audit
from app.agents.askdata import executor as ask_exec
from app.agents.askdata import metrics as ask_metrics
from app.agents.askdata import nl2sql as ask_nl2sql
from app.agents.askdata import permissions as ask_perm
from app.agents.askdata.intent import classify_intent
from app.agents.askdata.sql_guard import guard_sql
logger = logging.getLogger(__name__)


def _jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Decimal/日期转可 JSON 类型，便于 SSE 与单测断言。"""
    out: list[dict[str, Any]] = []
    for r in rows:
        item: dict[str, Any] = {}
        for k, v in r.items():
            if isinstance(v, Decimal):
                item[k] = float(v)
            elif hasattr(v, "isoformat"):
                item[k] = v.isoformat()
            else:
                item[k] = v
        out.append(item)
    return out


def _format_answer(
    *,
    rows: list[dict[str, Any]],
    sql: Optional[str],
    metric_name: Optional[str] = None,
    extra: str = "",
) -> str:
    """把结果整理成对用户可读的 Markdown。"""
    parts: list[str] = []
    if metric_name:
        parts.append(f"【按标准指标「{metric_name}」查询】")
    if extra:
        parts.append(extra)
    if not rows:
        parts.append("查询成功，但没有返回行（可能是筛选条件下无数据）。")
    elif len(rows) == 1 and len(rows[0]) <= 4:
        kv = "，".join(f"{k}={v}" for k, v in rows[0].items())
        parts.append(f"结果：{kv}")
    else:
        parts.append(f"共 {len(rows)} 行。预览：")
        preview = rows[:5]
        parts.append(
            "```json\n" + json.dumps(preview, ensure_ascii=False, indent=2) + "\n```"
        )
    if sql:
        parts.append(f"\n```sql\n{sql}\n```")
    return "\n".join(parts)


@dataclass
class AskResult:
    """一次问数的结构化结果（供 SSE / 评测）。"""

    ok: bool
    route: str
    answer: str
    sql: Optional[str] = None
    rows: list[dict[str, Any]] = field(default_factory=list)
    denied: bool = False
    error: Optional[str] = None
    selected_tables: list[str] = field(default_factory=list)
    latency_ms: int = 0


def answer_question(
    *,
    user: dict[str, Any],
    question: str,
    session_id: Optional[str] = None,
    max_rows: int = 200,
) -> AskResult:
    """
    核心同步问答。

    不写会话消息（由 agent 层负责双写）；本函数负责算答案 + 审计。
    """
    t0 = time.perf_counter()
    q = (question or "").strip()
    if not q:
        return AskResult(ok=False, route="refuse", answer="请输入问题。", denied=True)

    intent = classify_intent(q)

    # ----- 拒绝写 -----
    if intent.kind == "refuse_write":
        res = AskResult(
            ok=True,
            route="refuse",
            answer=intent.reason,
            denied=True,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        ask_audit.write_audit(
            user_id=int(user["id"]),
            unit_id=int(user["unit_id"]),
            role_id=int(user["role_id"]),
            session_id=session_id,
            question=q,
            route="refuse",
            sql_text=None,
            success=True,
            denied=True,
            error_message=intent.reason,
            latency_ms=res.latency_ms,
        )
        return res

    # ----- 口径文档 -----
    if intent.kind == "metric_doc" and intent.metric:
        text = ask_metrics.doc_answer(intent.metric.doc_key or "")
        res = AskResult(
            ok=True,
            route="metric_doc",
            answer=text,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        ask_audit.write_audit(
            user_id=int(user["id"]),
            unit_id=int(user["unit_id"]),
            role_id=int(user["role_id"]),
            session_id=session_id,
            question=q,
            route="metric_doc",
            sql_text=None,
            success=True,
            denied=False,
            latency_ms=res.latency_ms,
        )
        return res

    sql: Optional[str] = None
    params: dict[str, Any] = {}
    selected: list[str] = []
    route = intent.kind
    metric_name: Optional[str] = None

    try:
        if intent.kind == "metric" and intent.metric and intent.metric.sql:
            metric_name = intent.metric.name
            sql, params = ask_perm.apply_metric_unit_placeholders(
                intent.metric.sql, user
            )
            selected = ["biz_daily_sales_agg"] if "biz_daily_sales_agg" in sql else []
        else:
            # NL2SQL
            route = "nl2sql"
            sql, selected = ask_nl2sql.generate_sql(q, user)
            sql, params = ask_perm.inject_unit_predicate_simple(sql, user)

        # 列权限
        col_deny = ask_perm.sql_mentions_denied_columns(sql or "", user)
        if col_deny:
            res = AskResult(
                ok=True,
                route=route,
                answer=col_deny,
                sql=sql,
                denied=True,
                selected_tables=selected,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            ask_audit.write_audit(
                user_id=int(user["id"]),
                unit_id=int(user["unit_id"]),
                role_id=int(user["role_id"]),
                session_id=session_id,
                question=q,
                route=route,
                sql_text=sql,
                success=False,
                denied=True,
                error_message=col_deny,
                latency_ms=res.latency_ms,
            )
            return res

        guarded = guard_sql(sql or "", max_rows=max_rows)
        if not guarded.ok:
            res = AskResult(
                ok=False,
                route=route,
                answer=f"SQL 未通过安全校验：{guarded.reason}",
                sql=sql,
                denied=True,
                error=guarded.reason,
                selected_tables=selected,
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
            ask_audit.write_audit(
                user_id=int(user["id"]),
                unit_id=int(user["unit_id"]),
                role_id=int(user["role_id"]),
                session_id=session_id,
                question=q,
                route=route,
                sql_text=sql,
                success=False,
                denied=True,
                error_message=guarded.reason,
                latency_ms=res.latency_ms,
            )
            return res

        rows = ask_exec.run_select(guarded.sql, params, max_rows=max_rows)
        rows_j = _jsonable_rows(rows)
        admin_note = (
            "（跨 unit 视图）" if ask_perm.askdata_cross_unit(user) else ""
        )
        answer = _format_answer(
            rows=rows_j,
            sql=guarded.sql,
            metric_name=metric_name,
            extra=admin_note,
        )

        res = AskResult(
            ok=True,
            route=route,
            answer=answer,
            sql=guarded.sql,
            rows=rows_j,
            selected_tables=selected,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        ask_audit.write_audit(
            user_id=int(user["id"]),
            unit_id=int(user["unit_id"]),
            role_id=int(user["role_id"]),
            session_id=session_id,
            question=q,
            route=route,
            sql_text=guarded.sql,
            success=True,
            denied=False,
            row_count=len(rows_j),
            latency_ms=res.latency_ms,
        )
        return res

    except Exception as exc:
        logger.exception("askdata answer failed")
        res = AskResult(
            ok=False,
            route=route,
            answer=f"查询失败：{exc}",
            sql=sql,
            error=str(exc),
            selected_tables=selected,
            latency_ms=int((time.perf_counter() - t0) * 1000),
        )
        ask_audit.write_audit(
            user_id=int(user["id"]),
            unit_id=int(user["unit_id"]),
            role_id=int(user["role_id"]),
            session_id=session_id,
            question=q,
            route=route,
            sql_text=sql,
            success=False,
            denied=False,
            error_message=str(exc)[:512],
            latency_ms=res.latency_ms,
        )
        return res
