"""
自由 NL2SQL：全量 Schema Catalog 注入后调用 LLM 生成 SELECT。

提示词强调：
- 只输出一条 SQL
- 有效订单状态
- 非 admin 必须带 unit_id 条件（占位 %(unit_id)s）
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.askdata.permissions import askdata_cross_unit
from app.agents.askdata.schema_catalog import (
    all_business_tables,
    format_catalog_for_prompt,
)
from app.common.llm_client import invoke_chat

_PROMPT_FILE = Path(__file__).resolve().parents[3] / "prompts" / "askdata_nl2sql.txt"


def _load_system_prompt() -> str:
    if _PROMPT_FILE.is_file():
        return _PROMPT_FILE.read_text(encoding="utf-8")
    return (
        "你是 MySQL NL2SQL 助手。只输出一条 SELECT。金额单位元。"
        "有效订单 status in ('paid','shipped','completed')。"
    )


def _extract_sql(text: str) -> str:
    """从模型输出中抽出 SQL（容忍 ```sql 围栏）。"""
    t = (text or "").strip()
    fence = re.search(r"```(?:sql)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if fence:
        return fence.group(1).strip()
    # 找第一个 SELECT/WITH
    m = re.search(r"(?is)\b(with|select)\b[\s\S]*", t)
    return (m.group(0) if m else t).strip()


def generate_sql(question: str, user: dict[str, Any]) -> tuple[str, list[str]]:
    """
    返回 (sql_text, selected_tables)。

    失败抛 RuntimeError，由 chain 转成用户可读错误。
    """
    tables = all_business_tables()
    catalog = format_catalog_for_prompt(tables)
    unit_rule = (
        "当前用户可跨全部 unit 查询，不要伪造 unit_id 限制。"
        if askdata_cross_unit(user)
        else (
            f"当前用户 unit_id={int(user['unit_id'])}。"
            "所有带 unit_id 的事实表/视图必须加条件 unit_id = %(unit_id)s "
            "（参数名必须是 unit_id）。"
        )
    )
    system = _load_system_prompt()
    human = (
        f"{unit_rule}\n\n"
        f"可用 Schema:\n{catalog}\n\n"
        f"用户问题：{question}\n\n"
        "请只输出一条 MySQL SELECT（可 WITH），不要解释。"
    )
    raw = invoke_chat(
        [SystemMessage(content=system), HumanMessage(content=human)],
        purpose="strong",
        temperature=0.0,
        reason="askdata_nl2sql",
    )
    return _extract_sql(raw), tables
