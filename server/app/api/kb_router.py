"""
知识库 API。

权限
----
- 列表 / 详情：同 unit 可见
- 创建 / 删除：仅 admin（role_id == admin_role_id）
  删除 = 清该库全部文件向量 + 删 MySQL 文件行（CASCADE）+ 删 KB 行
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.schemas_kb import KnowledgeBaseCreate, KnowledgeBaseOut, MessageResponse
from app.common.permissions import (
    can_manage_kb,
    is_admin,
    require_admin,
    require_kb_access,
)
from app.common.security import get_current_user
from app.infra import file_repo, kb_repo
from app.infra import redis_client as redis_mgr
from app.infra.db import run_in_thread
from app.infra.milvus_service import milvus_service

router = APIRouter(prefix="/api/kb", tags=["知识库"])


def _to_out(row: dict, file_count: int | None = None) -> KnowledgeBaseOut:
    return KnowledgeBaseOut(
        id=row["id"],
        unit_id=row["unit_id"],
        owner_user_id=row["owner_user_id"],
        name=row["name"],
        description=row.get("description"),
        is_default=bool(row.get("is_default")),
        status=row.get("status") or "active",
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        file_count=file_count,
    )


@router.get("", response_model=list[KnowledgeBaseOut])
async def list_knowledge_bases(current_user: dict = Depends(get_current_user)):
    unit_id = int(current_user["unit_id"])
    user_id = int(current_user["id"])

    def _work():
        # 空库兜底：自动建「默认知识库」，避免前端无 KB 可选
        kb_repo.ensure_default_kb(unit_id=unit_id, owner_user_id=user_id)
        rows = kb_repo.list_by_unit(unit_id)
        out = []
        for r in rows:
            cnt = kb_repo.count_files(int(r["id"]))
            out.append(_to_out(r, cnt))
        return out

    return await run_in_thread(_work)


@router.get("/capabilities")
async def kb_capabilities(current_user: dict = Depends(get_current_user)):
    """前端用来显隐「新建/删除知识库」按钮。须在 /{kb_id} 之前注册。"""
    return {
        "is_admin": is_admin(current_user),
        "can_create_kb": is_admin(current_user),
        "can_delete_kb": is_admin(current_user),
        "can_upload_files": True,
        "can_delete_files": True,
        "role_id": current_user.get("role_id"),
        "unit_id": current_user.get("unit_id"),
    }


@router.post("", response_model=KnowledgeBaseOut, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    body: KnowledgeBaseCreate,
    current_user: dict = Depends(get_current_user),
):
    require_admin(current_user)

    def _work():
        try:
            kb_id = kb_repo.create_kb(
                unit_id=int(current_user["unit_id"]),
                owner_user_id=int(current_user["id"]),
                name=body.name.strip(),
                description=(body.description or "").strip() or None,
            )
        except Exception as exc:
            if "Duplicate" in str(exc) or "uk_kb_owner_name" in str(exc):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="同名知识库已存在",
                ) from exc
            raise
        row = kb_repo.get_by_id(kb_id)
        if not row:
            raise HTTPException(status_code=500, detail="创建失败")
        return _to_out(row, 0)

    return await run_in_thread(_work)


@router.get("/{kb_id}", response_model=KnowledgeBaseOut)
async def get_knowledge_base(
    kb_id: int,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = kb_repo.get_by_id(kb_id)
        if not row or row.get("status") != "active":
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_kb_access(current_user, row)
        return _to_out(row, kb_repo.count_files(kb_id))

    return await run_in_thread(_work)


@router.delete("/{kb_id}", response_model=MessageResponse)
async def delete_knowledge_base(
    kb_id: int,
    current_user: dict = Depends(get_current_user),
):
    """
    全量删除知识库：Milvus（unit+kb）→ 逐文件清进度锁 → DELETE KB
    （file_metadata CASCADE 清 content/elements/chunks）。
    """
    require_admin(current_user)

    def _work():
        row = kb_repo.get_by_id(kb_id)
        if not row:
            raise HTTPException(status_code=404, detail="知识库不存在")
        if not can_manage_kb(current_user, row):
            raise HTTPException(status_code=403, detail="无权删除该知识库")

        unit_id = int(row["unit_id"])
        file_ids = file_repo.list_ids_by_kb(kb_id)

        # 1) 向量
        milvus_service.delete_by_kb(unit_id, kb_id)

        # 2) 进度键
        for fid in file_ids:
            try:
                redis_mgr.clear_task_progress(fid)
                redis_mgr.release_file_lock(fid)
            except Exception:
                pass

        # 3) 删 KB（CASCADE 文件）
        kb_repo.hard_delete_kb(kb_id)
        # 4) 语料世代 +1（该 KB 下旧 QA 缓存全部自然 miss）
        try:
            redis_mgr.bump_corpus_version(unit_id, kb_id)
        except Exception:
            pass
        return MessageResponse(
            success=True,
            message="知识库已删除",
            data={"kb_id": kb_id, "files_removed": len(file_ids)},
        )

    return await run_in_thread(_work)
