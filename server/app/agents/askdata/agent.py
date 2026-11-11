"""
AskData Agent 对外 API：会话保证 + SSE 事件流。

2026-11 起
---------
流式问答改为走 **Reasoner 图 + askdata skill**（多工具编排），
与「智能问答」Tab 隔离的仍是本模块的 ``agent_type=askdata`` 会话与 ``/api/askdata``。

同步评测入口 ``chain.answer_question`` 仍保留（不经 LangGraph），便于单测/脚本。
后续稳定后可删除 chain 编排，只留 guard/perm/exec 等库。
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Optional

from app.infra import chat_repo

logger = logging.getLogger(__name__)

AGENT_TYPE = "askdata"
WELCOME = (
    "当前为智能问数：可问今日/本月销售额、门店汇总、退款等；"
    "复杂问题会做多表查询。仅支持只读，不能改数。"
)


def ensure_session(
    *,
    user_id: int,
    unit_id: int,
    session_id: Optional[str],
    title: str = "问数会话",
) -> str:
    """空则新建；非空则校验归属与 agent_type。"""
    if not session_id:
        sid = chat_repo.create_session(
            user_id=user_id,
            unit_id=unit_id,
            knowledge_base_id=None,
            title=(title or "问数会话")[:40],
            agent_type=AGENT_TYPE,
        )
        chat_repo.insert_message(session_id=sid, role="assistant", content=WELCOME)
        return sid

    row = chat_repo.get_session(session_id)
    if not row:
        raise ValueError("会话不存在")
    if int(row["user_id"]) != int(user_id):
        raise PermissionError("无权访问该会话")
    if (row.get("agent_type") or "") != AGENT_TYPE:
        raise ValueError("非智能问数会话")
    return session_id


async def astream_askdata(
    *,
    user: dict[str, Any],
    query: str,
    session_id: Optional[str] = None,
) -> AsyncIterator[dict[str, Any]]:
    """
    产出 dict: {event, data}，由路由再打包为 SSE。

    实现：确保 askdata 会话后，委托 ``astream_reasoner``，
    ``force_skills=["askdata"]``，与智能问答 Tab 的会话/技能隔离。
    """
    from app.agents.reasoner.agent import astream_reasoner
    from app.infra.db import run_in_thread

    try:
        sid = await run_in_thread(
            lambda: ensure_session(
                user_id=int(user["id"]),
                unit_id=int(user["unit_id"]),
                session_id=session_id,
            )
        )
    except Exception as exc:
        from app.common.user_errors import log_and_public

        public_msg = log_and_public(logger, exc, where="askdata_ensure_session")
        yield {"event": "error", "data": {"message": public_msg}}
        return

    async for ev in astream_reasoner(
        user_id=int(user["id"]),
        unit_id=int(user["unit_id"]),
        role_id=int(user.get("role_id") or 0),
        query=query,
        session_id=sid,
        agent_type=AGENT_TYPE,
        force_skills=["askdata"],
        skip_ensure=True,
        mode="react",
    ):
        # 兼容旧 Tab：done 上补 agent_type；route/denied 由工具日志间接体现，
        # 主模型回答文本里通常已含结论。
        if ev.get("event") == "done":
            data = dict(ev.get("data") or {})
            data.setdefault("agent_type", AGENT_TYPE)
            yield {"event": "done", "data": data}
        else:
            yield ev
