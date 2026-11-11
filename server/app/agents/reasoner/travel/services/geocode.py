"""批量地名地理编码。"""
from __future__ import annotations

from typing import Any

from app.agents.reasoner.travel.clients.amap import AmapClient
from app.agents.reasoner.travel.services._common import fail, ok, require


async def geocode_places(
    destination: str,
    places: list[dict[str, Any]],
    category: str = "poi",
) -> dict[str, Any]:
    try:
        require(
            {"destination": destination, "places": places},
            ["destination", "places"],
            "geocode_places",
        )
        client = AmapClient()
        out: list[dict[str, Any]] = []
        for item in places:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            item_id = item.get("id") or name
            result = await client.geocode(f"{destination}{name}", destination)
            out.append(
                {
                    "id": str(item_id),
                    "name": name,
                    "lat": float(result.get("lat") or 0),
                    "lng": float(result.get("lng") or 0),
                    "category": category,
                    "area": item.get("area") or "",
                    "address": result.get("address") or "",
                    "geocode_ok": bool(result.get("ok")),
                }
            )
        if not out:
            return fail("places 无有效地名", "invalid_params")
        return ok(destination=destination, category=category, places=out)
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
