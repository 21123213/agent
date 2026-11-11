"""AskData HTTP 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class MessageResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    data: Optional[dict[str, Any]] = None


class AskdataSessionCreate(BaseModel):
    title: Optional[str] = Field(default="问数会话", max_length=80)


class AskdataSessionOut(BaseModel):
    session_id: str
    title: Optional[str] = None
    status: Optional[str] = None
    agent_type: Optional[str] = "askdata"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AskdataMessageOut(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    created_at: Optional[datetime] = None


class AskdataHistoryOut(BaseModel):
    session_id: str
    messages: list[AskdataMessageOut]


class AskdataChatStreamRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = None
