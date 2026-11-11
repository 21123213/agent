from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class FileMetadataOut(BaseModel):
    id: int
    user_id: int
    unit_id: int
    knowledge_base_id: int
    filename: str
    original_filename: str
    file_size: int
    file_type: Optional[str] = None
    mime_type: Optional[str] = None
    status: Optional[str] = None
    chunks_count: Optional[int] = 0
    total_elements: Optional[int] = 0
    total_chunks: Optional[int] = 0
    embedded_chunks: Optional[int] = 0
    elements_stats: Optional[Any] = None
    error_message: Optional[str] = None
    upload_time: Optional[datetime] = None
    processed_time: Optional[datetime] = None


class FileListResponse(BaseModel):
    items: list[FileMetadataOut]
    total: int
    page: int
    size: int


class UploadItemResult(BaseModel):
    original_filename: str
    file_id: Optional[int] = None
    status: str  # pending | rejected | queued
    message: str = ""


class UploadBatchResponse(BaseModel):
    batch_status: str  # success | partial_success | all_failed
    items: list[UploadItemResult]


class MessageResponse(BaseModel):
    success: bool = True
    message: str
    data: Optional[dict[str, Any]] = None


class BatchDeleteRequest(BaseModel):
    file_ids: list[int] = Field(..., min_length=1)
