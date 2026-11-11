"""用户表访问（参数化 SQL）。"""
from __future__ import annotations

from typing import Any, Optional

from app.infra.db import execute, fetch_all, fetch_one

# 全局角色种子（与 seed_askdata / askdata-plan 一致）
_ROLE_SEED: tuple[tuple[int, str, str], ...] = (
    (1, "admin", "管理员"),
    (2, "manager", "经理"),
    (3, "analyst", "分析师"),
    (4, "viewer", "访客"),
)


def ensure_default_unit_role(unit_id: int = 1, role_id: int = 4) -> None:
    """
    保证默认 unit 与标准角色行存在，满足外键。

    注册默认 role 已改为 viewer(4)；此处按 id 写入正确 code，避免再把 4 写成 admin。
    """
    execute(
        "INSERT IGNORE INTO s_unit (id, code, name, is_active) VALUES (%s, %s, %s, 1)",
        (unit_id, "default", "Default"),
    )
    for rid, code, name in _ROLE_SEED:
        execute(
            "INSERT IGNORE INTO s_role (id, code, name, unit_id) VALUES (%s, %s, %s, NULL)",
            (rid, code, name),
        )
    # 若只要某个 role_id 不在种子里，再兜底一行
    if role_id not in {r[0] for r in _ROLE_SEED}:
        execute(
            "INSERT IGNORE INTO s_role (id, code, name, unit_id) VALUES (%s, %s, %s, NULL)",
            (role_id, f"role_{role_id}", f"角色{role_id}"),
        )


def get_by_id(user_id: int) -> Optional[dict[str, Any]]:
    return fetch_one(
        "SELECT id, username, password_hash, phone, unit_id, role_id, is_active, created_at, updated_at "
        "FROM users WHERE id = %s",
        (user_id,),
    )


def get_by_username(username: str) -> Optional[dict[str, Any]]:
    return fetch_one(
        "SELECT id, username, password_hash, phone, unit_id, role_id, is_active, created_at, updated_at "
        "FROM users WHERE username = %s",
        (username,),
    )


def username_exists(username: str) -> bool:
    row = fetch_one("SELECT id FROM users WHERE username = %s LIMIT 1", (username,))
    return row is not None


def phone_exists(phone: str) -> bool:
    row = fetch_one(
        "SELECT id FROM users WHERE phone = %s LIMIT 1",
        (phone,),
    )
    return row is not None


def create_user(
    *,
    username: str,
    password_hash: str,
    phone: Optional[str],
    unit_id: int = 1,
    role_id: int = 4,
) -> int:
    ensure_default_unit_role(unit_id, role_id)
    return execute(
        "INSERT INTO users (username, password_hash, phone, unit_id, role_id, is_active) "
        "VALUES (%s, %s, %s, %s, %s, 1)",
        (username, password_hash, phone, unit_id, role_id),
    )


def list_users(limit: int = 200) -> list[dict[str, Any]]:
    """管理员：列出用户（不含 password_hash）。"""
    return fetch_all(
        "SELECT id, username, phone, unit_id, role_id, is_active, created_at, updated_at "
        "FROM users ORDER BY id ASC LIMIT %s",
        (int(limit),),
    )


def list_roles() -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT id, code, name FROM s_role ORDER BY id ASC",
    )


def list_units(active_only: bool = True) -> list[dict[str, Any]]:
    if active_only:
        return fetch_all(
            "SELECT id, code, name, is_active FROM s_unit WHERE is_active=1 ORDER BY id ASC"
        )
    return fetch_all(
        "SELECT id, code, name, is_active FROM s_unit ORDER BY id ASC"
    )


def update_user_admin_fields(
    user_id: int,
    *,
    role_id: Optional[int] = None,
    unit_id: Optional[int] = None,
    is_active: Optional[bool] = None,
) -> bool:
    """
    管理员改角色/租户/启停。返回是否更新到行。

    禁止把最后一个 admin 降权的校验放在路由层（可按需加强）。
    """
    row = get_by_id(user_id)
    if not row:
        return False
    sets: list[str] = []
    params: list[Any] = []
    if role_id is not None:
        sets.append("role_id=%s")
        params.append(int(role_id))
    if unit_id is not None:
        sets.append("unit_id=%s")
        params.append(int(unit_id))
    if is_active is not None:
        sets.append("is_active=%s")
        params.append(1 if is_active else 0)
    if not sets:
        return True
    params.append(int(user_id))
    execute(f"UPDATE users SET {', '.join(sets)} WHERE id=%s", tuple(params))
    return True


def update_password_by_username_phone(
    *,
    username: str,
    phone: str,
    password_hash: str,
) -> bool:
    """方案 3：用户名 + 手机号同时匹配才重置。返回是否更新到行。"""
    row = fetch_one(
        "SELECT id FROM users WHERE username = %s AND phone = %s LIMIT 1",
        (username, phone),
    )
    if not row:
        return False
    execute(
        "UPDATE users SET password_hash = %s WHERE id = %s",
        (password_hash, row["id"]),
    )
    return True
