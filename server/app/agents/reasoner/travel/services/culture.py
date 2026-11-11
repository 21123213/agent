"""闭馆/活动线索：博查检索 + 轻量关键词归类（不做强过滤结论）。"""
from __future__ import annotations

import re
from typing import Any

from app.agents.reasoner.travel.clients.bocha import BochaClient
from app.agents.reasoner.travel.services._common import fail, ok, require

_HOLIDAY = ("节", "假", "休", "调休", "国庆", "春节")
_CLOSURE = ("闭馆", "暂停", "关闭", "周一", "维护")
_ETIQUETTE = ("礼仪", "注意", "习俗", "文明", "禁止")
_EVENT = ("活动", "展会", "音乐节", "庙会", "民俗")

_SEASON_CN = {"春": "春季", "夏": "夏季", "秋": "秋季", "冬": "冬季"}


def _pick(texts: list[str], keywords: tuple[str, ...], limit: int = 5) -> list[str]:
    picked: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if not any(k in text for k in keywords):
            continue
        snippet = re.sub(r"\s+", " ", text.strip())
        if snippet and snippet not in seen:
            seen.add(snippet)
            picked.append(snippet)
        if len(picked) >= limit:
            break
    return picked


def _season_label(start_date: str, season: str) -> str:
    s = (season or "").strip()
    if s in _SEASON_CN:
        return _SEASON_CN[s]
    if s:
        return s
    # 粗推月份
    try:
        month = int(str(start_date)[5:7])
    except (TypeError, ValueError):
        return ""
    if month in (3, 4, 5):
        return "春季"
    if month in (6, 7, 8):
        return "夏季"
    if month in (9, 10, 11):
        return "秋季"
    return "冬季"


async def get_culture(
    destination: str, start_date: str, season: str = ""
) -> dict[str, Any]:
    try:
        require(
            {"destination": destination, "start_date": start_date},
            ["destination", "start_date"],
            "get_culture",
        )
        season_l = _season_label(start_date, season)
        queries = [
            f"{destination} 景点 开放时间 闭馆",
            f"{destination} {season_l} 活动 节庆".strip(),
            f"{destination} 旅游 注意事项",
        ]
        client = BochaClient()
        texts: list[str] = []
        errors: list[str] = []
        for q in queries:
            data = await client.web_search(q, freshness="oneYear", count=5)
            if not data.get("ok"):
                errors.append(str(data.get("error") or "search_failed"))
                continue
            for sn in data.get("snippets") or []:
                if isinstance(sn, dict):
                    blob = " ".join(
                        str(sn.get(k) or "") for k in ("title", "summary")
                    )
                    if blob.strip():
                        texts.append(blob.strip())
        if not texts:
            return fail(
                "；".join(errors) if errors else "文化线索检索为空",
                "empty_result",
            )
        return ok(
            destination=destination,
            start_date=start_date,
            season=season_l,
            closures=_pick(texts, _CLOSURE),
            holidays=_pick(texts, _HOLIDAY),
            events=_pick(texts, _EVENT),
            etiquette=_pick(texts, _ETIQUETTE),
            raw_snippets=texts[:12],
            note="原始检索线索，请按行程日期自行判断相关性；勿编造未出现的闭馆/票价。",
            source="bocha",
        )
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
