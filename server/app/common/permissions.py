"""
知识库 / 文件权限（应用层）。

约定（本阶段）
-------------
- role_id == admin_role_id（默认 1）：本 unit 下全部 KB / 文件可读可管；
  **仅 admin** 可创建 / 删除知识库（级联清 MySQL + 向量）。
- 其它角色：同 unit 下可见全部 KB；可在可见 KB 内上传 / 删除文件；不可增删 KB。
- 向量侧过滤始终带 unit_id；可访问的 knowledge_base_id 列表由本模块计算。
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException, status

from app.settings import get_settings


def is_admin(user: dict[str, Any]) -> bool:
    s = get_settings()
    return int(user.get("role_id") or 0) == int(s.admin_role_id)


def require_admin(user: dict[str, Any]) -> None:
    if not is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行此操作",
        )


def same_unit(user: dict[str, Any], unit_id: int) -> bool:
    return int(user.get("unit_id") or 0) == int(unit_id)


def can_access_kb(user: dict[str, Any], kb: dict[str, Any]) -> bool:
    """同 unit 即可看 / 上传删文件；admin 与 member 此阶段规则相同。"""
    return same_unit(user, int(kb["unit_id"]))


def can_manage_kb(user: dict[str, Any], kb: dict[str, Any] | None = None) -> bool:
    """创建 / 删除知识库。"""
    if not is_admin(user):
        return False
    if kb is not None and not same_unit(user, int(kb["unit_id"])):
        return False
    return True


def require_kb_access(user: dict[str, Any], kb: dict[str, Any]) -> None:
    if not can_access_kb(user, kb):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该知识库",
        )


def require_file_access(user: dict[str, Any], file_row: dict[str, Any]) -> None:
    if not same_unit(user, int(file_row["unit_id"])):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="无权访问该文件",
        )
