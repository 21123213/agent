"""
缺参策略：什么时候问人，什么时候自动重试。

规则（产品拍板）
--------------
1. 仅当缺口字段属于该工具的 ``user_required_fields`` 白名单，
   **且**当前 arguments 里为空 → 才进入 ``interrupt`` 问用户。
2. 其它 required 空缺 / 格式错误 → 视为 ``model_fixable``：
   不 interrupt，由 agent 根据 ToolMessage 错误自行改参再调（图循环）。
3. 传输层失败 → ``common.retry`` 已在工具内重试；耗尽则返回错误字符串观察。

本模块提供「从 AIMessage.tool_calls 扫描 user_required 缺口」的辅助函数，
供 ``gate_before_tools`` / ``ask_user`` 节点使用。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from langchain_core.messages import AIMessage

from app.agents.reasoner.tools.core import user_required_fields_for

logger = logging.getLogger(__name__)


def _is_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    if val == [] or val == {}:
        return True
    return False


def find_user_required_gaps(message: AIMessage) -> List[Dict[str, Any]]:
    """
    扫描即将执行的 tool_calls，找出「用户必要字段」缺失。

    返回 questions 列表，元素形如::

        {
          "id": "q1",
          "tool": "xxx",
          "field": "city",
          "prompt": "请补充：城市（用于 xxx）",
          "call_id": "..."
        }

    空列表 = 没有需要问人的缺口，可以进 ToolNode。
    """
    questions: List[Dict[str, Any]] = []
    tool_calls = getattr(message, "tool_calls", None) or []
    qn = 0
    for tc in tool_calls:
        name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {}) or {}
        call_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        required_user = user_required_fields_for(str(name or ""))
        if not required_user:
            continue
        for field in sorted(required_user):
            if field not in args or _is_empty(args.get(field)):
                qn += 1
                questions.append(
                    {
                        "id": f"q{qn}",
                        "tool": name,
                        "field": field,
                        "call_id": call_id or "",
                        "prompt": f"请补充必要信息：{field}（工具 {name} 需要）",
                    }
                )
    if questions:
        logger.info(
            "user_required_gaps count=%s fields=%s",
            len(questions),
            ",".join(q["field"] for q in questions),
        )
    return questions


def find_user_required_gaps_in_plan(plan: dict | None) -> List[Dict[str, Any]]:
    """
    扫描 PE 计划全部 steps 的 input，找出 user_required 缺口。

    在分层执行**之前**一次性问人，避免执行节点中途 interrupt 丢进度。
    """
    if not plan or not isinstance(plan, dict):
        return []
    steps = plan.get("steps") or []
    tool_calls = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        name = str(s.get("tool") or "").strip()
        if not name:
            continue
        args = s.get("input") if isinstance(s.get("input"), dict) else {}
        tool_calls.append(
            {
                "id": str(s.get("id") or f"s{i}"),
                "name": name,
                "args": args,
            }
        )
    if not tool_calls:
        return []
    return find_user_required_gaps(AIMessage(content="", tool_calls=tool_calls))
