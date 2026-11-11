"""
file_metadata / file_content / document_elements / document_chunks 仓储。

上传链路：create_metadata → save_content →（Celery）更新 status / 写入 elements&chunks
删除链路：先清 Milvus，再 DELETE file_metadata（FK CASCADE 清 content/elements/chunks）
"""
from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from app.infra.db import execute, fetch_all, fetch_one


# ---------------------------------------------------------------------------
# file_metadata
# ---------------------------------------------------------------------------


def create_metadata(
    *,
    user_id: int,
    unit_id: int,
    knowledge_base_id: int,
    filename: str,
    original_filename: str,
    file_size: int,
    file_type: str,
    mime_type: str | None,
    storage_backend: str = "db",
    file_path: str | None = None,
    content_sha256: str | None = None,
    status: str = "pending",
) -> int:
    return execute(
        "INSERT INTO file_metadata "
        "(user_id, unit_id, knowledge_base_id, filename, original_filename, "
        " file_path, storage_backend, content_sha256, file_size, file_type, "
        " mime_type, status) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            user_id,
            unit_id,
            knowledge_base_id,
            filename,
            original_filename,
            file_path or f"db://pending",
            storage_backend,
            content_sha256,
            file_size,
            file_type,
            mime_type,
            status,
        ),
    )


def get_by_id(file_id: int) -> Optional[dict[str, Any]]:
    return fetch_one("SELECT * FROM file_metadata WHERE id = %s", (file_id,))


def list_by_kb(
    knowledge_base_id: int,
    *,
    unit_id: int,
    page: int = 1,
    size: int = 100,
) -> list[dict[str, Any]]:
    offset = max(0, (page - 1) * size)
    return fetch_all(
        "SELECT * FROM file_metadata "
        "WHERE knowledge_base_id = %s AND unit_id = %s "
        "ORDER BY upload_time DESC LIMIT %s OFFSET %s",
        (knowledge_base_id, unit_id, size, offset),
    )


def count_by_kb(knowledge_base_id: int, *, unit_id: int) -> int:
    row = fetch_one(
        "SELECT COUNT(*) AS c FROM file_metadata "
        "WHERE knowledge_base_id = %s AND unit_id = %s",
        (knowledge_base_id, unit_id),
    )
    return int(row["c"]) if row else 0


def list_ids_by_kb(knowledge_base_id: int) -> list[int]:
    rows = fetch_all(
        "SELECT id FROM file_metadata WHERE knowledge_base_id = %s",
        (knowledge_base_id,),
    )
    return [int(r["id"]) for r in rows]


def update_status(
    file_id: int,
    status: str,
    *,
    chunks_count: int | None = None,
    error_message: str | None = None,
) -> None:
    if chunks_count is not None:
        execute(
            "UPDATE file_metadata SET status=%s, chunks_count=%s, error_message=%s, "
            "processed_time=NOW(), completed_processing_time=NOW() WHERE id=%s",
            (status, chunks_count, error_message, file_id),
        )
    else:
        execute(
            "UPDATE file_metadata SET status=%s, error_message=%s WHERE id=%s",
            (status, error_message, file_id),
        )


def update_field(file_id: int, field: str, value: Any) -> None:
    """白名单字段更新，防注入列名。"""
    allowed = {
        "status",
        "chunks_count",
        "error_message",
        "total_elements",
        "total_chunks",
        "embedded_chunks",
        "elements_stats",
        "file_path",
        "started_processing_time",
        "completed_processing_time",
        "processed_time",
    }
    if field not in allowed:
        raise ValueError(f"field not allowed: {field}")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    if field in ("started_processing_time", "completed_processing_time", "processed_time") and value == "NOW()":
        execute(f"UPDATE file_metadata SET {field}=NOW() WHERE id=%s", (file_id,))
        return
    execute(f"UPDATE file_metadata SET {field}=%s WHERE id=%s", (value, file_id))


def delete_file(file_id: int) -> None:
    execute("DELETE FROM file_metadata WHERE id = %s", (file_id,))


# ---------------------------------------------------------------------------
# file_content (LONGBLOB)
# ---------------------------------------------------------------------------


def save_content(file_id: int, content: bytes) -> int:
    return execute(
        "INSERT INTO file_content (file_id, content) VALUES (%s, %s)",
        (file_id, content),
    )


def get_content(file_id: int) -> Optional[bytes]:
    row = fetch_one(
        "SELECT content FROM file_content WHERE file_id = %s",
        (file_id,),
    )
    if not row:
        return None
    raw = row["content"]
    return bytes(raw) if raw is not None else None


