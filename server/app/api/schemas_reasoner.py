"""Reasoner（推理 Agent）API 的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


class ReasonerResumeBody(BaseModel):
    """续跑 interrupt：用户补充必要信息。"""

    resume_token: Optional[str] = Field(
        None, description="可与 session_id 相同；校验用"
    )
    answers: str = Field(..., min_length=1, description="用户补充文本")


class ReasonerChatStreamRequest(BaseModel):
    """POST /api/reasoner/chat/stream"""

    query: str = Field("", description="新问题；resume 时可作补充文本")
    session_id: Optional[str] = None
    mode: Literal["react", "plan_execute"] = "react"
    answer_mode: Literal["quick", "deep"] = Field(
        "deep",
        description="quick=4万token；deep=8万token（绑 KB 时 quick 走 baseline 检索）",
    )
    resume: Optional[ReasonerResumeBody] = None


class ReasonerSessionCreate(BaseModel):
    title: Optional[str] = None


class ReasonerSessionOut(BaseModel):
    session_id: str
    title: Optional[str] = None
    status: Optional[str] = None
    agent_type: str = "reasoner"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ReasonerMessageOut(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    created_at: Optional[datetime] = None


class ReasonerHistoryOut(BaseModel):
    session_id: str
    messages: List[ReasonerMessageOut]


class MessageResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    data: Optional[dict[str, Any]] = None
