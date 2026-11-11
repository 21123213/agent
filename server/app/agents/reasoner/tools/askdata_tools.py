"""
AskData Skill 工具集（挂在 Reasoner 上）。

推荐调用顺序（见 prompts/reasoner/skills/askdata/SKILL.md）
----------------------------------------------------------
1. askdata_route          — 指标 / 口径 / NL2SQL / 拒绝写
2. askdata_draft_sql      — 仅 nl2sql；指标可跳过（route 已带草稿）
3. askdata_probe          — 探查执行，**返回完整行**；空结果附黑话提示
4. askdata_permission_rules — 给出 unit/role/白名单规则包（给主模型拼完整 SQL）
5. askdata_validate_and_run — 代码 guard + 列权限 + 小模型校验 + 执行

安全边界
--------
- 拼 SQL 可以交给主模型；**放行**必须过本模块的 guard / 列检 /（可选）小模型。
- 探查阶段也会注入 unit（非 admin）并过 guard，避免跨租户泄露。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool, tool

from app.agents.askdata import audit as ask_audit
from app.agents.askdata import draft as ask_draft
from app.agents.askdata import executor as ask_exec
from app.agents.askdata import metrics as ask_metrics
from app.agents.askdata import permissions as ask_perm
from app.agents.askdata import slang as ask_slang
from app.agents.askdata import tool_context as ask_ctx
from app.agents.askdata import validator as ask_validator
from app.agents.askdata.intent import classify_intent
from app.agents.askdata.schema_catalog import (
    all_business_tables,
    format_catalog_for_prompt,
)
from app.agents.askdata.sql_guard import DEFAULT_ALLOWED_TABLES, guard_sql
from app.agents.reasoner.tools.core import _wrap_async
from app.common import tracing as rag_trace
from app.common.llm_client import invoke_chat
from app.common.permissions import is_admin
from app.settings import get_settings

logger = logging.getLogger(__name__)


def _remember(stage: str, data: dict[str, Any]) -> None:
    """同轮工具轨迹：写入 AskdataToolContext.events（JSONL 已由 rag_trace 负责）。"""
    ctx = ask_ctx.get()
    if ctx is not None:
        ctx.events.append({"stage": stage, **dict(data)})


def _emit_guard(
    *,
    ok: bool,
    reason: str = "",
    sql_preview: str = "",
    bad_tables: Optional[list[str]] = None,
    denied_column: Optional[str] = None,
) -> None:
    """白名单 / sql_guard / 列拒绝 — 走 common.tracing.emit。"""
    data = {
        "ok": ok,
        "denied": not ok,
        "guard_reason": reason,
        "sql_preview": rag_trace.truncate_text(sql_preview, max_len=500),
        "bad_tables": list(bad_tables or []),
        "denied_column": denied_column,
    }
    rag_trace.emit(
        "askdata.guard",
        status="ok" if ok else "denied",
        data=data,
        message="" if ok else reason,
    )
    _remember("askdata.guard", data)

_DRAFT_PROMPT = (
    Path(__file__).resolve().parents[4] / "prompts" / "askdata_draft.txt"
)


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return float(o)
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def _jsonable_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
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


def _load_draft_system() -> str:
    if _DRAFT_PROMPT.is_file():
        return _DRAFT_PROMPT.read_text(encoding="utf-8")
    return "只输出问数草稿 JSON。非查询设 refused=true。"


def _prepare_sql_for_exec(
    sql: str,
    user: dict[str, Any],
    *,
    max_rows: int,
) -> tuple[Optional[str], dict[str, Any], Optional[str]]:
    """
    注入 unit（非 admin）→ 列权限检查 → sql_guard。

    成功返回 (guarded_sql, params, None)；失败返回 (None, params, reason)。
    """
    sql2, params = ask_perm.inject_unit_predicate_simple(sql, user)
    # 指标模板占位符兜底
    if "{unit_filter" in sql2:
        sql2, params = ask_perm.apply_metric_unit_placeholders(sql2, user)

    col_deny = ask_perm.sql_mentions_denied_columns(sql2, user)
    if col_deny:
        _emit_guard(
            ok=False, reason=col_deny, sql_preview=sql2, denied_column=col_deny
        )
        return None, params, col_deny

    guarded = guard_sql(sql2, max_rows=max_rows)
    if not guarded.ok:
        _emit_guard(ok=False, reason=guarded.reason, sql_preview=sql2)
        return None, params, guarded.reason

    _emit_guard(ok=True, reason="ok", sql_preview=guarded.sql)
    return guarded.sql, params, None


# ---------------------------------------------------------------------------
# 工具实现（同步核；对外 to_thread + _wrap_async）
# ---------------------------------------------------------------------------


async def _run_sync(fn: Callable[..., str], *args: Any, **kwargs: Any) -> str:
    """把同步工具核丢进线程池，供 _wrap_async await。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


