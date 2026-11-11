"""认证相关请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    password: str = Field(..., min_length=6, max_length=72)
    phone: Optional[str] = Field(None, max_length=20)

    @field_validator("username")
    @classmethod
    def strip_username(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        return v

    @field_validator("phone")
    @classmethod
    def normalize_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not v.isdigit() or len(v) != 11 or not v.startswith("1"):
            raise ValueError("手机号格式不正确")
        return v


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str = Field(..., min_length=1, max_length=72)


class ResetPasswordRequest(BaseModel):
    """忘记密码：用户名 + 手机号 + 新密码。"""

    username: str = Field(..., min_length=1, max_length=50)
    phone: str = Field(..., min_length=11, max_length=20)
    new_password: str = Field(..., min_length=6, max_length=72)

    @field_validator("phone")
    @classmethod
    def check_phone(cls, v: str) -> str:
        v = v.strip()
        if not v.isdigit() or len(v) != 11 or not v.startswith("1"):
            raise ValueError("手机号格式不正确")
        return v


class UserInfo(BaseModel):
    id: int
    username: str
    phone: Optional[str] = None
    unit_id: Optional[int] = None
    role_id: Optional[int] = None
    is_active: bool = True
    created_at: Optional[datetime] = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_info: UserInfo


class MessageResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict[str, Any]] = None
