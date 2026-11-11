"""认证：注册 / 登录 / 忘记密码（用户名+手机）/ me / logout。无短信。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.schemas_auth import (
    LoginRequest,
    MessageResponse,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserInfo,
)
from app.common.security import (
    create_access_token,
    get_current_user,
    hash_password,
    mask_phone,
    verify_password,
)
from app.infra import users as user_repo
from app.settings import get_settings

router = APIRouter(prefix="/api/auth", tags=["认证"])


def _to_user_info(row: dict) -> UserInfo:
    return UserInfo(
        id=row["id"],
        username=row["username"],
        phone=mask_phone(row.get("phone")),
        unit_id=row.get("unit_id"),
        role_id=row.get("role_id"),
        is_active=bool(row.get("is_active", True)),
        created_at=row.get("created_at"),
    )


@router.post("/register", response_model=MessageResponse)
async def register(body: RegisterRequest):
    if user_repo.username_exists(body.username):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="用户名已存在")

    if body.phone and user_repo.phone_exists(body.phone):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="手机号已被使用")

    s = get_settings()
    user_id = user_repo.create_user(
        username=body.username,
        password_hash=hash_password(body.password),
        phone=body.phone,
        unit_id=s.default_unit_id,
        role_id=s.default_role_id,
    )
    return MessageResponse(
        success=True,
        message="注册成功",
        data={"user_id": user_id, "username": body.username},
    )


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest):
    row = user_repo.get_by_username(body.username.strip())
    if not row or not verify_password(body.password, row["password_hash"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not row.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已禁用")

    token = create_access_token(
        {"user_id": row["id"], "username": row["username"]},
    )
    return TokenResponse(access_token=token, user_info=_to_user_info(row))


@router.post("/reset-password", response_model=MessageResponse)
async def reset_password(body: ResetPasswordRequest):
    """忘记密码：用户名 + 手机号必须同时匹配（方案 3，无短信）。"""
    ok = user_repo.update_password_by_username_phone(
        username=body.username.strip(),
        phone=body.phone,
        password_hash=hash_password(body.new_password),
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名与手机号不匹配，或该账号未绑定手机号",
        )
    return MessageResponse(success=True, message="密码已重置，请使用新密码登录")


@router.get("/me", response_model=UserInfo)
async def me(current_user: dict = Depends(get_current_user)):
    return _to_user_info(current_user)


@router.post("/logout", response_model=MessageResponse)
async def logout(_: dict = Depends(get_current_user)):
    return MessageResponse(success=True, message="登出成功")