def _route_impl(question: str) -> str:
    ctx = ask_ctx.require()
    with rag_trace.span("askdata.route") as tr:
        intent = classify_intent(question)
        out: dict[str, Any] = {
            "ok": True,
            "route": intent.kind,
            "reason": intent.reason,
            "next": "",
            "draft": None,
            "doc_answer": None,
        }

        if intent.kind == "refuse_write":
            out["next"] = "stop"
            out["ok"] = True
            out["denied"] = True
            out["message"] = intent.reason
            tr.update({"route": "refuse_write", "denied": True, "next": "stop"})
            _remember("askdata.route", tr)
            return _json(out)

        if intent.kind == "metric_doc" and intent.metric:
            text = ask_metrics.doc_answer(intent.metric.doc_key or "")
            out["next"] = "answer_doc"
            out["doc_answer"] = text
            out["metric_id"] = intent.metric.id
            out["metric_name"] = intent.metric.name
            tr.update(
                {
                    "route": "metric_doc",
                    "metric": intent.metric.id,
                    "next": "answer_doc",
                }
            )
            _remember("askdata.route", tr)
            return _json(out)

        if intent.kind == "metric" and intent.metric and intent.metric.sql:
            # 指标：先套 unit 占位，形成统一草稿；可跳过 draft_sql
            sql, _params = ask_perm.apply_metric_unit_placeholders(
                intent.metric.sql, ctx.user
            )
            tables = (
                ["biz_daily_sales_agg"]
                if "biz_daily_sales_agg" in sql
                else all_business_tables()
            )
            draft = ask_draft.draft_from_metric_sql(
                sql, tables=tables, metric_name=intent.metric.name
            )
            out["next"] = "probe"
            out["metric_id"] = intent.metric.id
            out["metric_name"] = intent.metric.name
            out["draft"] = draft
            tr.update(
                {
                    "route": "metric",
                    "metric": intent.metric.id,
                    "tables": tables,
                    "next": "probe",
                }
            )
            _remember("askdata.route", tr)
            return _json(out)

        # nl2sql（全量业务表，不再做域路由）
        out["route"] = "nl2sql"
        out["next"] = "draft_sql"
        out["tables"] = all_business_tables()
        tr.update({"route": "nl2sql", "tables": out["tables"], "next": "draft_sql"})
        _remember("askdata.route", tr)
        return _json(out)


def _draft_sql_impl(question: str) -> str:
    ask_ctx.require()
    q = (question or "").strip()
    with rag_trace.span("askdata.draft_sql") as tr:
        # 写操作二次拦截（即使跳过 route）
        intent = classify_intent(q)
        if intent.kind == "refuse_write":
            draft = ask_draft.empty_draft(
                refused=True, refuse_reason=intent.reason
            )
            tr.update({"refused": True, "denied": True, "next": "stop"})
            _remember("askdata.draft_sql", tr)
            return _json({"ok": True, "draft": draft, "next": "stop"})

        tables = all_business_tables()
        catalog = format_catalog_for_prompt(tables)
        slang_hits = ask_slang.match_slang(q)
        slang_block = ask_slang.format_slang_hints(slang_hits)

        human = f"可用 Schema（全量业务表）:\n{catalog}\n\n"
        if slang_block:
            human += f"黑话提示:\n{slang_block}\n\n"
        human += f"用户问题：{q}\n\n请输出草稿 JSON。"

        raw = invoke_chat(
            [
                SystemMessage(content=_load_draft_system()),
                HumanMessage(content=human),
            ],
            purpose="strong",
            temperature=0.0,
            reason="askdata_draft_sql",
        )
        draft = ask_draft.parse_draft_json(raw)
        if not draft.get("tables"):
            draft["tables"] = tables

        next_step = "stop" if draft.get("refused") else "probe"
        tr.update(
            {
                "tables": draft.get("tables"),
                "fields": draft.get("fields"),
                "order_by": draft.get("order_by"),
                "limit": draft.get("limit"),
                "sql_count": len(draft.get("sqls") or []),
                "refused": draft.get("refused"),
                "denied": bool(draft.get("refused")),
                "next": next_step,
            }
        )
        _remember("askdata.draft_sql", tr)
        return _json(
            {
                "ok": True,
                "draft": draft,
                "slang_hits": slang_hits,
                "next": next_step,
                "message": draft.get("refuse_reason") if draft.get("refused") else "",
            }
        )


