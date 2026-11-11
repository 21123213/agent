"""可选：多模态 LLM 为切片生成检索增强描述（经 llm_client 路由）。"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from app.common.llm_client import Purpose, invoke_chat
from app.common.llm_text import multimodal_user_content, normalize_llm_text
from app.settings import get_settings


def create_ai_enhanced_summary(text: str, tables: list[str], images: list[str]) -> str:
    s = get_settings()
    prompt = (
        "You are a helpful assistant that creates a searchable description for "
        "document content retrieval.\n\n"
        f"THE TEXT:\n{text}\n"
    )
    if tables:
        prompt += "\nTHE TABLES:\n"
        for i, table in enumerate(tables):
            prompt += f"Table {i + 1}: {table[:4000]}\n\n"
    prompt += (
        "\nYOUR TASK:\nGenerate a comprehensive, searchable description covering "
        "key facts, topics, and alternative search terms.\nSEARCHABLE DESCRIPTION:"
    )

    max_images = max(0, s.llm_enhance_max_images)
    content: Any = multimodal_user_content(prompt, images[:max_images] if images else None)
    # purpose=ENHANCE → Settings.dashscope_chat_model（默认 qwen-vl-max），与 PE 强模型无关
    raw = invoke_chat(
        [HumanMessage(content=content)],
        purpose=Purpose.ENHANCE,
        temperature=0.2,
        reason="chunk_enhance",
    )
    return normalize_llm_text(raw)
