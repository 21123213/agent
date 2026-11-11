"""RAG 问答 / 会话 API 的 Pydantic 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


class RagChatStreamRequest(BaseModel):
    """POST /api/rag/chat/stream 请求体。"""

    query: str = Field(..., min_length=1, description="用户问题")
    session_id: Optional[str] = Field(
        None, description="可选；空则新建会话"
    )
    knowledge_base_id: int = Field(..., description="必填；须有 unit 权限")
    file_ids: Optional[List[int]] = Field(
        None, description="可选；收窄检索文件"
    )
    vector_weight: Optional[float] = Field(
        None, ge=0, description="向量权重；与 bm25 归一化和为 1"
    )
    bm25_weight: Optional[float] = Field(
        None, ge=0, description="BM25 权重"
    )
    answer_mode: str = Field(
        "deep",
        description="quick=快速回答(baseline+4万token)；deep=深度思考(full+8万token)",
    )


class RagSessionCreate(BaseModel):
    knowledge_base_id: int
    title: Optional[str] = None


class RagSessionOut(BaseModel):
    session_id: str
    knowledge_base_id: Optional[int] = None
    title: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    welcome_hint: Optional[str] = None


class RagSourceOut(BaseModel):
    content: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


class RagMessageOut(BaseModel):
    id: Optional[int] = None
    role: str
    content: str
    sources: Optional[List[Any]] = None
    created_at: Optional[datetime] = None


class RagHistoryOut(BaseModel):
    session_id: str
    messages: List[RagMessageOut]


class MessageResponse(BaseModel):
    success: bool = True
    message: str = "ok"
    data: Optional[dict[str, Any]] = None
