"""
旅游域异步 HTTP（httpx）。所有上游调用必须走本模块，禁止同步 requests。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from app.agents.reasoner.travel.env import env_float

logger = logging.getLogger(__name__)

_client: Optional[httpx.AsyncClient] = None
_lock = asyncio.Lock()


async def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is not None and not _client.is_closed:
        return _client
    async with _lock:
        if _client is None or _client.is_closed:
            timeout = env_float("http_timeout", 20.0)
            _client = httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=True,
            )
            logger.info("travel httpx.AsyncClient ready timeout=%s", timeout)
        return _client


async def aclose_http_client() -> None:
    global _client
    async with _lock:
        if _client is not None and not _client.is_closed:
            await _client.aclose()
        _client = None


async def http_get(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    client = await get_http_client()
    try:
        resp = await client.get(
            url, params=params, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return {"ok": True, "data": data, "error": None}
    except Exception as exc:
        logger.warning("http_get failed url=%s err=%s", url, exc)
        return {"ok": False, "data": None, "error": str(exc)}


async def http_post(
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    client = await get_http_client()
    try:
        resp = await client.post(
            url, json=json, headers=headers, timeout=timeout
        )
        resp.raise_for_status()
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return {"ok": True, "data": data, "error": None}
    except Exception as exc:
        logger.warning("http_post failed url=%s err=%s", url, exc)
        return {"ok": False, "data": None, "error": str(exc)}
