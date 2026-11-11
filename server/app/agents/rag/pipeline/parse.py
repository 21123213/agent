"""
文档解析 → 元素列表。

txt/md：轻量路径（不调 Unstructured partition，避免首次拉 NLTK 卡住）。
pdf/docx：Unstructured（pdf 可 hi_res，失败降级 fast）。
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from app.settings import get_settings

logger = logging.getLogger(__name__)


def _ocr_languages() -> list[str]:
    raw = get_settings().unstructured_ocr_languages
    parts = raw.replace("+", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


def parse_document(file_path: str) -> tuple[list[Any], list[dict[str, Any]]]:
    """
    返回 (原始 Element 列表, 入库用 dict 列表)。

    dict 字段: index / type / text / metadata
    """
    os.environ.setdefault("UNSTRUCTURED_DISABLE_NOTICE", "1")
    ext = os.path.splitext(file_path)[1].lower()
    languages = _ocr_languages()
    strategy = get_settings().unstructured_pdf_strategy.strip().lower()

    if ext in (".txt", ".md"):
        # 关键路径：纯文本切段，不触发 Unstructured/NLTK 下载
        return _parse_plain_text(file_path)

    if ext == ".pdf":
        raw = _parse_pdf(file_path, languages, strategy)
    elif ext == ".docx":
        from unstructured.partition.docx import partition_docx

        print(f"[parse] docx via unstructured: {file_path}")
        raw = partition_docx(filename=file_path, languages=languages)
    else:
        from unstructured.partition.auto import partition

        print(f"[parse] auto via unstructured: {file_path}")
        raw = partition(
            filename=file_path,
            strategy=strategy,
            include_page_breaks=True,
            infer_table_structure=True,
            languages=languages,
        )

    return _elements_to_dicts(raw)


def _parse_plain_text(file_path: str) -> tuple[list[Any], list[dict[str, Any]]]:
    """
    按空行 / Markdown 标题切成 NarrativeText / Title 元素，
    供后续 chunk_by_title 使用（对象来自 unstructured.documents.elements）。
    """
    from unstructured.documents.elements import NarrativeText, Title

    text = Path(file_path).read_text(encoding="utf-8", errors="ignore")
    print(f"[parse] plain text path chars={len(text)} file={file_path}")

    raw: list[Any] = []
    # 先按空行分段，再识别 markdown 标题
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.splitlines()
        # 整段是一级标题
        if len(lines) == 1 and re.match(r"^#{1,6}\s+\S", lines[0]):
            title = re.sub(r"^#{1,6}\s+", "", lines[0]).strip()
            raw.append(Title(text=title))
            continue
        # 段首是标题，其余当正文
        if re.match(r"^#{1,6}\s+\S", lines[0]):
            title = re.sub(r"^#{1,6}\s+", "", lines[0]).strip()
            raw.append(Title(text=title))
            body = "\n".join(lines[1:]).strip()
            if body:
                raw.append(NarrativeText(text=body))
            continue
        raw.append(NarrativeText(text=block))

    if not raw and text.strip():
        raw.append(NarrativeText(text=text.strip()))

    return _elements_to_dicts(raw)


def _elements_to_dicts(raw: list[Any]) -> tuple[list[Any], list[dict[str, Any]]]:
    element_dicts: list[dict[str, Any]] = []
    for idx, elem in enumerate(raw):
        metadata = {}
        if hasattr(elem, "metadata") and hasattr(elem.metadata, "to_dict"):
            try:
                metadata = elem.metadata.to_dict() or {}
            except Exception:
                metadata = {}
        category = getattr(elem, "category", None) or type(elem).__name__
        element_dicts.append(
            {
                "index": idx,
                "type": category,
                "text": str(elem),
                "metadata": metadata,
            }
        )
    print(f"[parse] done elements={len(element_dicts)}")
    return raw, element_dicts


def _parse_pdf(file_path: str, languages: list[str], strategy: str) -> list[Any]:
    from unstructured.partition.pdf import partition_pdf

    if strategy == "fast":
        print("[parse] PDF strategy=fast")
        return partition_pdf(
            filename=file_path,
            strategy="fast",
            include_page_breaks=True,
            infer_table_structure=False,
            languages=languages,
        )

    try:
        print("[parse] PDF strategy=hi_res")
        return partition_pdf(
            filename=file_path,
            strategy="hi_res",
            include_page_breaks=True,
            infer_table_structure=True,
            extract_image_block_types=["Image"],
            extract_image_block_to_payload=True,
            languages=languages,
        )
    except Exception as exc:
        print(f"[parse] hi_res failed, fallback fast: {exc}")
        return partition_pdf(
            filename=file_path,
            strategy="fast",
            include_page_breaks=True,
            infer_table_structure=False,
            languages=languages,
        )


def count_element_types(elements: list[dict]) -> dict[str, int]:
    stats: dict[str, int] = {}
    for elem in elements:
        t = elem["type"]
        stats[t] = stats.get(t, 0) + 1
    return stats
