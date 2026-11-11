"""地理距离（纯本地，无 IO）。"""
from __future__ import annotations

import math
from typing import Any


def point_dist_km(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lng1 = float(a.get("lat") or 0), float(a.get("lng") or 0)
    lat2, lng2 = float(b.get("lat") or 0), float(b.get("lng") or 0)
    if not lat1 or not lng1 or not lat2 or not lng2:
        return 9999.0
    r = 6371.0
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    x = (
        math.sin(d_lat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(d_lng / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(x))
