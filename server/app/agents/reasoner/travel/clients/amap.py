"""高德：地理编码、酒店周边、路径（异步）。"""
from __future__ import annotations

from typing import Any, Literal

from app.agents.reasoner.travel.env import env_int, env_str
from app.agents.reasoner.travel.http import http_get

HotelTier = Literal["luxury", "comfort", "economy"]

TIER_KEYWORDS: dict[HotelTier, str] = {
    "luxury": "五星级酒店",
    "comfort": "精品酒店",
    "economy": "经济型酒店",
}


class AmapClient:
    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        key = env_str("amap_key")
        base = env_str("amap_api_base", "https://restapi.amap.com")
        if not key:
            return {"status": "0", "info": "未配置 AMAP_KEY"}
        wrapped = await http_get(
            f"{base}{path}",
            params={**params, "key": key},
            timeout=15.0,
        )
        if not wrapped.get("ok"):
            return {"status": "0", "info": wrapped.get("error")}
        data = wrapped.get("data")
        return data if isinstance(data, dict) else {"status": "0", "info": "invalid json"}

    async def geocode(self, address: str, city: str) -> dict[str, Any]:
        body = await self._get("/v3/geocode/geo", {"address": address, "city": city})
        if body.get("status") != "1" or not body.get("geocodes"):
            return {
                "ok": False,
                "error": body.get("info", "geocode 失败"),
                "lat": 0,
                "lng": 0,
            }
        geo = body["geocodes"][0]
        location = geo.get("location", "")
        if not location or "," not in location:
            return {"ok": False, "error": "无坐标", "lat": 0, "lng": 0}
        lng_str, lat_str = location.split(",", 1)
        return {
            "ok": True,
            "lat": float(lat_str),
            "lng": float(lng_str),
            "address": geo.get("formatted_address", address),
        }

    async def search_hotels_near(
        self,
        lat: float,
        lng: float,
        tier: HotelTier,
        city: str,
        radius: int | None = None,
    ) -> dict[str, Any]:
        if radius is None:
            radius = env_int("stay_hotel_search_radius", 3000)
        body = await self._get(
            "/v3/place/around",
            {
                "location": f"{lng},{lat}",
                "keywords": TIER_KEYWORDS[tier],
                "radius": radius,
                "city": city,
                "types": "100100",
                "offset": 5,
            },
        )
        pois = body.get("pois") or []
        if body.get("status") != "1":
            info = str(body.get("info") or "").strip()
            if not info or info.upper() == "OK":
                info = "高德周边搜酒店失败"
            return {"ok": False, "error": info, "hotel": None}
        if not pois:
            return {
                "ok": False,
                "error": f"锚点附近 {radius}m 内未找到「{TIER_KEYWORDS[tier]}」",
                "hotel": None,
            }
        poi = pois[0]
        loc = poi.get("location", "")
        if "," not in loc:
            return {"ok": False, "error": "酒店无坐标", "hotel": None}
        lng_h, lat_h = loc.split(",", 1)
        return {
            "ok": True,
            "hotel": {
                "name": poi.get("name", ""),
                "lat": float(lat_h),
                "lng": float(lng_h),
                "address": poi.get("address", "") or poi.get("name", ""),
                "amap_id": poi.get("id", ""),
            },
        }

    @staticmethod
    def _od(a: dict[str, Any], b: dict[str, Any]) -> tuple[str, str]:
        return (
            f"{a.get('lng', 0)},{a.get('lat', 0)}",
            f"{b.get('lng', 0)},{b.get('lat', 0)}",
        )

    async def direction_walking(
        self, a: dict[str, Any], b: dict[str, Any]
    ) -> dict[str, Any]:
        o, d = self._od(a, b)
        return self._parse_path(
            await self._get("/v3/direction/walking", {"origin": o, "destination": d}),
            "walking",
        )

    async def direction_driving(
        self, a: dict[str, Any], b: dict[str, Any]
    ) -> dict[str, Any]:
        o, d = self._od(a, b)
        return self._parse_path(
            await self._get("/v3/direction/driving", {"origin": o, "destination": d}),
            "driving",
        )

    async def direction_transit(
        self, a: dict[str, Any], b: dict[str, Any], city: str
    ) -> dict[str, Any]:
        o, d = self._od(a, b)
        body = await self._get(
            "/v3/direction/transit/integrated",
            {
                "origin": o,
                "destination": d,
                "city": city,
                "cityd": city,
                "strategy": 0,
            },
        )
        if body.get("status") != "1":
            return {"ok": False, "error": body.get("info", "公交失败")}
        transits = (body.get("route") or {}).get("transits") or []
        if not transits:
            return {"ok": False, "error": "无公交方案"}
        t0 = transits[0]
        duration_min = max(1, int(int(t0.get("duration", 0)) / 60))
        distance_km = float(t0.get("distance", 0)) / 1000.0
        return {
            "ok": True,
            "mode": "transit",
            "duration_min": duration_min,
            "distance_km": round(distance_km, 2),
            "summary": f"公交/地铁约 {duration_min} 分钟",
            "cost_estimate": 4.0,
            "source": "amap",
        }

    @staticmethod
    def _parse_path(body: dict[str, Any], mode: str) -> dict[str, Any]:
        if body.get("status") != "1":
            return {"ok": False, "error": body.get("info", "路径失败")}
        route = (body.get("route") or {}).get("paths") or []
        if not route:
            return {"ok": False, "error": "无路径"}
        path = route[0]
        distance_km = float(path.get("distance", 0)) / 1000.0
        duration_min = max(1, int(int(path.get("duration", 0)) / 60))
        cost = round(distance_km * 2.5, 1) if mode == "driving" else 0.0
        label = "步行" if mode == "walking" else "驾车/打车"
        return {
            "ok": True,
            "mode": mode,
            "duration_min": duration_min,
            "distance_km": round(distance_km, 2),
            "summary": f"{label}约 {duration_min} 分钟",
            "cost_estimate": cost,
            "source": "amap",
        }
