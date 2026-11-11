"""
文本向量化（DashScope Embeddings）。

仅供入库 / 检索使用；对话 LLM 不走这里。
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import List

from langchain_community.embeddings import DashScopeEmbeddings

from app.settings import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def _client() -> DashScopeEmbeddings:
    s = get_settings()
    key = s.effective_embedding_api_key
    if not key:
        raise RuntimeError("缺少 DASHSCOPE_EMBEDDING_API_KEY / DASHSCOPE_API_KEY")
    return DashScopeEmbeddings(
        model=s.dashscope_embedding_model,
        dashscope_api_key=key,
    )


def embed_document(text: str) -> List[float]:
    """单条文档切片向量。"""
    vecs = _client().embed_documents([text or " "])
    return list(vecs[0])


def embed_query(text: str) -> List[float]:
    return list(_client().embed_query(text or " "))
