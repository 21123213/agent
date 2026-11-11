"""市内接驳规划。"""
from __future__ import annotations

from typing import Any, Literal

from app.agents.reasoner.travel.clients.amap import AmapClient
from app.agents.reasoner.travel.geo import point_dist_km
from app.agents.reasoner.travel.services._common import fail, ok, require

HotelTier = Literal["luxury", "comfort", "economy"]
WALK_THRESHOLD_KM = 0.8
TIER_MODE: dict[HotelTier, str] = {
    "economy": "transit",
    "comfort": "transit",
    "luxury": "driving",
}


def _estimate_segment(
    a: dict[str, Any], b: dict[str, Any], mode: str
) -> dict[str, Any]:
    distance_km = point_dist_km(a, b)
    if mode == "walking" or distance_km <= WALK_THRESHOLD_KM:
        mode, speed, cost = "walking", 4.5, 0.0
    elif mode == "driving":
        speed, cost = 25.0, round(distance_km * 2.5, 1)
    else:
        mode, speed, cost = "transit", 18.0, 4.0
    duration_min = max(5, int((distance_km / speed) * 60))
    labels = {
        "walking": f"步行约 {duration_min} 分钟",
        "transit": f"公交/地铁约 {duration_min} 分钟",
        "driving": f"驾车约 {duration_min} 分钟",
    }
    return {
        "ok": True,
        "mode": mode,
        "duration_min": duration_min,
        "distance_km": round(distance_km, 2),
        "summary": labels[mode],
        "cost_estimate": cost,
        "source": "estimate",
    }


def _coerce_hotel(
    hotel: Any, daily_routes: dict[str, Any]
) -> tuple[dict[str, Any] | None, str]:
    warnings = ""
    if isinstance(hotel, str):
        name = hotel.strip()
        hotel = {"name": name} if name else {}
        if name:
            warnings = "hotel 为名称字符串，已尝试用行程首站坐标接驳"
    if not isinstance(hotel, dict):
        hotel = {}
    lat = float(hotel.get("lat") or 0)
    lng = float(hotel.get("lng") or 0)
    if lat or lng:
        return hotel, warnings
    for day in daily_routes.get("days") or []:
        if not isinstance(day, dict):
            continue
        for stop in day.get("stops") or []:
            if not isinstance(stop, dict):
                continue
            if stop.get("type") == "hotel":
                continue
            reason = str(stop.get("reason") or "")
            if reason.startswith("散落"):
                continue
            slat, slng = float(stop.get("lat") or 0), float(stop.get("lng") or 0)
            if not (slat or slng):
                continue
            fallback = {
                "id": stop.get("id") or "route_anchor",
                "name": hotel.get("name") or stop.get("name") or "行程锚点",
                "lat": slat,
                "lng": slng,
                "address": "",
                "area": f"近{stop.get('name') or '首站'}",
            }
            note = "hotel 缺坐标，已用每日整线首站作为接驳起点"
            return fallback, (warnings + "；" + note).strip("；") if warnings else note
    return None, warnings or "hotel 缺少 lat/lng，且行程中无可用锚点"


async def plan_transport(
    origin: str,
    destination: str,
    daily_routes: dict[str, Any],
    hotel: Any = None,
    tier: str = "comfort",
    extra_context: str = "",
) -> dict[str, Any]:
    try:
        _ = extra_context
        if not isinstance(daily_routes, dict):
            return fail("daily_routes 须为对象（含 days）", "invalid_params")
        require(
            {
                "origin": origin,
                "destination": destination,
                "daily_routes": daily_routes,
            },
            ["origin", "destination", "daily_routes"],
            "plan_transport",
        )
        days = daily_routes.get("days") or []
        if not days:
            return fail("daily_routes.days 为空", "missing_params")
        hotel_obj, hotel_note = _coerce_hotel(hotel, daily_routes)
        if hotel_obj is None:
            return fail(
                hotel_note
                or "缺少酒店坐标；请先成功调用 search_stay 或传入带 lat/lng 的 hotel",
                "upstream_gap",
            )
        use_tier: HotelTier = (tier or "comfort")  # type: ignore[assignment]
        if use_tier not in ("luxury", "comfort", "economy"):
            use_tier = "comfort"
        client = AmapClient()
        hotel_pt = {
            "id": hotel_obj.get("id") or "hotel",
            "name": hotel_obj.get("name") or "酒店",
            "lat": float(hotel_obj.get("lat") or 0),
            "lng": float(hotel_obj.get("lng") or 0),
        }
        segments: list[dict] = []
        daily_summaries: list[str] = []
        used_amap = False
        for day in days:
            if not isinstance(day, dict):
                continue
            stops = day.get("stops") or []
            prev = hotel_pt
            parts: list[str] = []
            for stop in stops:
                if not isinstance(stop, dict):
                    continue
                stop_pt = {
                    "id": stop.get("id", ""),
                    "name": stop.get("name", ""),
                    "lat": float(stop.get("lat") or 0),
                    "lng": float(stop.get("lng") or 0),
                }
                if not (stop_pt["lat"] or stop_pt["lng"]):
                    continue
                preferred = TIER_MODE.get(use_tier, "transit")
                if point_dist_km(prev, stop_pt) <= WALK_THRESHOLD_KM:
                    preferred = "walking"
                result: dict[str, Any] = {"ok": False}
                if preferred == "walking":
                    result = await client.direction_walking(prev, stop_pt)
                elif preferred == "driving":
                    result = await client.direction_driving(prev, stop_pt)
                else:
                    result = await client.direction_transit(
                        prev, stop_pt, destination
                    )
                if result.get("ok"):
                    used_amap = True
                else:
                    result = _estimate_segment(prev, stop_pt, preferred)
                segments.append(
                    {
                        "from_name": str(prev.get("name") or ""),
                        "to_name": str(stop_pt.get("name") or ""),
                        "mode": result.get("mode", preferred),
                        "duration_min": int(result.get("duration_min", 0)),
                        "distance_km": float(result.get("distance_km", 0)),
                        "cost_estimate": float(result.get("cost_estimate", 0)),
                        "summary": str(result.get("summary", "")),
                        "source": str(result.get("source", "amap")),
                    }
                )
                parts.append(
                    f"{prev.get('name')}→{stop_pt.get('name')} {result.get('summary')}"
                )
                prev = stop_pt
            daily_summaries.append(
                f"D{day.get('day', 0)}: "
                + ("；".join(parts[:3]) if parts else "无行程")
            )
        out = ok(
            intercity={
                "mode": "高铁/航班待确认",
                "summary": f"{origin} → {destination}",
                "note": "大交通未接入",
                "source": "placeholder",
            },
            segments=segments,
            daily_summaries=daily_summaries,
            total_commute_min=sum(s["duration_min"] for s in segments),
            total_cost_estimate=round(
                sum(s["cost_estimate"] for s in segments), 1
            ),
            hotel_used=hotel_pt,
            source="amap" if used_amap else "estimate",
        )
        if hotel_note:
            out["warnings"] = [hotel_note]
        return out
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
