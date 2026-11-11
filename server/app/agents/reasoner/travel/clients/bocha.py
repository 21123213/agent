"""博查 Web Search（异步）。"""
from __future__ import annotations

from typing import Any

from app.agents.reasoner.travel.env import env_str
from app.agents.reasoner.travel.http import http_post


class BochaClient:
    async def web_search(
        self,
        query: str,
        *,
        freshness: str = "oneYear",
        count: int = 5,
    ) -> dict[str, Any]:
        key = env_str("bocha_api_key")
        base = env_str("bocha_api_base", "https://api.bochaai.com")
        if not key:
            return {"ok": False, "error": "未配置 BOCHA_API_KEY", "snippets": []}
        url = f"{base}/v1/web-search"
        wrapped = await http_post(
            url,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "summary": True,
                "freshness": freshness,
                "count": count,
            },
            timeout=15.0,
        )
        if not wrapped.get("ok"):
            return {"ok": False, "error": wrapped.get("error"), "snippets": []}
        body = wrapped.get("data") or {}
        data = body.get("data") if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            data = {}
        pages = (data.get("webPages") or {}).get("value") or []
        snippets = [
            {
                "title": item.get("name", ""),
                "url": item.get("url", ""),
                "summary": item.get("summary", ""),
                "site": item.get("siteName", "") or item.get("site", ""),
                "published": item.get("datePublished", "")
                or item.get("published", ""),
            }
            for item in pages
            if isinstance(item, dict)
        ]
        return {"ok": True, "snippets": snippets, "error": None}
