"""
SQL 守卫（应用层只读控制）。

为什么不用单独 MySQL 只读账号？
--------------------------------
本阶段产品约定：共用业务库连接，在执行前用本模块做静态校验即可演示
「防删改 / 防多语句 / 表白名单」。上线若要纵深防御，可再叠加只读账号。

校验要点
--------
1. 仅允许一条语句；去掉尾部分号后仍不能出现第二个分号。
2. 必须以 SELECT 或 WITH（CTE）开头。
3. 禁止写操作关键字、危险子句（INTO OUTFILE、FOR UPDATE、LOCK 等）。
4. 引用的表/视图名必须在白名单（biz_*）。
5. 若无 LIMIT，自动追加 LIMIT max_rows（子查询场景尽力追加在最外层）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

# 允许出现的表/视图（小写比较）
DEFAULT_ALLOWED_TABLES: frozenset[str] = frozenset(
    {
        "biz_channel",
        "biz_store",
        "biz_category",
        "biz_product",
        "biz_customer",
        "biz_sales_order",
        "biz_sales_order_item",
        "biz_payment",
        "biz_refund",
        "biz_daily_sales_agg",  # VIEW
        # 维度字典偶尔需要
        "s_unit",
    }
)

# 写/危险模式（忽略大小写）
_FORBIDDEN = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|REPLACE|GRANT|REVOKE|"
    r"CALL|EXEC|EXECUTE|LOAD\s+DATA|INTO\s+OUTFILE|INTO\s+DUMPFILE|"
    r"FOR\s+UPDATE|LOCK\s+TABLES|UNLOCK\s+TABLES|SET\s+|"
    r"SLEEP\s*\(|BENCHMARK\s*\("
    r")\b",
    re.IGNORECASE,
)

# 粗提取 FROM / JOIN 后的表名（不含 schema 前缀时）
_TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?",
    re.IGNORECASE,
)

# WITH cte AS / WITH cte AS ( … ), cte2 AS
_CTE_NAME = re.compile(
    r"\bWITH\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?\s+AS\b"
    r"|\),\s*`?([a-zA-Z_][a-zA-Z0-9_]*)`?\s+AS\b",
    re.IGNORECASE,
)

_LIMIT_TAIL = re.compile(r"\bLIMIT\s+\d+(\s*,\s*\d+)?\s*;?\s*$", re.IGNORECASE)


@dataclass
class GuardResult:
    """守卫结果：ok 时 sql 为可执行文本。"""

    ok: bool
    sql: str
    reason: str = ""


def _strip_comments(sql: str) -> str:
    """去掉 -- 行注释与 /* */ 块注释，降低绕过概率。"""
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    lines = []
    for line in no_block.splitlines():
        if "--" in line:
            line = line[: line.index("--")]
        lines.append(line)
    return "\n".join(lines)


def _normalize(sql: str) -> str:
    s = _strip_comments(sql or "").strip()
    # 去掉末尾多余分号（保留逻辑上「单句」）
    while s.endswith(";"):
        s = s[:-1].rstrip()
    return s


def ensure_single_statement(sql: str) -> GuardResult:
    """禁止多语句（分号分隔）。"""
    s = _normalize(sql)
    if not s:
        return GuardResult(False, "", "SQL 为空")
    if ";" in s:
        return GuardResult(False, "", "禁止多语句（检测到额外分号）")
    return GuardResult(True, s)


def ensure_select_only(sql: str) -> GuardResult:
    """仅 SELECT / WITH。"""
    s = _normalize(sql)
    head = s.lstrip().lower()
    if not (head.startswith("select") or head.startswith("with")):
        return GuardResult(False, "", "仅允许 SELECT / WITH 查询")
    if _FORBIDDEN.search(s):
        return GuardResult(False, "", "检测到禁止的写操作或危险关键字")
    return GuardResult(True, s)


def extract_table_names(sql: str) -> set[str]:
    """启发式提取表名（演示足够；极端别名场景靠白名单拒绝未知表）。"""
    return {m.group(1).lower() for m in _TABLE_REF.finditer(sql)}


def extract_cte_names(sql: str) -> set[str]:
    """提取 CTE 名，避免把 `FROM cte` 误判为非法物理表。"""
    names: set[str] = set()
    for m in _CTE_NAME.finditer(sql):
        n = m.group(1) or m.group(2)
        if n:
            names.add(n.lower())
    return names


def ensure_allowed_tables(
    sql: str,
    allowed: Iterable[str] | None = None,
) -> GuardResult:
    allowed_set = frozenset(t.lower() for t in (allowed or DEFAULT_ALLOWED_TABLES))
    cte = extract_cte_names(sql)
    found = extract_table_names(sql) - cte
    bad = sorted(t for t in found if t not in allowed_set)
    if bad:
        return GuardResult(False, "", f"表不在白名单: {', '.join(bad)}")
    return GuardResult(True, _normalize(sql))


def ensure_limit(sql: str, max_rows: int = 200) -> str:
    """最外层无 LIMIT 则追加，防止一次拖垮连接。"""
    s = _normalize(sql)
    if _LIMIT_TAIL.search(s):
        return s
    return f"{s}\nLIMIT {int(max_rows)}"


def guard_sql(
    sql: str,
    *,
    allowed_tables: Iterable[str] | None = None,
    max_rows: int = 200,
) -> GuardResult:
    """
    完整守卫流水线。

    返回的 sql 已规范化并带 LIMIT；失败时 ok=False 且 reason 可读。
    """
    r = ensure_single_statement(sql)
    if not r.ok:
        return r
    r = ensure_select_only(r.sql)
    if not r.ok:
        return r
    r = ensure_allowed_tables(r.sql, allowed_tables)
    if not r.ok:
        return r
    limited = ensure_limit(r.sql, max_rows=max_rows)
    return GuardResult(True, limited, "ok")
