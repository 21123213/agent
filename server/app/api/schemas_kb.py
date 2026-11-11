from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: Optional[str] = Field(None, max_length=512)


class KnowledgeBaseOut(BaseModel):
    id: int
    unit_id: int
    owner_user_id: int
    name: str
    description: Optional[str] = None
    is_default: bool = False
    status: str = "active"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    file_count: Optional[int] = None


class MessageResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict[str, Any]] = None
