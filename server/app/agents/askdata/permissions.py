"""
AskData 行 / 列权限。

规则（docs/askdata-plan.md）
--------------------------
- admin / viewer：问数 **跨 unit**，不注入 unit 过滤。
- manager / analyst：对带 unit_id 的事实表/视图注入 `AND <alias.>unit_id = %(unit_id)s`。
- 敏感列：viewer / analyst 禁止出现在 SELECT 列表；manager 允许但展示层可脱敏。

注意：行权限注入是**保守字符串改写**，主要保障模板 SQL 与简单 NL2SQL；
复杂 SQL 若绕过，仍有表白名单 + 只读守卫托底。演示评测以模板与规范 SQL 为准。
"""
from __future__ import annotations

import re
from typing import Any

from app.common.permissions import is_admin

# 角色 code → 禁止出现在 SELECT 中的列名（小写）
_ROLE_DENIED_COLUMNS: dict[str, frozenset[str]] = {
    "viewer": frozenset({"phone"}),
    "analyst": frozenset({"phone"}),
    "manager": frozenset(),  # 可查，回答阶段建议脱敏
    "admin": frozenset(),
}

# 问数行权限：可看全部 unit 的角色（仍受列黑名单约束）
_CROSS_UNIT_ROLES = frozenset({"admin", "viewer"})

# 指标模板里占位符
UNIT_FILTER = "{unit_filter}"
UNIT_FILTER_A = "{unit_filter_a}"  # 别名 a.unit_id


def role_code(user: dict[str, Any], role_id_to_code: dict[int, str] | None = None) -> str:
    """
    解析角色 code。

    优先用调用方传入的字典；否则按约定 id：1 admin / 2 manager / 3 analyst / 4 viewer。
    """
    rid = int(user.get("role_id") or 0)
    if role_id_to_code and rid in role_id_to_code:
        return role_id_to_code[rid]
    return {1: "admin", 2: "manager", 3: "analyst", 4: "viewer"}.get(rid, "viewer")


def denied_columns_for_user(user: dict[str, Any]) -> frozenset[str]:
    return _ROLE_DENIED_COLUMNS.get(role_code(user), frozenset({"phone"}))


def askdata_cross_unit(user: dict[str, Any]) -> bool:
    """问数是否可跨全部 unit（admin / viewer）。"""
    if is_admin(user):
        return True
    return role_code(user) in _CROSS_UNIT_ROLES


def apply_metric_unit_placeholders(sql_template: str, user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    替换指标 YAML 中的 {unit_filter} / {unit_filter_a}。

    返回 (sql, params)。跨 unit 角色替换为空串且 params 可不带 unit_id。
    """
    params: dict[str, Any] = {}
    if askdata_cross_unit(user):
        sql = sql_template.replace(UNIT_FILTER, "").replace(UNIT_FILTER_A, "")
        return sql, params

    uid = int(user["unit_id"])
    params["unit_id"] = uid
    sql = sql_template.replace(UNIT_FILTER, "AND unit_id = %(unit_id)s")
    sql = sql.replace(UNIT_FILTER_A, "AND a.unit_id = %(unit_id)s")
    return sql, params


def sql_mentions_denied_columns(sql: str, user: dict[str, Any]) -> str | None:
    """若 SELECT 列表疑似包含禁用列，返回原因；否则 None。"""
    denied = denied_columns_for_user(user)
    if not denied:
        return None
    # 粗检：列名作为独立标识出现
    for col in denied:
        if re.search(rf"(?i)(?<![a-z0-9_`])`?{col}`?(?![a-z0-9_])", sql):
            # 允许在注释中出现；已 strip comment 的由 guard 处理，这里简单拒绝
            return f"当前角色无权查询敏感列: {col}"
    return None


def inject_unit_predicate_simple(sql: str, user: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """
    对「未带 unit 条件」的简单 SELECT 追加 WHERE/AND unit_id。

    跨 unit 角色（admin/viewer）原样返回。若 SQL 已含 unit_id 谓词则不重复追加。
    这是演示级辅助；指标路径应走 apply_metric_unit_placeholders。
    """
    params: dict[str, Any] = {}
    if askdata_cross_unit(user):
        return sql, params
    if re.search(r"(?i)\bunit_id\b", sql):
        # 已有 unit_id：要求调用方自己参数化；此处仅绑定用户 unit 供 %(unit_id)s
        params["unit_id"] = int(user["unit_id"])
        return sql, params

    uid = int(user["unit_id"])
    params["unit_id"] = uid
    # 在 LIMIT 前插入条件
    lim = re.search(r"(?i)\blimit\b", sql)
    predicate = " unit_id = %(unit_id)s "
    if re.search(r"(?i)\bwhere\b", sql):
        clause = " AND " + predicate
    else:
        clause = " WHERE " + predicate

    if lim:
        idx = lim.start()
        sql = sql[:idx] + clause + sql[idx:]
    else:
        sql = sql + clause
    return sql, params