def update_file_path_after_insert(file_id: int) -> None:
    execute(
        "UPDATE file_metadata SET file_path=%s WHERE id=%s",
        (f"db://{file_id}", file_id),
    )


# ---------------------------------------------------------------------------
# document_elements / document_chunks
# ---------------------------------------------------------------------------


def clear_elements(file_id: int) -> None:
    execute("DELETE FROM document_elements WHERE file_id = %s", (file_id,))


def clear_chunks(file_id: int) -> None:
    execute("DELETE FROM document_chunks WHERE file_id = %s", (file_id,))


def insert_element(
    *,
    file_id: int,
    element_index: int,
    element_type: str,
    text_content: str | None,
    metadata: dict | None,
) -> int:
    return execute(
        "INSERT INTO document_elements "
        "(file_id, element_index, element_type, text_content, metadata) "
        "VALUES (%s,%s,%s,%s,%s)",
        (
            file_id,
            element_index,
            element_type,
            text_content,
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )


def insert_chunk(
    *,
    file_id: int,
    chunk_index: int,
    chunk_text: str,
    char_count: int,
    element_count: int,
    element_ids: Sequence[int],
    page_numbers: Sequence[int],
    content_types: Sequence[str] | None = None,
    element_types: Sequence[str] | None = None,
) -> int:
    return execute(
        "INSERT INTO document_chunks "
        "(file_id, chunk_index, chunk_text, char_count, element_count, "
        " element_ids, page_numbers, content_types, element_types, is_embedded) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0)",
        (
            file_id,
            chunk_index,
            chunk_text,
            char_count,
            element_count,
            json.dumps(list(element_ids)),
            json.dumps(list(page_numbers)),
            json.dumps(list(content_types or [])),
            json.dumps(list(element_types or [])),
        ),
    )


def mark_chunk_embedded(
    file_id: int,
    chunk_index: int,
    vector_id: str,
) -> None:
    execute(
        "UPDATE document_chunks SET vector_id=%s, is_embedded=1, embedded_at=NOW() "
        "WHERE file_id=%s AND chunk_index=%s",
        (vector_id, file_id, chunk_index),
    )


def get_elements(file_id: int) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT id, file_id, element_index, element_type, text_content, metadata, created_at "
        "FROM document_elements WHERE file_id=%s ORDER BY element_index",
        (file_id,),
    )
    for r in rows:
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                r["metadata"] = json.loads(meta)
            except json.JSONDecodeError:
                r["metadata"] = {}
    return rows


def get_chunks(file_id: int) -> list[dict[str, Any]]:
    rows = fetch_all(
        "SELECT * FROM document_chunks WHERE file_id=%s ORDER BY chunk_index",
        (file_id,),
    )
    for r in rows:
        for key in ("element_ids", "page_numbers", "content_types", "element_types"):
            val = r.get(key)
            if isinstance(val, str):
                try:
                    r[key] = json.loads(val)
                except json.JSONDecodeError:
                    r[key] = []
    return rows


def search_chunks_for_bm25(
    *,
    unit_id: int,
    knowledge_base_id: int,
    file_ids: Optional[Sequence[int]] = None,
    limit: int = 2000,
) -> list[dict[str, Any]]:
    """
    BM25 语料：本 unit + KB 下已入库切片（可再按 file_ids 收窄）。
    """
    params: list[Any] = [unit_id, knowledge_base_id]
    sql = (
        "SELECT c.file_id, c.chunk_index, c.chunk_text, c.page_numbers, c.vector_id "
        "FROM document_chunks c "
        "INNER JOIN file_metadata f ON f.id = c.file_id "
        "WHERE f.unit_id = %s AND f.knowledge_base_id = %s "
        "AND f.status = 'processed' "
    )
    if file_ids:
        placeholders = ",".join(["%s"] * len(file_ids))
        sql += f"AND c.file_id IN ({placeholders}) "
        params.extend(int(x) for x in file_ids)
    sql += "ORDER BY c.file_id, c.chunk_index LIMIT %s"
    params.append(int(limit))
    rows = fetch_all(sql, tuple(params))
    for r in rows:
        pages = r.get("page_numbers")
        if isinstance(pages, str):
            try:
                r["page_numbers"] = json.loads(pages)
            except json.JSONDecodeError:
                r["page_numbers"] = []
    return rows
