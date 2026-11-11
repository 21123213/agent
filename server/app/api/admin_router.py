"""
用户管理（仅 admin）。

前缀 ``/api/admin``。用于调整 users.role_id / unit_id / is_active。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.schemas_admin import (
    AdminCapabilities,
    AdminUserOut,
    AdminUserPatch,
    MessageResponse,
    RoleOut,
    UnitOut,
)
from app.common.permissions import is_admin, require_admin
from app.common.security import get_current_user, mask_phone
from app.infra import users as user_repo
from app.infra.db import run_in_thread
from app.settings import get_settings

router = APIRouter(prefix="/api/admin", tags=["用户管理"])


@router.get("/capabilities", response_model=AdminCapabilities)
async def capabilities(current_user: dict = Depends(get_current_user)):
    return AdminCapabilities(is_admin=is_admin(current_user))


@router.get("/roles", response_model=list[RoleOut])
async def roles(current_user: dict = Depends(get_current_user)):
    require_admin(current_user)

    def _work():
        return [RoleOut(**r) for r in user_repo.list_roles()]

    return await run_in_thread(_work)


@router.get("/units", response_model=list[UnitOut])
async def units(current_user: dict = Depends(get_current_user)):
    require_admin(current_user)

    def _work():
        return [UnitOut(**r) for r in user_repo.list_units()]

    return await run_in_thread(_work)


@router.get("/users", response_model=list[AdminUserOut])
async def list_users(current_user: dict = Depends(get_current_user)):
    require_admin(current_user)

    def _work():
        rows = user_repo.list_users()
        return [
            AdminUserOut(
                id=int(r["id"]),
                username=r["username"],
                phone=mask_phone(r.get("phone")),
                unit_id=int(r["unit_id"]),
                role_id=int(r["role_id"]),
                is_active=bool(r.get("is_active", True)),
                created_at=r.get("created_at"),
                updated_at=r.get("updated_at"),
            )
            for r in rows
        ]

    return await run_in_thread(_work)


@router.patch("/users/{user_id}", response_model=MessageResponse)
async def patch_user(
    user_id: int,
    body: AdminUserPatch,
    current_user: dict = Depends(get_current_user),
):
    require_admin(current_user)
    if body.role_id is None and body.unit_id is None and body.is_active is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="没有可更新字段")

    s = get_settings()

    def _work():
        target = user_repo.get_by_id(user_id)
        if not target:
            raise HTTPException(404, "用户不存在")

        # 防止管理员把自己降权后锁死：禁止改自己的 role / 停用自己
        if int(user_id) == int(current_user["id"]):
            if body.role_id is not None and int(body.role_id) != int(s.admin_role_id):
                raise HTTPException(400, "不能修改自己的管理员角色")
            if body.is_active is False:
                raise HTTPException(400, "不能停用自己的账号")

        if body.role_id is not None:
            roles = {int(r["id"]) for r in user_repo.list_roles()}
            if int(body.role_id) not in roles:
                raise HTTPException(400, "角色不存在")
        if body.unit_id is not None:
            units = {int(u["id"]) for u in user_repo.list_units(active_only=False)}
            if int(body.unit_id) not in units:
                raise HTTPException(400, "租户不存在")

        ok = user_repo.update_user_admin_fields(
            user_id,
            role_id=body.role_id,
            unit_id=body.unit_id,
            is_active=body.is_active,
        )
        if not ok:
            raise HTTPException(404, "用户不存在")
        return MessageResponse(message="已更新")

    return await run_in_thread(_work)