def _probe_impl(sql: str, question: str = "") -> str:
    """探查：返回完整行；无数据则附黑话澄清建议。"""
    ctx = ask_ctx.require()
    max_rows = int(ctx.max_rows or get_settings().askdata_max_rows or 200)
    sql_in = (sql or "").strip()
    with rag_trace.span("askdata.probe") as tr:
        if not sql_in:
            tr.update({"error": "sql 不能为空", "next": "draft_sql"})
            _remember("askdata.probe", tr)
            return _json({"ok": False, "error": "sql 不能为空", "next": "draft_sql"})

        guarded_sql, params, err = _prepare_sql_for_exec(
            sql_in, ctx.user, max_rows=max_rows
        )
        if err or not guarded_sql:
            tr.update({"error": err, "denied": True, "next": "stop"})
            _remember("askdata.probe", tr)
            return _json(
                {
                    "ok": False,
                    "denied": True,
                    "error": err or "SQL 未通过守卫",
                    "next": "stop",
                    "message": (
                        "查询未通过安全校验（只读/白名单/列权限）。"
                        "请仅陈述该事实，不要建议如何绕过。"
                    ),
                }
            )

        rows = _jsonable_rows(
            ask_exec.run_select(guarded_sql, params, max_rows=max_rows)
        )
        has_data = len(rows) > 0
        slang_hits = ask_slang.match_slang(question) if not has_data else []
        out: dict[str, Any] = {
            "ok": True,
            "has_data": has_data,
            "row_count": len(rows),
            "rows": rows,
            "sql_executed": guarded_sql,
            "next": "permission_rules" if has_data else "clarify_semantics",
        }
        if not has_data:
            out["slang_hits"] = slang_hits
            out["clarify_prompt"] = (
                ask_slang.format_slang_hints(slang_hits)
                or "查询成功但无数据。请确认筛选条件、时间范围或业务黑话是否理解有误，再向用户澄清。"
            )
        tr.update(
            {
                "has_data": has_data,
                "row_count": len(rows),
                "sql_preview": rag_trace.truncate_text(guarded_sql, max_len=500),
                "next": out["next"],
            }
        )
        _remember("askdata.probe", tr)
        return _json(out)


def _permission_rules_impl() -> str:
    """返回给主模型拼完整 SQL 用的规则包（unit + role 列 + 表白名单）。"""
    ctx = ask_ctx.require()
    user = ctx.user
    with rag_trace.span("askdata.permission_rules") as tr:
        role = ask_perm.role_code(user)
        denied = sorted(ask_perm.denied_columns_for_user(user))
        max_rows = int(ctx.max_rows or get_settings().askdata_max_rows or 200)
        admin = is_admin(user)
        cross_unit = ask_perm.askdata_cross_unit(user)
        rules = ask_validator.build_rules_package(
            user=user,
            role_code=role,
            denied_columns=denied,
            allowed_tables=sorted(DEFAULT_ALLOWED_TABLES),
            max_rows=max_rows,
            is_admin=admin,
        )
        unit_id = None if cross_unit else int(user.get("unit_id") or 0)
        # 权限结果单独 emit，便于按 stage=askdata.permission 过滤排查
        perm_data = {
            "ok": True,
            "unit_id": unit_id,
            "role_code": role,
            "denied_columns": denied,
            "cross_unit": cross_unit,
            "unit_required": not cross_unit,
            "max_rows": max_rows,
        }
        rag_trace.emit("askdata.permission", status="ok", data=perm_data)
        _remember("askdata.permission", perm_data)
        tr.update(perm_data)
        _remember("askdata.permission_rules", tr)
        return _json(
            {
                "ok": True,
                "rules": rules,
                "next": "finalize_then_validate_and_run",
                "instruction": (
                    "请根据 rules 与此前 draft（含 order_by/limit）改写出完整 SQL，"
                    "再调用 askdata_validate_and_run。"
                    "不要向用户解释如何绕过权限或白名单。"
                ),
            }
        )


