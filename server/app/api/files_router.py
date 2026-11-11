"""
文件 / 入库 API（到向量化为止，不含问答）。

主链路
------
POST /upload?knowledge_base_id=
  → 校验扩展名/大小
  → 写 file_metadata + file_content
  → Celery process_file(file_id)
  → 立即返回 batch 汇总

GET  /list?knowledge_base_id=     列表
GET  /{id}                        元数据
GET  /{id}/progress               Redis 进度（无则回落 DB）
GET  /{id}/status                 详情聚合（分区/切片/向量进度）— 可轮询实时看
GET  /{id}/chunks                 切片列表
DELETE /{id}                      删向量 + MySQL
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status

from app.api.schemas_files import (
    BatchDeleteRequest,
    FileListResponse,
    FileMetadataOut,
    MessageResponse,
    UploadBatchResponse,
    UploadItemResult,
)
from app.common.permissions import require_file_access, require_kb_access
from app.common.security import get_current_user
from app.infra import file_repo, kb_repo
from app.infra import redis_client as redis_mgr
from app.infra.db import run_in_thread
from app.infra.milvus_service import milvus_service
from app.settings import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["文件"])


def _ext(name: str) -> str:
    return Path(name).suffix.lower()


def _to_out(row: dict) -> FileMetadataOut:
    stats = row.get("elements_stats")
    if isinstance(stats, str):
        import json

        try:
            stats = json.loads(stats)
        except Exception:
            stats = None
    return FileMetadataOut(
        id=row["id"],
        user_id=row["user_id"],
        unit_id=row["unit_id"],
        knowledge_base_id=row["knowledge_base_id"],
        filename=row["filename"],
        original_filename=row["original_filename"],
        file_size=row["file_size"],
        file_type=row.get("file_type"),
        mime_type=row.get("mime_type"),
        status=row.get("status"),
        chunks_count=row.get("chunks_count") or 0,
        total_elements=row.get("total_elements") or 0,
        total_chunks=row.get("total_chunks") or 0,
        embedded_chunks=row.get("embedded_chunks") or 0,
        elements_stats=stats,
        error_message=row.get("error_message"),
        upload_time=row.get("upload_time"),
        processed_time=row.get("processed_time"),
    )


def _purge_file(file_row: dict) -> None:
    """删向量 → 清 Redis → 删 MySQL 元数据（CASCADE）→ bump 语料版本。"""
    fid = int(file_row["id"])
    unit_id = int(file_row["unit_id"])
    kb_id = int(file_row["knowledge_base_id"])
    try:
        milvus_service.delete_by_file(unit_id, fid)
    except Exception as exc:
        logger.warning("milvus purge file=%s: %s", fid, exc)
    try:
        redis_mgr.clear_task_progress(fid)
        redis_mgr.release_file_lock(fid)
    except Exception:
        pass
    file_repo.delete_file(fid)
    # 删除文件后旧 QA 缓存应失效
    try:
        redis_mgr.bump_corpus_version(unit_id, kb_id)
    except Exception as exc:
        logger.warning("bump_corpus_version after delete file=%s: %s", fid, exc)


def _enqueue_process(file_id: int) -> str:
    """Celery 优先；无 Redis 时进程内线程回退。返回 celery|thread。"""
    from app.agents.rag.enqueue import enqueue_process_file

    return enqueue_process_file(file_id)


async def _read_and_validate(upload: UploadFile) -> tuple[bytes | None, str]:
    """返回 (content, error_message)。content 为 None 表示拒绝。"""
    s = get_settings()
    name = upload.filename or "unknown"
    ext = _ext(name)
    if ext not in s.allowed_extensions:
        return None, f"不支持的格式: {ext}"
    data = await upload.read()
    if len(data) == 0:
        return None, "空文件"
    if len(data) > s.upload_max_file_size:
        return None, f"超过大小限制 ({s.upload_max_file_size} bytes)"
    return data, ""


@router.post("/upload", response_model=UploadBatchResponse)
async def upload_files(
    knowledge_base_id: int = Query(..., description="目标知识库"),
    files: list[UploadFile] = File(...),
    current_user: dict = Depends(get_current_user),
):
    """
    可多文件。非法文件 rejected 不入队；合法写库并 Celery 入队。
    batch_status: success | partial_success | all_failed
    """

    def _load_kb():
        kb = kb_repo.get_by_id(knowledge_base_id)
        if not kb or kb.get("status") != "active":
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_kb_access(current_user, kb)
        return kb

    await run_in_thread(_load_kb)

    items: list[UploadItemResult] = []
    queued = 0
    rejected = 0

    for upload in files:
        original = upload.filename or "unknown"
        content, err = await _read_and_validate(upload)
        if content is None:
            rejected += 1
            items.append(
                UploadItemResult(
                    original_filename=original,
                    status="rejected",
                    message=err,
                )
            )
            continue

        ext = _ext(original)
        sha = hashlib.sha256(content).hexdigest()
        stored_name = f"{uuid.uuid4().hex}{ext}"

        def _persist(
            _content=content,
            _original=original,
            _ext=ext,
            _sha=sha,
            _stored=stored_name,
            _mime=upload.content_type,
        ):
            fid = file_repo.create_metadata(
                user_id=int(current_user["id"]),
                unit_id=int(current_user["unit_id"]),
                knowledge_base_id=knowledge_base_id,
                filename=_stored,
                original_filename=_original,
                file_size=len(_content),
                file_type=_ext.lstrip("."),
                mime_type=_mime,
                content_sha256=_sha,
                status="pending",
            )
            file_repo.save_content(fid, _content)
            file_repo.update_file_path_after_insert(fid)
            return fid

        try:
            file_id = await run_in_thread(_persist)
        except Exception as exc:
            logger.exception("upload persist failed")
            rejected += 1
            items.append(
                UploadItemResult(
                    original_filename=original,
                    status="rejected",
                    message=str(exc),
                )
            )
            continue

        try:
            mode = await run_in_thread(_enqueue_process, file_id)
            queued += 1
            items.append(
                UploadItemResult(
                    original_filename=original,
                    file_id=file_id,
                    status="queued",
                    message=(
                        "已入队 Celery"
                        if mode == "celery"
                        else "已启动进程内处理（无 Redis/Celery）"
                    ),
                )
            )
        except Exception as exc:
            logger.exception("enqueue failed file_id=%s", file_id)
            await run_in_thread(
                file_repo.update_status,
                file_id,
                "failed",
                error_message=f"入队失败: {exc}",
            )
            rejected += 1
            items.append(
                UploadItemResult(
                    original_filename=original,
                    file_id=file_id,
                    status="rejected",
                    message=f"入队失败: {exc}",
                )
            )

    if queued and not rejected:
        batch = "success"
    elif queued and rejected:
        batch = "partial_success"
    else:
        batch = "all_failed"

    return UploadBatchResponse(batch_status=batch, items=items)


@router.get("/list", response_model=FileListResponse)
async def list_files(
    knowledge_base_id: int = Query(...),
    page: int = Query(1, ge=1),
    size: int = Query(100, ge=1, le=500),
    current_user: dict = Depends(get_current_user),
):
    def _work():
        kb = kb_repo.get_by_id(knowledge_base_id)
        if not kb or kb.get("status") != "active":
            raise HTTPException(status_code=404, detail="知识库不存在")
        require_kb_access(current_user, kb)
        unit_id = int(current_user["unit_id"])
        rows = file_repo.list_by_kb(
            knowledge_base_id, unit_id=unit_id, page=page, size=size
        )
        total = file_repo.count_by_kb(knowledge_base_id, unit_id=unit_id)
        return FileListResponse(
            items=[_to_out(r) for r in rows],
            total=total,
            page=page,
            size=size,
        )

    return await run_in_thread(_work)


@router.get("/{file_id}", response_model=FileMetadataOut)
async def get_file_detail(
    file_id: int,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = file_repo.get_by_id(file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        require_file_access(current_user, row)
        return _to_out(row)

    return await run_in_thread(_work)


@router.get("/{file_id}/progress")
async def get_progress(
    file_id: int,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = file_repo.get_by_id(file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        require_file_access(current_user, row)
        prog = None
        try:
            prog = redis_mgr.get_task_progress(file_id)
        except Exception:
            prog = None
        if prog:
            return prog
        st = row.get("status") or "pending"
        return {
            "status": st,
            "progress": 100 if st in ("processed", "completed") else 0,
            "current_step": st,
            "message": {
                "pending": "等待处理",
                "processing": "处理中...",
                "processed": "处理完成",
                "failed": "处理失败",
            }.get(st, st),
        }

    return await run_in_thread(_work)


@router.get("/{file_id}/status")
async def get_processing_status(
    file_id: int,
    current_user: dict = Depends(get_current_user),
):
    """
    文档详情聚合：进度 + partitioning / chunking / summarization。
    前端轮询本接口即可实时看到切片入库情况（Worker 写 MySQL 后立刻可见）。
    """

    def _work() -> dict[str, Any]:
        row = file_repo.get_by_id(file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        require_file_access(current_user, row)

        progress_data: dict[str, Any] = {}
        try:
            progress_data = redis_mgr.get_task_progress(file_id) or {}
        except Exception:
            progress_data = {}

        response: dict[str, Any] = {
            "file_id": file_id,
            "filename": row.get("original_filename"),
            "status": progress_data.get("status") or row.get("status"),
            "current_stage": progress_data.get("current_step"),
            "progress": progress_data.get("progress", 0),
            "message": progress_data.get("message"),
            "error_message": row.get("error_message"),
        }

        elements = file_repo.get_elements(file_id)
        if elements:
            element_types: dict[str, int] = {}
            for elem in elements:
                t = elem.get("element_type") or "unknown"
                element_types[t] = element_types.get(t, 0) + 1
            response["partitioning"] = {
                "total_elements": len(elements),
                "element_types": {
                    "text_sections": (
                        element_types.get("NarrativeText", 0)
                        + element_types.get("Text", 0)
                        + element_types.get("UncategorizedText", 0)
                    ),
                    "tables": element_types.get("Table", 0),
                    "images": element_types.get("Image", 0)
                    + element_types.get("FigureCaption", 0),
                    "titles": element_types.get("Title", 0),
                    "list_items": element_types.get("ListItem", 0)
                    + element_types.get("List", 0),
                },
            }

        chunks = file_repo.get_chunks(file_id)
        if chunks:
            total_chars = sum(int(c.get("char_count") or 0) for c in chunks)
            response["chunking"] = {
                "total_elements": len(elements) if elements else 0,
                "total_chunks": len(chunks),
                "avg_chunk_size": int(total_chars / len(chunks)) if chunks else 0,
            }

        processed = int(progress_data.get("processed_chunks") or 0)
        step = progress_data.get("current_step")
        if row.get("status") == "processed":
            total = len(chunks) if chunks else processed
            response["summarization"] = {
                "total_chunks": total,
                "processed_chunks": total,
                "progress_percentage": 100,
            }
        elif step in ("summarization", "embedding"):
            total = len(chunks) if chunks else max(processed, 1)
            response["summarization"] = {
                "total_chunks": total,
                "processed_chunks": processed,
                "progress_percentage": (processed / total * 100) if total else 0,
            }

        return response

    return await run_in_thread(_work)


@router.get("/{file_id}/chunks")
async def get_file_chunks(
    file_id: int,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = file_repo.get_by_id(file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        require_file_access(current_user, row)
        chunks = file_repo.get_chunks(file_id)
        return {
            "file_id": file_id,
            "total": len(chunks),
            "items": [
                {
                    "chunk_index": c["chunk_index"],
                    "chunk_text": c.get("chunk_text"),
                    "char_count": c.get("char_count"),
                    "element_count": c.get("element_count") or 0,
                    "element_ids": c.get("element_ids") or [],
                    "page_numbers": c.get("page_numbers") or [],
                    "content_types": c.get("content_types") or [],
                    "element_types": c.get("element_types") or [],
                    "is_embedded": bool(c.get("is_embedded")),
                    "vector_id": c.get("vector_id"),
                }
                for c in chunks
            ],
        }

    return await run_in_thread(_work)


@router.delete("/{file_id}", response_model=MessageResponse)
async def delete_file(
    file_id: int,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        row = file_repo.get_by_id(file_id)
        if not row:
            raise HTTPException(status_code=404, detail="文件不存在")
        require_file_access(current_user, row)
        # 确认所属 KB 仍可访问
        kb = kb_repo.get_by_id(int(row["knowledge_base_id"]))
        if kb:
            require_kb_access(current_user, kb)
        _purge_file(row)
        return MessageResponse(success=True, message="文件已删除", data={"file_id": file_id})

    return await run_in_thread(_work)


@router.post("/batch-delete", response_model=MessageResponse)
async def batch_delete(
    body: BatchDeleteRequest,
    current_user: dict = Depends(get_current_user),
):
    def _work():
        ok, fail = 0, 0
        for fid in body.file_ids:
            row = file_repo.get_by_id(fid)
            if not row:
                fail += 1
                continue
            try:
                require_file_access(current_user, row)
                _purge_file(row)
                ok += 1
            except Exception:
                fail += 1
        return MessageResponse(
            success=fail == 0,
            message=f"删除完成：成功 {ok}，失败 {fail}",
            data={"ok": ok, "fail": fail},
        )

    return await run_in_thread(_work)
