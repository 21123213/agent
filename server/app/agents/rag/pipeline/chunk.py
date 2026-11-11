"""chunk_by_title 切片 → 基础记录（可先入库供详情页展示）。"""
from __future__ import annotations

import os
from typing import Any, Optional

from app.settings import get_settings


def chunk_elements_by_title(raw_elements: list[Any]) -> list[Any]:
    """优先 Unstructured chunk_by_title；失败则按字符窗口硬切。"""
    s = get_settings()
    max_chars = s.chunk_max_characters
    new_after = min(s.chunk_new_after_n_chars, max_chars)
    combine = min(s.chunk_combine_text_under_n_chars, max_chars)
    overlap = s.chunk_overlap
    if overlap >= max_chars:
        overlap = max(0, max_chars // 4)

    try:
        from unstructured.chunking.title import chunk_by_title

        chunks = chunk_by_title(
            raw_elements,
            max_characters=max_chars,
            new_after_n_chars=new_after,
            combine_text_under_n_chars=combine,
            overlap=overlap,
            overlap_all=bool(s.chunk_overlap_all),
            include_orig_elements=True,
        )
        print(f"[chunk] chunk_by_title -> {len(chunks)}")
        return chunks
    except Exception as exc:
        print(f"[chunk] chunk_by_title failed, fallback window: {exc}")
        return _fallback_window_chunks(raw_elements, max_chars=max_chars, overlap=overlap)


def _fallback_window_chunks(
    raw_elements: list[Any], *, max_chars: int, overlap: int
) -> list[Any]:
    """无 Unstructured chunk 时：拼全文再按窗口切。"""
    from types import SimpleNamespace

    class _Chunk:
        def __init__(self, text: str):
            self.text = text
            self.category = "CompositeElement"
            self.metadata = SimpleNamespace(orig_elements=[], page_number=None)

        def __str__(self) -> str:
            return self.text

    full = "\n\n".join(str(e).strip() for e in raw_elements if str(e).strip())
    if not full:
        return []
    step = max(1, max_chars - max(0, overlap))
    out = [_Chunk(full[i : i + max_chars]) for i in range(0, len(full), step)]
    print(f"[chunk] fallback window -> {len(out)}")
    return out


def separate_content_types(chunk: Any) -> dict[str, Any]:
    content: dict[str, Any] = {
        "text": str(chunk),
        "table": [],
        "image": [],
        "types": ["text"],
        "element_types": [],
    }
    orig = getattr(getattr(chunk, "metadata", None), "orig_elements", None)
    if not orig:
        return content
    for element in orig:
        element_type = type(element).__name__
        category = getattr(element, "category", element_type)
        if category not in content["element_types"]:
            content["element_types"].append(category)
        if element_type == "Table":
            content["types"].append("table")
            content["table"].append(getattr(element, "text_as_html", None) or str(element))
        elif element_type == "Image":
            meta = getattr(element, "metadata", None)
            image_b64 = getattr(meta, "image_base64", None) if meta else None
            if image_b64:
                content["types"].append("image")
                content["image"].append(image_b64)
    content["types"] = list(set(content["types"]))
    return content


def _extract_page_numbers(chunk: Any) -> list[int]:
    pages = set()
    orig = getattr(getattr(chunk, "metadata", None), "orig_elements", None) or []
    for element in orig:
        meta = getattr(element, "metadata", None)
        if meta and getattr(meta, "page_number", None) is not None:
            pages.add(meta.page_number)
    if not pages and hasattr(chunk, "metadata"):
        pn = getattr(chunk.metadata, "page_number", None)
        if pn is not None:
            pages.add(pn)
    return sorted(pages)


def build_element_id_map(element_dicts: list[dict[str, Any]]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for elem in element_dicts:
        meta = elem.get("metadata") or {}
        for key in ("element_id", "id"):
            eid = meta.get(key)
            if eid is not None:
                mapping[str(eid)] = elem["index"]
        if elem.get("id") is not None:
            mapping[str(elem["id"])] = elem["index"]
    return mapping


def _extract_element_ids(chunk: Any, element_id_map: dict[str, int]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    orig = getattr(getattr(chunk, "metadata", None), "orig_elements", None) or []
    for element in orig:
        candidates: list[str] = []
        eid = getattr(element, "id", None)
        if eid is not None:
            candidates.append(str(eid))
        meta: dict = {}
        if hasattr(element, "metadata") and element.metadata is not None:
            if hasattr(element.metadata, "to_dict"):
                meta = element.metadata.to_dict() or {}
            elif isinstance(element.metadata, dict):
                meta = element.metadata
        for key in ("element_id", "id"):
            if meta.get(key) is not None:
                candidates.append(str(meta[key]))
        for c in candidates:
            mapped = element_id_map.get(c)
            if mapped is not None and mapped not in seen:
                seen.add(mapped)
                ids.append(mapped)
    return ids


def document_title(filename: Optional[str]) -> str:
    if not filename:
        return ""
    base = os.path.basename(str(filename).replace("\\", "/")).strip()
    stem, _ = os.path.splitext(base)
    return (stem or base).strip()


def prepend_doc_title(text: str, doc_title: str) -> str:
    text = (text or "").strip()
    title = (doc_title or "").strip()
    if not title:
        return text
    marker = f"[Document: {title}]"
    if text.startswith(marker):
        return text
    if not text:
        return marker
    return f"{marker}\n{text}"


def build_basic_chunk_records(
    unstructured_chunks: list[Any],
    *,
    element_id_map: Optional[dict[str, int]] = None,
    doc_title: str = "",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    element_id_map = element_id_map or {}
    doc_title = (doc_title or "").strip()

    for chunk in unstructured_chunks:
        content = separate_content_types(chunk)
        raw_text = content["text"].strip()
        if not raw_text and not content["table"] and not content["image"]:
            continue
        titled = prepend_doc_title(raw_text, doc_title)
        orig = getattr(getattr(chunk, "metadata", None), "orig_elements", None) or []
        element_types = content["element_types"] or list(content["types"] or ["text"])
        records.append(
            {
                "chunk_index": len(records),
                "text": titled,
                "text_for_embedding": titled,
                "doc_title": doc_title,
                "enhanced_summary": "",
                "char_count": len(titled),
                "element_count": len(orig) or 1,
                "element_ids": _extract_element_ids(chunk, element_id_map),
                "page_numbers": _extract_page_numbers(chunk),
                "content_types": content["types"] or ["text"],
                "element_types": element_types,
                "has_mixed_content": bool(content["table"] or content["image"]),
                "_content_data": content,
            }
        )
    return records


def build_embedding_text(raw_text: str, enhanced_summary: str) -> str:
    raw_text = (raw_text or "").strip()
    enhanced_summary = (enhanced_summary or "").strip()
    if not enhanced_summary:
        return raw_text
    return f"{raw_text}\n\n[检索增强描述]\n{enhanced_summary}"


def enhance_chunk_records(
    records: list[dict[str, Any]],
    *,
    progress_callback=None,
) -> list[dict[str, Any]]:
    """
    LLM 增强（可选）。LLM_ENHANCE_MODE=off 时直接跳过。
    mixed：仅含表/图的切片；all：全部切片。
    """
    mode = get_settings().llm_enhance_mode.strip().lower()
    total = len(records)
    if mode == "off":
        for idx, record in enumerate(records):
            record["text_for_embedding"] = record.get("text") or ""
            if progress_callback:
                progress_callback(idx + 1, total)
        return records

    for idx, record in enumerate(records):
        content = record.get("_content_data") or {
            "text": record.get("text", ""),
            "table": [],
            "image": [],
        }
        has_mixed = bool(record.get("has_mixed_content"))
        summary = ""
        if mode == "all" or (mode == "mixed" and has_mixed):
            try:
                from app.agents.rag.pipeline.enhance import create_ai_enhanced_summary

                summary = create_ai_enhanced_summary(
                    content.get("text", ""),
                    content.get("table", []),
                    content.get("image", []),
                )
            except Exception:
                summary = ""
        record["enhanced_summary"] = summary
        record["text_for_embedding"] = build_embedding_text(
            record.get("text", ""), summary
        )
        if progress_callback:
            progress_callback(idx + 1, total)
    return records
