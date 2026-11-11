"""把线路/散落点编成每日行程（含地理编码）。"""
from __future__ import annotations

from typing import Any

from app.agents.reasoner.travel.services._common import fail, ok, require
from app.agents.reasoner.travel.services.geocode import geocode_places


def _normalize_itinerary_lines(lines: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict):
            continue
        row = dict(ln)
        if not str(row.get("title") or "").strip() and row.get("name"):
            row["title"] = str(row.get("name") or "")
        if not str(row.get("id") or "").strip():
            row["id"] = f"line_{i + 1}"
        stops_in = row.get("stops")
        if not stops_in:
            stops_in = row.get("points") or row.get("pois") or []
        stops_out: list[dict] = []
        for st in stops_in or []:
            if isinstance(st, str):
                name = st.strip()
                if name:
                    stops_out.append({"name": name, "role": "poi", "note": ""})
            elif isinstance(st, dict):
                name = str(st.get("name") or "").strip()
                if name:
                    stops_out.append(
                        {
                            "name": name,
                            "role": str(st.get("role") or "poi"),
                            "note": str(st.get("note") or ""),
                        }
                    )
        row["stops"] = stops_out
        row.pop("points", None)
        row.pop("pois", None)
        out.append(row)
    return out


def _line_stop_count(lines: list[dict]) -> int:
    return sum(len(ln.get("stops") or []) for ln in lines)


def _collect_route_names(
    lines: list[dict],
    loose: list[dict],
    dining: list[dict],
) -> tuple[list[dict], list[dict]]:
    pois: list[dict] = []
    dinings: list[dict] = []
    seen: set[str] = set()
    for ln in lines:
        for st in ln.get("stops") or []:
            name = (st.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            if (st.get("role") or "poi") == "food":
                dinings.append({"id": f"food_{name}", "name": name})
            else:
                pois.append({"id": f"poi_{name}", "name": name})
    for p in loose:
        name = (p.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            pois.append({"id": p.get("id") or f"poi_{name}", "name": name})
    for d in dining:
        name = (d.get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            dinings.append({"id": d.get("id") or f"food_{name}", "name": name})
    return pois, dinings


def _index(points: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {p.get("name", ""): p for p in points if p.get("name")}


async def build_route(
    destination: str,
    days: int,
    itinerary_lines: list[dict[str, Any]] | None = None,
    loose_pois: list[dict[str, Any]] | None = None,
    dining_items: list[dict[str, Any]] | None = None,
    late_start: bool = False,
    extra_context: str = "",
) -> dict[str, Any]:
    try:
        require(
            {"destination": destination, "days": days},
            ["destination", "days"],
            "build_route",
        )
        lines = _normalize_itinerary_lines(list(itinerary_lines or []))
        loose = list(loose_pois or [])
        dining = list(dining_items or [])
        if not lines and not loose:
            return fail(
                "需要 itinerary_lines 或 loose_pois",
                "missing_params",
            )
        if lines and _line_stop_count(lines) == 0 and not loose:
            return fail(
                "itinerary_lines 未识别到 stops（请用 stops:[{name,role}]）",
                "invalid_params",
            )
        warnings: list[str] = []
        if lines and _line_stop_count(lines) == 0 and loose:
            warnings.append(
                "itinerary_lines 无有效 stops，仅使用 loose_pois 补位"
            )
        days_n = max(int(days), 1)
        use_late = late_start or any(
            k in (extra_context or "") for k in ("不要早起", "不早起", "晚出发")
        )
        poi_names, dining_names = _collect_route_names(lines, loose, dining)
        geo_poi_list: list[dict] = []
        geo_dining_list: list[dict] = []
        if poi_names:
            g = await geocode_places(destination, poi_names, "poi")
            if not g.get("ok"):
                return g
            geo_poi_list = g.get("places") or []
        if dining_names:
            g = await geocode_places(destination, dining_names, "dining")
            if not g.get("ok"):
                return g
            geo_dining_list = g.get("places") or []
        geo_poi, geo_dining = _index(geo_poi_list), _index(geo_dining_list)
        by_heat = sorted(
            lines, key=lambda x: float(x.get("heat") or 0), reverse=True
        )
        queue = list(by_heat)
        used: set[str] = set()
        day_plans: list[dict] = []
        for day_i in range(1, days_n + 1):
            stops: list[dict] = []
            summary_parts: list[str] = []
            line_stop_count = 0
            if queue:
                ln = queue.pop(0)
                summary_parts.append(ln.get("title") or ln.get("id") or "线路")
                slots = ["am", "pm", "eve"]
                si = 0
                for st in ln.get("stops") or []:
                    name = (st.get("name") or "").strip()
                    if not name:
                        continue
                    role = st.get("role") or "poi"
                    if role != "food" and name in used:
                        continue
                    if role != "food":
                        used.add(name)
                    g = geo_dining.get(name) if role == "food" else geo_poi.get(name)
                    g = g or {}
                    slot = "lunch" if role == "food" else slots[min(si, len(slots) - 1)]
                    if role != "food":
                        si += 1
                        line_stop_count += 1
                    stops.append(
                        {
                            "id": str(g.get("id") or name),
                            "name": name,
                            "type": "dining" if role == "food" else "poi",
                            "slot": slot,
                            "lat": float(g.get("lat") or 0),
                            "lng": float(g.get("lng") or 0),
                            "duration_min": 60 if role == "food" else 120,
                            "reason": f"整线:{ln.get('id', '')}",
                        }
                    )
            allow_loose_only = line_stop_count == 0
            if line_stop_count > 0 or allow_loose_only:
                for p in loose:
                    name = (p.get("name") or "").strip()
                    if not name or name in used:
                        continue
                    used.add(name)
                    g = geo_poi.get(name) or {}
                    stops.append(
                        {
                            "id": str(g.get("id") or name),
                            "name": name,
                            "type": "poi",
                            "slot": "eve" if line_stop_count > 0 else "pm",
                            "lat": float(g.get("lat") or 0),
                            "lng": float(g.get("lng") or 0),
                            "duration_min": 90,
                            "reason": "散落补位",
                        }
                    )
                    summary_parts.append(f"+{name}")
                    break
            day_plans.append(
                {
                    "day": day_i,
                    "start_time": "10:00" if use_late else "09:30",
                    "stops": stops,
                    "summary": "；".join(summary_parts) or f"D{day_i}",
                }
            )
        unused = [str(ln.get("id") or "") for ln in queue if ln.get("id")]
        result = ok(
            days=day_plans,
            geo_poi=geo_poi_list,
            geo_dining=geo_dining_list,
            unused_line_ids=unused,
            source="lines+geocode",
        )
        if warnings:
            result["warnings"] = warnings
        return result
    except ValueError as exc:
        return fail(str(exc), "missing_params")
    except Exception as exc:
        return fail(str(exc))
