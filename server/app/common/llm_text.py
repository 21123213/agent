"""LLM 响应文本归一化（跨厂商 content 形态）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union


def normalize_llm_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
                    continue
                if item.get("type") == "text" and item.get("text"):
                    parts.append(str(item["text"]))
        return "\n".join(parts).strip()
    return str(content).strip()


def extract_stream_delta(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    if content is None and isinstance(chunk, dict):
        content = chunk.get("content")
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
        return "".join(parts)
    return str(content)


def to_openai_image_url_part(image_b64: str) -> Optional[Dict[str, Any]]:
    if not image_b64 or not isinstance(image_b64, str):
        return None
    raw = image_b64.strip()
    if not raw:
        return None
    if raw.startswith("data:image"):
        url = raw
    else:
        mime = "image/png" if raw.startswith("iVBOR") else "image/jpeg"
        url = f"data:{mime};base64,{raw}"
    return {"type": "image_url", "image_url": {"url": url}}


def multimodal_user_content(
    text: str,
    images_b64: Optional[List[str]] = None,
) -> Union[str, List[Dict[str, Any]]]:
    images_b64 = images_b64 or []
    parts: List[Dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for img in images_b64:
        item = to_openai_image_url_part(img)
        if item:
            parts.append(item)
    if not parts:
        return text or ""
    if len(parts) == 1 and parts[0].get("type") == "text":
        return text
    return parts
