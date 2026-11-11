"""用户管理（admin）HTTP 模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class AdminCapabilities(BaseModel):
    is_admin: bool


class RoleOut(BaseModel):
    id: int
    code: str
    name: str


class UnitOut(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool = True


class AdminUserOut(BaseModel):
    id: int
    username: str
    phone: Optional[str] = None
    unit_id: int
    role_id: int
    is_active: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class AdminUserPatch(BaseModel):
    """至少提供一个字段。"""

    role_id: Optional[int] = None
    unit_id: Optional[int] = None
    is_active: Optional[bool] = None


class MessageResponse(BaseModel):
    success: bool = True
    message: str = "ok"