def _validate_and_run_impl(sql: str, question: str = "") -> str:
    """
    代码硬闸 + 小模型校验 + 执行 + 审计。

    两者都通过才返回行数据；否则 denied，主模型只陈述事实。
    """
    ctx = ask_ctx.require()
    user = ctx.user
    max_rows = int(ctx.max_rows or get_settings().askdata_max_rows or 200)
    sql_in = (sql or "").strip()
    q = (question or "").strip() or "(askdata_validate_and_run)"

    with rag_trace.span("askdata.validate_and_run") as tr:
        if not sql_in:
            tr.update({"error": "sql 不能为空"})
            _remember("askdata.validate_and_run", tr)
            return _json({"ok": False, "error": "sql 不能为空"})

        role = ask_perm.role_code(user)
        denied_cols = sorted(ask_perm.denied_columns_for_user(user))
        admin = is_admin(user)
        rules = ask_validator.build_rules_package(
            user=user,
            role_code=role,
            denied_columns=denied_cols,
            allowed_tables=sorted(DEFAULT_ALLOWED_TABLES),
            max_rows=max_rows,
            is_admin=admin,
        )

        t0 = time.perf_counter()
        guarded_sql, params, err = _prepare_sql_for_exec(
            sql_in, user, max_rows=max_rows
        )
        if err or not guarded_sql:
            latency = int((time.perf_counter() - t0) * 1000)
            ask_audit.write_audit(
                user_id=int(user["id"]),
                unit_id=int(user["unit_id"]),
                role_id=int(user["role_id"]),
                session_id=ctx.session_id,
                question=q,
                route="validate_run",
                sql_text=sql_in,
                success=False,
                denied=True,
                error_message=err,
                latency_ms=latency,
            )
            tr.update(
                {
                    "denied": True,
                    "fail_stage": "code_guard",
                    "error": err,
                }
            )
            _remember("askdata.validate_and_run", tr)
            return _json(
                {
                    "ok": False,
                    "denied": True,
                    "stage": "code_guard",
                    "error": err,
                    "message": (
                        "因权限或白名单限制，无法执行该查询。"
                        "请只告知用户这一事实，禁止提供绕过建议。"
                    ),
                    "rules": rules,
                }
            )

        # 小模型校验（完整规则 + 完整 SQL；span 上暴露规则摘要便于 LangSmith 阅读）
        with rag_trace.span(
            "askdata.validate_llm",
            sql_preview=rag_trace.truncate_text(guarded_sql, max_len=500),
        ) as vtr:
            vtr.update(
                {
                    "validator_system_preview": rag_trace.truncate_text(
                        ask_validator._SYSTEM, max_len=400
                    ),
                    "rules_preview": {
                        "role_code": rules.get("role_code"),
                        "unit_id": rules.get("unit_id"),
                        "unit_required": rules.get("unit_required"),
                        "denied_columns": rules.get("denied_columns"),
                        "allowed_tables": (rules.get("allowed_tables") or [])[:20],
                        "max_rows": rules.get("max_rows"),
                        "read_only": rules.get("read_only"),
                        "hint_for_llm": rag_trace.truncate_text(
                            rules.get("hint_for_llm") or "", max_len=200
                        ),
                    },
                }
            )
            v = ask_validator.validate_sql_with_small_model(guarded_sql, rules)
            vtr.update(
                {
                    "validator_pass": bool(v.get("pass")),
                    "validator_reason": v.get("reason"),
                    "denied": not bool(v.get("pass")),
                }
            )
            _remember("askdata.validate_llm", vtr)

        if not v.get("pass"):
            latency = int((time.perf_counter() - t0) * 1000)
            ask_audit.write_audit(
                user_id=int(user["id"]),
                unit_id=int(user["unit_id"]),
                role_id=int(user["role_id"]),
                session_id=ctx.session_id,
                question=q,
                route="validate_run",
                sql_text=guarded_sql,
                success=False,
                denied=True,
                error_message=str(v.get("reason") or "llm_validate_fail")[:512],
                latency_ms=latency,
            )
            tr.update(
                {
                    "denied": True,
                    "fail_stage": "llm_validate",
                    "error": v.get("reason"),
                    "sql_preview": rag_trace.truncate_text(guarded_sql, max_len=500),
                }
            )
            _remember("askdata.validate_and_run", tr)
            return _json(
                {
                    "ok": False,
                    "denied": True,
                    "stage": "llm_validate",
                    "error": v.get("reason"),
                    "message": (
                        "查询未通过安全复核（权限/白名单/只读规则）。"
                        "请只陈述结果与限制事实，禁止给出破解或绕过建议。"
                    ),
                    "rules": rules,
                    "sql": guarded_sql,
                }
            )

        rows = _jsonable_rows(
            ask_exec.run_select(guarded_sql, params, max_rows=max_rows)
        )
        latency = int((time.perf_counter() - t0) * 1000)
        ask_audit.write_audit(
            user_id=int(user["id"]),
            unit_id=int(user["unit_id"]),
            role_id=int(user["role_id"]),
            session_id=ctx.session_id,
            question=q,
            route="validate_run",
            sql_text=guarded_sql,
            success=True,
            denied=False,
            row_count=len(rows),
            latency_ms=latency,
        )
        tr.update(
            {
                "row_count": len(rows),
                "sql_preview": rag_trace.truncate_text(guarded_sql, max_len=500),
                "denied": False,
                "next": "answer",
            }
        )
        _remember("askdata.validate_and_run", tr)
        return _json(
            {
                "ok": True,
                "denied": False,
                "sql": guarded_sql,
                "row_count": len(rows),
                "rows": rows,
                "next": "answer",
                "answer_policy": (
                    "仅依据 rows 陈述事实与数字；可附带 SQL 供透明展示；"
                    "禁止对权限/白名单提出绕过或破解建议。"
                ),
            }
        )


