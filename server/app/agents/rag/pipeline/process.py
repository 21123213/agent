"""
单文件处理主链路（供 Celery 调用）。

状态机
------
pending → processing → processed
                    ↘ failed

阶段与进度（Redis，详情页轮询可读）
----------------------------------
1. parsing      ~10–35   Unstructured → document_elements
2. chunking     ~40–50   chunk_by_title → document_chunks
3. summarization ~55–65  可选 LLM 增强
4. embedding    ~65–100  DashScope 向量 → Milvus + is_embedded

MySQL 是真相：elements/chunks 入库后，即使 Redis 丢了，详情接口仍能从库回填切片。
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from app.common import embeddings
from app.infra import file_repo
from app.infra import redis_client as redis_mgr
from app.infra.milvus_service import milvus_service
from app.settings import get_settings

from . import chunk as chunk_mod
from . import parse as parse_mod

logger = logging.getLogger(__name__)


def _progress(
    file_id: int,
    *,
    progress: int,
    step: str,
    message: str,
    processed_chunks: int | None = None,
    status: str = "processing",
) -> None:
    try:
        redis_mgr.set_task_progress(
            file_id,
            status=status,
            progress=progress,
            current_step=step,
            message=message,
            processed_chunks=processed_chunks,
        )
    except Exception as exc:
        logger.warning("redis progress failed: %s", exc)


def _fail(file_id: int, exc: Exception) -> None:
    file_repo.update_status(file_id, "failed", error_message=str(exc)[:2000])
    _progress(
        file_id,
        progress=0,
        step="error",
        message=f"处理失败: {exc}",
        status="failed",
    )
    try:
        redis_mgr.release_file_lock(file_id)
    except Exception:
        pass


def materialize_to_temp(file_id: int, original_filename: str) -> str:
    """从 LONGBLOB 写出临时文件，供 Unstructured 读路径。"""
    content = file_repo.get_content(file_id)
    if content is None:
        raise FileNotFoundError(f"file_content missing for file_id={file_id}")
    suffix = Path(original_filename).suffix or ".bin"
    fd, path = tempfile.mkstemp(prefix=f"agents_f{file_id}_", suffix=suffix)
    os.close(fd)
    with open(path, "wb") as f:
        f.write(content)
    return path


def process_file(file_id: int) -> dict[str, Any]:
    """
    完整流水线。幂等注意：重跑前清旧 elements/chunks，并删旧向量再插入。
    """
    meta = file_repo.get_by_id(file_id)
    if not meta:
        raise ValueError(f"file_id={file_id} not found")

    unit_id = int(meta["unit_id"])
    kb_id = int(meta["knowledge_base_id"])
    owner_user_id = int(meta["user_id"])
    original = meta.get("original_filename") or meta.get("filename") or "doc"
    temp_path: Optional[str] = None

    try:
        print(f"==== process_file start file_id={file_id} kb={kb_id} ====")
        file_repo.update_status(file_id, "processing")
        file_repo.update_field(file_id, "started_processing_time", "NOW()")
        _progress(file_id, progress=5, step="parsing", message="准备解析...")

        # ---- 物化附件 ----
        temp_path = materialize_to_temp(file_id, original)
        print(f"[process] temp file: {temp_path}")

        # ---- 1. Parsing ----
        _progress(file_id, progress=10, step="parsing", message="正在解析文档...", processed_chunks=0)
        raw_elements, element_dicts = parse_mod.parse_document(temp_path)
        file_repo.clear_elements(file_id)
        for elem in element_dicts:
            file_repo.insert_element(
                file_id=file_id,
                element_index=elem["index"],
                element_type=elem["type"],
                text_content=elem["text"],
                metadata=elem.get("metadata"),
            )
        elements_count = len(element_dicts)
        stats = parse_mod.count_element_types(element_dicts)
        file_repo.update_field(file_id, "total_elements", elements_count)
        file_repo.update_field(file_id, "elements_stats", stats)
        _progress(
            file_id,
            progress=35,
            step="parsing",
            message=f"解析完成，共{elements_count}个元素",
            processed_chunks=0,
        )

        # ---- 2. Chunking ----
        _progress(file_id, progress=40, step="chunking", message="正在生成切片...", processed_chunks=0)
        element_id_map = chunk_mod.build_element_id_map(element_dicts)
        title = chunk_mod.document_title(original)
        u_chunks = chunk_mod.chunk_elements_by_title(raw_elements)
        records = chunk_mod.build_basic_chunk_records(
            u_chunks, element_id_map=element_id_map, doc_title=title
        )
        file_repo.clear_chunks(file_id)
        for rec in records:
            file_repo.insert_chunk(
                file_id=file_id,
                chunk_index=rec["chunk_index"],
                chunk_text=rec["text"],
                char_count=rec["char_count"],
                element_count=rec["element_count"],
                element_ids=rec["element_ids"],
                page_numbers=rec["page_numbers"],
                content_types=rec.get("content_types"),
                element_types=rec.get("element_types"),
            )
        chunks_count = len(records)
        file_repo.update_field(file_id, "total_chunks", chunks_count)
        _progress(
            file_id,
            progress=50,
            step="chunking",
            message=f"切片完成，共{chunks_count}个",
            processed_chunks=0,
        )

        # ---- 3. Enhance (optional) ----
        _progress(
            file_id,
            progress=55,
            step="summarization",
            message="正在处理切片内容...",
            processed_chunks=0,
        )

        def _sum_cb(cur: int, total: int) -> None:
            pct = 55 + int((cur / total) * 10) if total else 55
            _progress(
                file_id,
                progress=pct,
                step="summarization",
                message=f"内容增强中... {cur}/{total}",
                processed_chunks=cur,
            )

        records = chunk_mod.enhance_chunk_records(records, progress_callback=_sum_cb)
        _progress(
            file_id,
            progress=65,
            step="summarization",
            message=f"内容增强完成，共{chunks_count}个",
            processed_chunks=chunks_count,
        )

        # ---- 4. Embedding + Milvus ----
        _progress(file_id, progress=65, step="embedding", message="正在向量化...", processed_chunks=0)
        # 先删旧向量，保证重试幂等
        milvus_service.delete_by_file(unit_id, file_id)

        s = get_settings()
        embedded: list[dict[str, Any]] = []
        failed: list[int] = []
        total = len(records)

        for i, rec in enumerate(records):
            cidx = int(rec["chunk_index"])
            try:
                text = rec.get("text_for_embedding") or rec["text"]
                vec = embeddings.embed_document(text)
                if len(vec) != s.milvus_embedding_dim:
                    failed.append(cidx)
                    continue
                embedded.append(
                    {
                        "chunk_index": cidx,
                        "embedding": vec,
                        "text": rec["text"],
                        "metadata": {
                            "file_id": file_id,
                            "doc_title": rec.get("doc_title") or "",
                            "char_count": rec["char_count"],
                            "page_numbers": rec.get("page_numbers") or [],
                            "content_types": rec.get("content_types") or [],
                            "element_types": rec.get("element_types") or [],
                            "has_enhanced_summary": bool(rec.get("enhanced_summary")),
                        },
                    }
                )
            except Exception as exc:
                logger.warning("embed fail chunk=%s: %s", cidx, exc)
                failed.append(cidx)

            pct = 65 + int(((i + 1) / total) * 30) if total else 65
            _progress(
                file_id,
                progress=pct,
                step="embedding",
                message=f"向量化中... {i + 1}/{total}",
                processed_chunks=i + 1,
            )

        if not embedded:
            raise RuntimeError(f"全部切片向量化失败 failed={failed}")

        inserted = milvus_service.insert_vectors(
            unit_id=unit_id,
            knowledge_base_id=kb_id,
            owner_user_id=owner_user_id,
            file_id=file_id,
            chunks=embedded,
            embedding_model=s.dashscope_embedding_model,
            embedding_version=s.embedding_version,
        )
        for item in embedded:
            vid = milvus_service.make_vector_id(
                unit_id, kb_id, file_id, int(item["chunk_index"])
            )
            file_repo.mark_chunk_embedded(file_id, int(item["chunk_index"]), vid)

        file_repo.update_field(file_id, "embedded_chunks", inserted)
        file_repo.update_status(file_id, "processed", chunks_count=chunks_count)
        _progress(
            file_id,
            progress=100,
            step="completed",
            message="处理完成！",
            processed_chunks=chunks_count,
            status="processed",
        )
        redis_mgr.release_file_lock(file_id)
        # 语料变更 → INCR corpus_ver，使 unit 级 QA 缓存自然 miss
        try:
            redis_mgr.bump_corpus_version(unit_id, kb_id)
        except Exception as bump_exc:
            logger.warning("bump_corpus_version after process failed: %s", bump_exc)

        logger.info(
            "==== process_file done file_id=%s elements=%s chunks=%s vectors=%s ====",
            file_id,
            elements_count,
            chunks_count,
            inserted,
        )
        return {
            "status": "success",
            "file_id": file_id,
            "elements_count": elements_count,
            "chunks_count": chunks_count,
            "embedded_count": inserted,
            "failed_chunks": failed,
        }

    except Exception as exc:
        logger.exception("process_file failed file_id=%s", file_id)
        _fail(file_id, exc)
        raise
    finally:
        if temp_path and os.path.isfile(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass
