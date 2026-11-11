"""
AskData 工具执行上下文（类比 rag_tool_context）。

Why
---
LangGraph 的 ``@tool`` 拿不到完整 ``user``（含 role_id）。
由 ``tool_exec`` 在进入工具批之前 ``bind``，工具内 ``get``；批结束 ``reset``。

字段说明
--------
- ``user``：JWT/DB 用户字典，至少含 id / unit_id / role_id
- ``session_id``：写审计用
- ``max_rows``：探查与正式执行的行上限
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AskdataToolContext:
    """单轮问数工具共享的租户/角色上下文。"""

    user: dict[str, Any]
    session_id: Optional[str] = None
    max_rows: int = 200
    # 本轮工具轨迹（权限/白名单结果等），供日志与调试
    events: list[dict[str, Any]] = field(default_factory=list)


_ctx: contextvars.ContextVar[Optional[AskdataToolContext]] = contextvars.ContextVar(
    "askdata_tool_ctx", default=None
)


def bind(ctx: AskdataToolContext) -> contextvars.Token:
    return _ctx.set(ctx)


def reset(token: contextvars.Token) -> None:
    _ctx.reset(token)


def get() -> Optional[AskdataToolContext]:
    return _ctx.get()


def require() -> AskdataToolContext:
    """工具内调用：无上下文则抛错，避免静默用错租户。"""
    ctx = _ctx.get()
    if ctx is None or not ctx.user:
        raise RuntimeError(
            "AskData 工具上下文未绑定：请确认会话走 askdata skill，"
            "且 tool_exec 已 bind 用户信息。"
        )
    return ctx
