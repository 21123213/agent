"""通用网页搜索（博查，core 工具）。"""
from __future__ import annotations

from typing import Any

from app.agents.reasoner.travel.clients.bocha import BochaClient
from app.agents.reasoner.travel.env import env_int


def _compact_snippets(data: dict[str, Any]) -> dict[str, Any]:
    cap = env_int("web_search_snippet_max", 5)
    summary_chars = env_int("web_search_summary_chars", 500)
    raw = list(data.get("snippets") or [])
    seen_urls: set[str] = set()
    compact: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        summary = str(item.get("summary") or "").strip()
        if len(summary) > summary_chars:
            summary = summary[:summary_chars] + "…"
        compact.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "summary": summary,
                "site": str(item.get("site") or "").strip(),
                "published": str(item.get("published") or "").strip(),
            }
        )
        if len(compact) >= cap:
            break
    out = dict(data)
    out["snippets"] = compact
    out["truncated"] = True
    out["snippet_cap"] = cap
    out["summary_chars"] = summary_chars
    return out


async def web_search(
    query: str,
    freshness: str = "oneYear",
    count: int = 0,
) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空", "snippets": []}
    default_n = env_int("web_search_default_count", 5)
    snippet_cap = env_int("web_search_snippet_max", 5)
    try:
        default_n = default_n if not count else int(count)
        n = max(1, min(default_n, snippet_cap, 20))
    except (TypeError, ValueError):
        n = min(default_n, snippet_cap)
    data = await BochaClient().web_search(
        q, freshness=freshness or "oneYear", count=n
    )
    if not isinstance(data, dict):
        data = {"ok": False, "snippets": [], "error": "invalid response"}
    return _compact_snippets(data)
