"""搜酒店：道旅 MCP 优先，失败回退高德周边。"""
from __future__ import annotations

import logging
from typing import Any, Literal

from app.agents.reasoner.travel.clients.amap import AmapClient, TIER_KEYWORDS
from app.agents.reasoner.travel.clients.dida_mcp import (
    DidaHotelClient,
    TIER_STAR_INT,
    parse_hotel_fields,
    parse_hotel_price,
)
from app.agents.reasoner.travel.env import env_int, env_str
from app.agents.reasoner.travel.services._common import fail, ok, require

logger = logging.getLogger(__name__)

HotelTier = Literal["luxury", "comfort", "economy"]


def day_anchors(daily_routes: dict[str, Any]) -> list[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    for day in daily_routes.get("days") or []:
        stops = day.get("stops") or []
        chosen: dict[str, Any] | None = None
        for stop in stops:
            if stop.get("type") == "hotel":
                continue
            reason = str(stop.get("reason") or "")
            if reason.startswith("散落"):
                continue
            lat, lng = float(stop.get("lat") or 0), float(stop.get("lng") or 0)
            if lat or lng:
                chosen = stop
                break
        if chosen is None:
            for stop in stops:
                if stop.get("type") == "hotel":
                    continue
                lat, lng = float(stop.get("lat") or 0), float(stop.get("lng") or 0)
                if lat or lng:
                    chosen = stop
                    break
        if chosen is not None:
            anchors.append(
                {
                    "day": day.get("day"),
                    "name": chosen.get("name", ""),
                    "lat": float(chosen.get("lat") or 0),
                    "lng": float(chosen.get("lng") or 0),
                    "reason": chosen.get("reason") or "",
                }
            )
    return anchors


async def search_stay(
    destination: str,
    check_in_date: str,
    stay_nights: int,
    daily_routes: dict[str, Any] | None = None,
    anchors: list[dict[str, Any]] | None = None,
    tier: str = "",
    extra_context: str = "",
) -> dict[str, Any]:
    try:
        _ = extra_context
        require(
            {
                "destination": destination,
                "check_in_date": check_in_date,
                "stay_nights": stay_nights,
            },
            ["destination", "check_in_date", "stay_nights"],
            "search_stay",
        )
        default_tier = env_str("default_hotel_tier", "comfort") or "comfort"
        use_tier: HotelTier = (tier or default_tier)  # type: ignore[assignment]
        if use_tier not in ("luxury", "comfort", "economy"):
            use_tier = "comfort"
        anchor_list = list(anchors or [])
        if not anchor_list and daily_routes:
            anchor_list = day_anchors(daily_routes)
        if not anchor_list:
            return fail("需要 daily_routes 或 anchors", "missing_params")
        primary = anchor_list[0]
        area_name = f"近{primary.get('name') or '日行程起点'}"
        nights = max(int(stay_nights), 1)
        fallback_names = [
            str(a.get("name") or "") for a in anchor_list if a.get("name")
        ]
        hotel: dict[str, Any] | None = None
        source = ""
        dutu = env_str("dutu_api_key")
        if dutu:
            try:
                client = DidaHotelClient()
                star = TIER_STAR_INT[use_tier]
                origin = f"{destination}{TIER_KEYWORDS[use_tier]}"
                plans = [
                    (area_name, "区/县"),
                    (area_name, "景点"),
                    (destination, "城市"),
                ]
                for n in fallback_names[:3]:
                    plans.append((n, "景点"))
                for place, place_type in plans:
                    if not place:
                        continue
                    hotels = await client.search_hotels(
                        place=place,
                        place_type=place_type,
                        origin_query=origin,
                        check_in_date=check_in_date,
                        stay_nights=nights,
                        tier=use_tier,
                        size=3,
                    )
                    if not hotels:
                        continue
                    parsed = parse_hotel_fields(hotels[0])
                    price = parsed.get("price_per_night") or None
                    price_source = "dida" if price else "unavailable"
                    if parsed["hotel_id"]:
                        detail = await client.get_hotel_detail(
                            parsed["hotel_id"],
                            check_in_date=check_in_date,
                            stay_nights=nights,
                        )
                        dp = parse_hotel_price(detail)
                        if dp:
                            price = dp
                            price_source = "dida+detail"
                    if not price:
                        price = None
                        if price_source == "dida":
                            price_source = "unavailable"
                    hotel = {
                        "tier": use_tier,
                        "name": parsed["name"]
                        or f"{place}{TIER_KEYWORDS[use_tier]}",
                        "lat": parsed["lat"],
                        "lng": parsed["lng"],
                        "address": parsed["address"],
                        "area": area_name,
                        "price_per_night": price,
                        "star": parsed["star"] or star,
                        "dida_hotel_id": parsed["hotel_id"],
                        "price_source": price_source,
                    }
                    source = price_source
                    break
            except Exception as exc:
                logger.warning("道旅不可用，回退高德: %s", exc)
        if hotel is None:
            amap = AmapClient()
            radius0 = env_int("stay_hotel_search_radius", 3000)
            lat0 = float(primary.get("lat") or 0)
            lng0 = float(primary.get("lng") or 0)
            attempts: list[tuple[float, float, int, str]] = [
                (lat0, lng0, radius0, area_name),
                (lat0, lng0, max(radius0 * 3, 10000), area_name),
            ]
            city_geo = await amap.geocode(destination, destination)
            if city_geo.get("ok"):
                attempts.append(
                    (
                        float(city_geo["lat"]),
                        float(city_geo["lng"]),
                        max(radius0 * 2, 8000),
                        f"{destination}市区",
                    )
                )
            last_err = "未找到酒店"
            search: dict[str, Any] = {"ok": False}
            for alat, alng, radius, area_label in attempts:
                if not (alat or alng):
                    continue
                search = await amap.search_hotels_near(
                    alat, alng, use_tier, destination, radius=radius
                )
                if search.get("ok"):
                    area_name = area_label
                    break
                last_err = str(search.get("error") or last_err)
            if not search.get("ok"):
                return fail(last_err, "empty_result")
            h = search["hotel"]
            hotel = {
                "tier": use_tier,
                "name": h["name"],
                "lat": h["lat"],
                "lng": h["lng"],
                "address": h.get("address", ""),
                "area": area_name,
                "price_per_night": None,
                "amap_id": h.get("amap_id", ""),
                "star": TIER_STAR_INT[use_tier],
                "price_source": "unavailable",
            }
            source = "amap"
        return ok(
            destination=destination,
            tier=use_tier,
            hotel=hotel,
            day_anchors=anchor_list,
            geo_hotels=[
                {
                    "id": f"hotel_{use_tier}",
                    "name": hotel["name"],
                    "lat": hotel["lat"],
                    "lng": hotel["lng"],
                    "category": "hotel",
                    "address": hotel.get("address", ""),
                }
            ],
            source=source,
        )
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
