"""knowledge_bases 表访问。"""
from __future__ import annotations

from typing import Any, Optional

from app.infra.db import execute, fetch_all, fetch_one


def list_by_unit(unit_id: int) -> list[dict[str, Any]]:
    return fetch_all(
        "SELECT id, unit_id, owner_user_id, name, description, is_default, status, "
        "created_at, updated_at FROM knowledge_bases "
        "WHERE unit_id = %s AND status = 'active' ORDER BY is_default DESC, id ASC",
        (unit_id,),
    )


def ensure_default_kb(*, unit_id: int, owner_user_id: int) -> dict[str, Any]:
    """
    本 unit 若还没有任何 active KB，则自动建一个「默认知识库」。
    云库从 v1 迁过来时常出现 knowledge_bases 为空；列表接口会调本函数兜底。
    """
    rows = list_by_unit(unit_id)
    if rows:
        return rows[0]
    kb_id = create_kb(
        unit_id=unit_id,
        owner_user_id=owner_user_id,
        name="默认知识库",
        description="系统自动创建的默认库",
        is_default=True,
    )
    row = get_by_id(kb_id)
    if not row:
        raise RuntimeError("failed to create default knowledge base")
    return row


def get_by_id(kb_id: int) -> Optional[dict[str, Any]]:
    return fetch_one(
        "SELECT id, unit_id, owner_user_id, name, description, is_default, status, "
        "created_at, updated_at FROM knowledge_bases WHERE id = %s",
        (kb_id,),
    )


def create_kb(
    *,
    unit_id: int,
    owner_user_id: int,
    name: str,
    description: str | None = None,
    is_default: bool = False,
) -> int:
    return execute(
        "INSERT INTO knowledge_bases "
        "(unit_id, owner_user_id, name, description, is_default, status) "
        "VALUES (%s, %s, %s, %s, %s, 'active')",
        (unit_id, owner_user_id, name, description, 1 if is_default else 0),
    )


def soft_delete_kb(kb_id: int) -> None:
    """先归档；物理删文件由业务层级联处理后再硬删。"""
    execute(
        "UPDATE knowledge_bases SET status = 'archived' WHERE id = %s",
        (kb_id,),
    )


def hard_delete_kb(kb_id: int) -> None:
    execute("DELETE FROM knowledge_bases WHERE id = %s", (kb_id,))


def count_files(kb_id: int) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS c FROM file_metadata WHERE knowledge_base_id = %s",
        (kb_id,),
    )
    return int(row["c"]) if row else 0
