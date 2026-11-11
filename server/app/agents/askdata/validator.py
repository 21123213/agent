"""
小模型二次校验：完整规则包 + 完整 SQL → pass/fail。

定位
----
- **不是**权限执行器：代码侧 ``sql_guard`` + ``permissions`` 仍是硬闸门。
- **是**语义补网：例如漏了 unit、选了禁用列、混入写操作意图等。

对用户话术：只陈述「未通过安全校验」类事实，**禁止**教如何绕过。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import invoke_chat

logger = logging.getLogger(__name__)

_SYSTEM = """你是企业问数 SQL 安全校验员（只读场景）。
输入是「权限/白名单规则」和「待执行 SQL」。
你只判断是否合规，输出严格 JSON：
{"pass": true/false, "reason": "简短中文原因"}

判定要点：
1. 必须是单条 SELECT 或 WITH...SELECT，不能有写操作。
2. 若 rules.unit_required=true，则带 unit_id 的事实表/条件应约束到 rules.unit_id（或等价参数）。
3. SELECT 列表不能出现 rules.denied_columns 中的列名。
4. FROM/JOIN 表名应落在 rules.allowed_tables 内（CTE 名除外）。
5. LIMIT 不应明显超过 rules.max_rows（若完全无 LIMIT，可 pass，由系统补）。

禁止在 reason 里提供「如何改 SQL 以绕过权限」的步骤；只说明违反了哪条规则。"""


def build_rules_package(
    *,
    user: dict[str, Any],
    role_code: str,
    denied_columns: list[str],
    allowed_tables: list[str],
    max_rows: int,
    is_admin: bool,
) -> dict[str, Any]:
    """给 LLM finalize 与小模型校验共用的规则包。"""
    from app.agents.askdata.permissions import askdata_cross_unit

    unit_id = int(user.get("unit_id") or 0)
    cross_unit = bool(is_admin) or askdata_cross_unit(user)
    return {
        "role_code": role_code,
        "role_id": int(user.get("role_id") or 0),
        "unit_id": unit_id,
        "is_admin": is_admin,
        "cross_unit": cross_unit,
        "unit_required": (not cross_unit),
        "denied_columns": list(denied_columns),
        "allowed_tables": list(allowed_tables),
        "max_rows": int(max_rows),
        "read_only": True,
        "hint_for_llm": (
            "请在草稿 SQL 上补全权限条件后输出完整可执行 SELECT；"
            "保留原有 ORDER BY / LIMIT 意图；不要删除业务筛选。"
            if not cross_unit
            else (
                "当前角色可跨全部 unit 查询；仍禁止写操作与白名单外的表；"
                "若 rules.denied_columns 非空，SELECT 中不得出现这些列。"
            )
        ),
    }


def validate_sql_with_small_model(
    sql: str,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """
    调用 validate purpose 小模型校验（默认 qwen3.6-flash，开思考）。

    返回 ``{"pass": bool, "reason": str}``。
    模型失败时 **保守拒绝**（pass=False），避免放行未校验 SQL。
    """
    human = (
        "rules:\n"
        + json.dumps(rules, ensure_ascii=False, indent=2)
        + "\n\nsql:\n"
        + (sql or "")
        + "\n\n只输出 JSON。"
    )
    try:
        # DashScope 校验模型（默认 qwen3.6-flash，开思考）；见 settings.dashscope_validate_model
        raw = invoke_chat(
            [SystemMessage(content=_SYSTEM), HumanMessage(content=human)],
            purpose="validate",
            temperature=0.0,
            reason="askdata_sql_validate",
        )
    except Exception as exc:
        logger.exception("askdata small-model validate failed")
        return {"pass": False, "reason": f"校验模型调用失败: {exc}"}

    t = (raw or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if fence:
        t = fence.group(1).strip()
    brace = re.search(r"\{[\s\S]*\}", t)
    if brace:
        t = brace.group(0)
    try:
        data = json.loads(t)
        return {
            "pass": bool(data.get("pass")),
            "reason": str(data.get("reason") or ""),
        }
    except json.JSONDecodeError:
        # 兜底：文本里出现明确 pass
        low = t.lower()
        if '"pass": true' in low or '"pass":true' in low:
            return {"pass": True, "reason": t[:200]}
        return {"pass": False, "reason": f"校验输出无法解析: {t[:200]}"}
