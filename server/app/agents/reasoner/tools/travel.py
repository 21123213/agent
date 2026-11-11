"""
旅游 skill 工具（LangChain StructuredTool，异步实现）。

仅当 skill_ids 含 ``travel`` 时由 ``build_tools_for_skills`` 挂载。
"""
from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Optional

from langchain_core.tools import StructuredTool, tool

from app.agents.reasoner.travel.services import culture as culture_svc
from app.agents.reasoner.travel.services import geocode as geocode_svc
from app.agents.reasoner.travel.services import route as route_svc
from app.agents.reasoner.travel.services import stay as stay_svc
from app.agents.reasoner.travel.services import transport as transport_svc
from app.agents.reasoner.travel.services import weather as weather_svc
from app.common.backoff import BackoffPolicy
from app.common.circuit_breaker import CircuitOpenError
from app.common.retry import RetryPolicy, call_with_retry_async

logger = logging.getLogger(__name__)

# 旅游工具默认不问英文字段；意图靠 SKILL + 模型推断
TRAVEL_USER_REQUIRED_FIELDS: dict[str, set[str]] = {
    "get_weather": set(),
    "get_culture": set(),
    "geocode_places": set(),
    "build_route": set(),
    "search_stay": set(),
    "plan_transport": set(),
}


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=3,
        backoff=BackoffPolicy(),
        retry_kind="transient",
        use_circuit_breaker=True,
    )


def _to_json(data: Any) -> str:
    if isinstance(data, str):
        return data
    return json.dumps(data, ensure_ascii=False)


async def _wrap_async(tool_name: str, coro_factory):
    dep = f"tool:{tool_name}"

    async def _call() -> str:
        raw = await coro_factory()
        return _to_json(raw)

    try:
        return await call_with_retry_async(dep, _call, policy=_retry_policy())
    except CircuitOpenError as exc:
        logger.warning(
            "tool circuit open name=%s failures=%s",
            tool_name,
            getattr(exc, "failures", 0),
        )
        return (
            f"工具暂时不可用（熔断）：{exc.dependency}"
            f"（连续逻辑失败≈{getattr(exc, 'failures', 0)}；"
            "请改用其它工具或稍后重试，勿立即重复调用同一工具）"
        )
    except Exception as exc:
        logger.exception("tool failed name=%s", tool_name)
        return f"工具执行失败：{type(exc).__name__}: {exc}"


def build_travel_tools() -> list[StructuredTool]:
    @tool("get_weather")
    async def get_weather(
        destination: Annotated[str, "城市或目的地"],
        start_date: Annotated[str, "开始日期 YYYY-MM-DD"],
        days: Annotated[int, "预报天数"] = 3,
    ) -> str:
        """查询目的地多日天气预报及是否适合户外。"""
        return await _wrap_async(
            "get_weather",
            lambda: weather_svc.get_weather(destination, start_date, int(days)),
        )

    @tool("get_culture")
    async def get_culture(
        destination: Annotated[str, "目的地"],
        start_date: Annotated[str, "行程开始日期 YYYY-MM-DD"],
        season: Annotated[str, "可选：春/夏/秋/冬"] = "",
    ) -> str:
        """检索目的地闭馆/开放时间与当地活动原始线索（不做工具内强过滤）。"""
        return await _wrap_async(
            "get_culture",
            lambda: culture_svc.get_culture(destination, start_date, season or ""),
        )

    @tool("geocode_places")
    async def geocode_places(
        destination: Annotated[str, "城市"],
        places: Annotated[
            list, "地名列表，项如 {id,name}"
        ],
        category: Annotated[str, "poi 或 dining"] = "poi",
    ) -> str:
        """批量地名转经纬度（高德）。"""
        return await _wrap_async(
            "geocode_places",
            lambda: geocode_svc.geocode_places(
                destination, list(places or []), category or "poi"
            ),
        )

    @tool("build_route")
    async def build_route(
        destination: Annotated[str, "目的地"],
        days: Annotated[int, "行程天数"],
        itinerary_lines: Annotated[
            Optional[list],
            "完整线路；每项 {id,title,stops:[{name,role}]}，role=poi|food",
        ] = None,
        loose_pois: Annotated[
            Optional[list], "不成线的散落点 {id,name}"
        ] = None,
        dining_items: Annotated[Optional[list], "餐饮点"] = None,
        late_start: Annotated[bool, "是否晚出发"] = False,
        extra_context: Annotated[str, "额外上下文"] = "",
    ) -> str:
        """将线路/散落景点排入每日日程并地理编码。itinerary_lines 或 loose_pois 至少一项。"""
        return await _wrap_async(
            "build_route",
            lambda: route_svc.build_route(
                destination,
                int(days),
                itinerary_lines=list(itinerary_lines or []),
                loose_pois=list(loose_pois or []),
                dining_items=list(dining_items or []),
                late_start=bool(late_start),
                extra_context=extra_context or "",
            ),
        )

    @tool("search_stay")
    async def search_stay(
        destination: Annotated[str, "目的地"],
        check_in_date: Annotated[str, "入住日期 YYYY-MM-DD"],
        stay_nights: Annotated[int, "入住晚数"],
        daily_routes: Annotated[
            Optional[dict], "build_route 返回（含 days）"
        ] = None,
        anchors: Annotated[Optional[list], "日锚点列表"] = None,
        tier: Annotated[str, "luxury|comfort|economy"] = "",
        extra_context: Annotated[str, "额外上下文"] = "",
    ) -> str:
        """按行程锚点搜酒店；有 DUTU_API_KEY 时走道旅 MCP，否则高德周边。无报价时 price_per_night 为 null。"""
        return await _wrap_async(
            "search_stay",
            lambda: stay_svc.search_stay(
                destination,
                check_in_date,
                int(stay_nights),
                daily_routes=daily_routes if isinstance(daily_routes, dict) else None,
                anchors=list(anchors or []) if anchors else None,
                tier=tier or "",
                extra_context=extra_context or "",
            ),
        )

    @tool("plan_transport")
    async def plan_transport(
        origin: Annotated[str, "出发城市"],
        destination: Annotated[str, "目的地城市"],
        daily_routes: Annotated[dict, "build_route 返回（含 days）"],
        hotel: Annotated[
            Optional[dict], "search_stay 返回的 hotel 对象（含 lat/lng）"
        ] = None,
        tier: Annotated[str, "luxury|comfort|economy"] = "comfort",
        extra_context: Annotated[str, "额外上下文"] = "",
    ) -> str:
        """酒店到各日景点的市内接驳；城际大交通为占位说明。"""
        return await _wrap_async(
            "plan_transport",
            lambda: transport_svc.plan_transport(
                origin,
                destination,
                daily_routes if isinstance(daily_routes, dict) else {},
                hotel=hotel,
                tier=tier or "comfort",
                extra_context=extra_context or "",
            ),
        )

    return [
        get_weather,
        get_culture,
        geocode_places,
        build_route,
        search_stay,
        plan_transport,
    ]