def build_askdata_tools() -> list[StructuredTool]:
    """构建 askdata skill 工具列表。"""

    @tool("askdata_route")
    async def askdata_route(
        question: Annotated[str, "用户原问句（不要改写成另一题）"],
    ) -> str:
        """判断问数路由：拒绝写 / 口径文档 / 指标模板 SQL / 需 NL2SQL。指标命中时直接返回 draft。"""
        return await _wrap_async(
            "askdata_route", lambda: _run_sync(_route_impl, question)
        )

    @tool("askdata_draft_sql")
    async def askdata_draft_sql(
        question: Annotated[str, "用户原问句"],
    ) -> str:
        """NL2SQL 草稿：域路由 + catalog/黑话 → 结构化 sqls/fields/tables/order_by/limit。非查询则 refused。"""
        return await _wrap_async(
            "askdata_draft_sql", lambda: _run_sync(_draft_sql_impl, question)
        )

    @tool("askdata_probe")
    async def askdata_probe(
        sql: Annotated[str, "待探查的 SELECT（通常取 draft.sqls[0]）"],
        question: Annotated[str, "用户原问句，用于无数据时匹配黑话"] = "",
    ) -> str:
        """探查执行：注入 unit、过守卫后跑 SQL，返回完整行；无数据则给黑话澄清提示。"""
        return await _wrap_async(
            "askdata_probe",
            lambda: _run_sync(_probe_impl, sql, question=question),
        )

    @tool("askdata_permission_rules")
    async def askdata_permission_rules() -> str:
        """返回当前用户的 unit/role 列限制与表白名单规则包，供主模型拼完整 SQL。"""
        return await _wrap_async(
            "askdata_permission_rules",
            lambda: _run_sync(_permission_rules_impl),
        )

    @tool("askdata_validate_and_run")
    async def askdata_validate_and_run(
        sql: Annotated[str, "主模型按规则包改写后的完整 SELECT"],
        question: Annotated[str, "用户原问句（写审计）"] = "",
    ) -> str:
        """代码守卫 + 小模型校验通过后执行；失败只返回受限事实，禁止绕过指引。"""
        return await _wrap_async(
            "askdata_validate_and_run",
            lambda: _run_sync(_validate_and_run_impl, sql, question=question),
        )

    return [
        askdata_route,
        askdata_draft_sql,
        askdata_probe,
        askdata_permission_rules,
        askdata_validate_and_run,
    ]
