"""多日天气。"""
from __future__ import annotations

from typing import Any

from app.agents.reasoner.travel.clients.qweather import fetch_weather_bundle
from app.agents.reasoner.travel.services._common import fail, ok, require

_OUTDOOR_BAD = ("雨", "雪", "暴", "雷", "冰雹", "沙尘")


async def get_weather(
    destination: str, start_date: str, days: int
) -> dict[str, Any]:
    try:
        require(
            {"destination": destination, "start_date": start_date, "days": days},
            ["destination", "start_date", "days"],
            "get_weather",
        )
        days_n = int(days)
        if days_n < 1:
            return fail("days 必须 >= 1", "invalid_params")
        bundle = await fetch_weather_bundle(destination)
        if not bundle.get("ok"):
            return fail(bundle.get("error") or "天气失败", "upstream_error")
        rows = ((bundle.get("daily_forecast") or {}).get("data") or {}).get(
            "daily"
        ) or []
        if start_date:
            filtered = [r for r in rows if r.get("fxDate", "") >= start_date]
            if filtered:
                rows = filtered
        daily = []
        for row in rows[:days_n]:
            cond = row.get("textDay") or row.get("textNight") or "未知"
            outdoor_ok = not any(k in cond for k in _OUTDOOR_BAD)
            daily.append(
                {
                    "date": row.get("fxDate", ""),
                    "condition": cond,
                    "temp": f"{row.get('tempMax', '')}/{row.get('tempMin', '')}",
                    "outdoor_ok": outdoor_ok,
                }
            )
        if not daily:
            return fail("无可用预报", "empty_result")
        loc = bundle.get("location") or {}
        summary = f"{destination} {daily[0]['date']} 起 {len(daily)} 天：" + "、".join(
            d["condition"] for d in daily[:3]
        )
        return ok(
            destination=destination,
            daily=daily,
            summary=summary,
            location_id=loc.get("id"),
            location_name=loc.get("name"),
            source="qweather",
        )
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
