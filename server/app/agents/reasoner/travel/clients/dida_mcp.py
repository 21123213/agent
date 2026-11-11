"""
道旅 RollingGo MCP（HTTP JSON-RPC ``tools/call``，异步）。

不是 stdio MCP SDK；详见 ``docs/reasoner-travel-tools.md``。
密钥：环境变量 ``DUTU_API_KEY``（兼容 ``DOUR_API_KEY``）；URL：``DIDA_MCP_URL``。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from app.agents.reasoner.travel.env import env_str
from app.agents.reasoner.travel.http import http_post

logger = logging.getLogger(__name__)

HotelTier = Literal["luxury", "comfort", "economy"]

TIER_STAR_RATINGS: dict[HotelTier, list[float]] = {
    "luxury": [5.0],
    "comfort": [4.0],
    "economy": [2.0, 3.0],
}

TIER_STAR_INT: dict[HotelTier, int] = {
    "luxury": 5,
    "comfort": 4,
    "economy": 2,
}


class DidaHotelClient:
    def __init__(self) -> None:
        self.api_key = env_str("dutu_api_key")
        self.base_url = env_str("dida_mcp_url", "https://mcp.rollinggo.cn/mcp")
        if not self.api_key:
            raise RuntimeError("未配置 DUTU_API_KEY")

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
            "id": 1,
        }
        wrapped = await http_post(
            self.base_url,
            headers=self._headers(),
            json=payload,
            timeout=30.0,
        )
        if not wrapped.get("ok"):
            raise RuntimeError(wrapped.get("error") or "道旅 MCP 请求失败")
        data = wrapped.get("data")
        if isinstance(data, dict) and "text" in data and len(data) == 1:
            return self._parse_response(str(data["text"]))
        if isinstance(data, dict):
            return self._extract_result(data)
        return self._parse_response(str(data or ""))

    def _parse_response(self, text: str) -> Any:
        text = text.strip()
        if not text:
            raise RuntimeError("道旅 MCP 空响应")
        try:
            return self._extract_result(json.loads(text))
        except json.JSONDecodeError:
            pass
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        body = json.loads(chunk)
                        result = self._extract_result(body)
                        if result is not None:
                            return result
                    except json.JSONDecodeError:
                        continue
        raise RuntimeError(f"道旅 MCP 无法解析: {text[:300]}")

    def _extract_result(self, body: dict[str, Any]) -> Any:
        if "error" in body:
            err = body["error"]
            raise RuntimeError(f"道旅 MCP 错误: {err.get('message', err)}")
        result = body.get("result")
        if result is None:
            return None
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict)]
            joined = "\n".join(t for t in texts if t)
            if joined:
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return joined
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        return result

    async def search_hotels(
        self,
        *,
        place: str,
        place_type: str,
        origin_query: str,
        check_in_date: str,
        stay_nights: int,
        tier: HotelTier,
        size: int = 3,
        country_code: str = "CN",
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {
            "originQuery": origin_query,
            "place": place,
            "placeType": place_type,
            "countryCode": country_code,
            "size": size,
            "checkInParam": {
                "checkInDate": check_in_date,
                "stayNights": stay_nights,
            },
            "filterOptions": {"starRatings": TIER_STAR_RATINGS[tier]},
        }
        logger.info("dida searchHotels place=%s tier=%s", place, tier)
        raw = await self._call_tool("searchHotels", arguments)
        return _normalize_hotel_list(raw)

    async def get_hotel_detail(
        self,
        hotel_id: str,
        *,
        check_in_date: str,
        stay_nights: int,
    ) -> dict[str, Any]:
        arguments = {
            "hotelId": hotel_id,
            "checkInParam": {
                "checkInDate": check_in_date,
                "stayNights": stay_nights,
            },
        }
        raw = await self._call_tool("getHotelDetail", arguments)
        return raw if isinstance(raw, dict) else {}


def _normalize_hotel_list(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [h for h in raw if isinstance(h, dict)]
    if isinstance(raw, dict):
        for key in ("hotels", "hotelList", "data", "items", "list"):
            val = raw.get(key)
            if isinstance(val, list):
                return [h for h in val if isinstance(h, dict)]
        if raw.get("hotelId") or raw.get("hotelName") or raw.get("name"):
            return [raw]
    return []


def parse_hotel_price(detail: dict[str, Any]) -> int:
    candidates: list[float] = []

    def _collect(obj: Any) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                kl = k.lower()
                if kl in (
                    "price",
                    "lowestprice",
                    "minprice",
                    "displayprice",
                    "saleprice",
                ):
                    try:
                        candidates.append(float(v))
                    except (TypeError, ValueError):
                        pass
                elif kl in ("roomtypes", "rooms", "roomlist", "rateplans"):
                    if isinstance(v, list):
                        for item in v:
                            _collect(item)
                else:
                    _collect(v)
        elif isinstance(obj, list):
            for item in obj:
                _collect(item)

    _collect(detail)
    if not candidates:
        return 0
    positive = [c for c in candidates if c > 0]
    return int(min(positive)) if positive else 0


def parse_hotel_fields(hotel: dict[str, Any]) -> dict[str, Any]:
    hotel_id = str(
        hotel.get("hotelId") or hotel.get("id") or hotel.get("hotelID") or ""
    )
    name = hotel.get("hotelName") or hotel.get("name") or hotel.get("title") or ""
    address = hotel.get("address") or hotel.get("hotelAddress") or ""
    lat = lng = 0.0
    loc = hotel.get("location") or hotel.get("geo") or {}
    if isinstance(loc, dict):
        lat = float(loc.get("lat") or loc.get("latitude") or 0)
        lng = float(loc.get("lng") or loc.get("longitude") or 0)
    lat = float(hotel.get("lat") or hotel.get("latitude") or lat or 0)
    lng = float(hotel.get("lng") or hotel.get("longitude") or lng or 0)
    price = 0
    for key in ("lowestPrice", "minPrice", "price", "displayPrice", "salePrice"):
        if key in hotel and hotel[key]:
            try:
                price = int(float(hotel[key]))
                break
            except (TypeError, ValueError):
                pass
    star = 0
    for key in ("starRating", "star", "hotelStar"):
        if key in hotel and hotel[key]:
            try:
                star = int(float(hotel[key]))
                break
            except (TypeError, ValueError):
                pass
    return {
        "hotel_id": hotel_id,
        "name": name,
        "address": address,
        "lat": lat,
        "lng": lng,
        "price_per_night": price,
        "star": star,
    }
