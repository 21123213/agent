"""和风天气（异步）。"""
from __future__ import annotations

import logging
from typing import Any

from app.agents.reasoner.travel.env import env_str
from app.agents.reasoner.travel.http import http_get

logger = logging.getLogger(__name__)


async def fetch_weather_bundle(city_name: str) -> dict[str, Any]:
    key = env_str("qweather_key")
    host = env_str("qweather_host")
    if not key or not host:
        return {"ok": False, "error": "未配置 QWEATHER_KEY / QWEATHER_HOST"}

    base = host.rstrip("/")
    common = {"key": key, "lang": "zh", "unit": "m"}

    city = await http_get(
        f"{base}/geo/v2/city/lookup",
        params={**common, "location": city_name},
        timeout=10.0,
    )
    if not city.get("ok"):
        return {"ok": False, "error": city.get("error") or "城市查询失败"}
    data = city.get("data") or {}
    if data.get("code") != "200":
        return {"ok": False, "error": f"和风 code={data.get('code')}"}
    locations = data.get("location") or []
    if not locations:
        return {"ok": False, "error": f"未找到城市: {city_name}"}
    location = locations[0]
    loc_id = location["id"]

    daily = await http_get(
        f"{base}/v7/weather/30d",
        params={**common, "location": loc_id},
        timeout=10.0,
    )
    if not daily.get("ok"):
        return {"ok": False, "error": daily.get("error"), "location": location}
    ddata = daily.get("data") or {}
    if ddata.get("code") != "200":
        return {
            "ok": False,
            "error": f"预报 code={ddata.get('code')}",
            "location": location,
        }

    logger.info("qweather city=%s id=%s", city_name, loc_id)
    return {
        "ok": True,
        "location": location,
        "daily_forecast": {"data": ddata},
    }
