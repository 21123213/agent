"""
NL2SQL / 指标草稿的结构化约定。

五类数据（步骤 2 工具返回，后续 finalize / 校验都认这个形状）
----------------------------------------------------------
1. sqls      — 草稿 SQL 列表（多表相加场景可多条）
2. fields    — 查询字段，统一 ``表.字段``
3. tables    — 涉及的全部表
4. order_by  — 排序意图，如 ``biz_sales_order.order_time DESC``；无则 []
5. limit     — 用户要的条数 / TopN；无则 null（执行时用系统 max_rows）

非查询请求在 draft 阶段应 ``refused=true``，不要生成 SQL。
"""
from __future__ import annotations

import json
import re
from typing import Any, Optional


def empty_draft(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "refused": False,
        "refuse_reason": "",
        "sqls": [],
        "fields": [],
        "tables": [],
        "order_by": [],
        "limit": None,
        "notes": "",
    }
    base.update(overrides)
    return base


def parse_draft_json(text: str) -> dict[str, Any]:
    """
    从模型输出解析草稿 JSON（容忍 ```json 围栏与前后废话）。
    解析失败返回 refused 草稿并带原因。
    """
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if fence:
        t = fence.group(1).strip()
    # 尝试截取第一个 { ... } 块
    brace = re.search(r"\{[\s\S]*\}", t)
    if brace:
        t = brace.group(0)
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        return empty_draft(
            refused=True,
            refuse_reason=f"草稿 JSON 解析失败: {exc}",
            notes=text[:500] if text else "",
        )
    if not isinstance(data, dict):
        return empty_draft(refused=True, refuse_reason="草稿不是 JSON 对象")

    def _list_str(key: str) -> list[str]:
        v = data.get(key) or []
        if isinstance(v, str):
            return [v] if v.strip() else []
        return [str(x).strip() for x in v if str(x).strip()]

    limit = data.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = None

    return empty_draft(
        refused=bool(data.get("refused")),
        refuse_reason=str(data.get("refuse_reason") or ""),
        sqls=_list_str("sqls"),
        fields=_list_str("fields"),
        tables=_list_str("tables"),
        order_by=_list_str("order_by"),
        limit=limit,
        notes=str(data.get("notes") or ""),
    )


def draft_from_metric_sql(
    sql: str,
    *,
    tables: Optional[list[str]] = None,
    metric_name: str = "",
) -> dict[str, Any]:
    """指标模板 SQL → 统一草稿形状（可跳过 NL draft LLM）。"""
    order_by: list[str] = []
    m = re.search(r"(?is)\bORDER\s+BY\s+(.+?)(?:\s+LIMIT\b|$)", sql or "")
    if m:
        order_by = [m.group(1).strip()]
    lim = None
    m2 = re.search(r"(?i)\bLIMIT\s+(\d+)", sql or "")
    if m2:
        lim = int(m2.group(1))
    return empty_draft(
        sqls=[sql.strip()] if sql and sql.strip() else [],
        tables=list(tables or []),
        fields=[],
        order_by=order_by,
        limit=lim,
        notes=f"metric:{metric_name}" if metric_name else "metric",
    )


def primary_sql(draft: dict[str, Any]) -> str:
    """取草稿第一条 SQL；多条时由调用方决定是否逐条 probe。"""
    sqls = draft.get("sqls") or []
    return str(sqls[0]).strip() if sqls else ""
