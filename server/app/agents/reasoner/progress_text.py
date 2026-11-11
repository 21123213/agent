"""
中间过程可读文案：给 SSE ``progress`` 用，避免把工具原始 JSON 整包砸进气泡。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


def _clip(text: str, n: int = 160) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if len(t) <= n:
        return t
    return t[:n] + "…"


def summarize_rag_observation(raw: str) -> str:
    try:
        data = json.loads(raw)
    except Exception:
        return f"检索结果：{_clip(raw, 280)}"
    if not isinstance(data, dict):
        return f"检索结果：{_clip(str(data), 280)}"
    if data.get("ok") is False:
        return f"检索失败：{data.get('error') or _clip(raw, 160)}"
    hits = int(data.get("hits") or 0)
    q = str(data.get("query") or "").strip()
    lines = [f"检索「{q}」· 命中 {hits} 条" if q else f"检索完成 · 命中 {hits} 条"]
    for c in list(data.get("chunks") or [])[:3]:
        if not isinstance(c, dict):
            continue
        prev = _clip(str(c.get("preview") or ""), 140)
        if prev:
            lines.append(f"- {prev}")
    if hits > 3:
        lines.append(f"…另有 {hits - 3} 条")
    return "\n".join(lines)


def _summarize_web_search(data: dict) -> str:
    results = data.get("results") or data.get("items") or data.get("snippets") or []
    if not isinstance(results, list):
        results = []
    lines = [f"网络搜索完成 · {len(results)} 条"]
    for r in results[:3]:
        if isinstance(r, dict):
            title = str(r.get("title") or r.get("name") or "").strip()
            snip = str(r.get("snippet") or r.get("summary") or r.get("content") or "").strip()
            bit = _clip(title or snip, 120)
        else:
            bit = _clip(str(r), 120)
        if bit:
            lines.append(f"- {bit}")
    if len(results) > 3:
        lines.append(f"…另有 {len(results) - 3} 条")
    return "\n".join(lines)


def _summarize_weather(data: dict) -> str:
    city = str(data.get("city") or data.get("location") or "").strip()
    forecast = data.get("forecast") or data.get("daily") or data.get("weather") or []
    head = f"天气「{city}」" if city else "天气查询"
    if isinstance(forecast, list) and forecast:
        bits = []
        for d in forecast[:4]:
            if not isinstance(d, dict):
                continue
            day = str(d.get("date") or d.get("day") or "")[:10]
            cond = str(d.get("text") or d.get("condition") or d.get("weather") or "")
            lo = d.get("low") or d.get("temp_min") or d.get("night")
            hi = d.get("high") or d.get("temp_max") or d.get("day_temp")
            temp = ""
            if lo is not None and hi is not None:
                temp = f"{lo}~{hi}℃"
            bits.append(_clip(f"{day} {cond} {temp}".strip(), 40))
        if bits:
            return f"{head}：{'；'.join(bits)}"
    return f"{head}完成"


def _summarize_generic(name: str, data: dict) -> str:
    if data.get("ok") is False:
        err = data.get("error") or data.get("message") or data.get("info") or "失败"
        return f"【{name}】失败：{_clip(str(err), 160)}"

    # 常见可读字段优先
    for key in (
        "summary",
        "message",
        "hint",
        "route_summary",
        "itinerary_summary",
        "title",
        "name",
        "address",
        "desc",
        "description",
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return f"【{name}】{_clip(v, 220)}"

    # 列表类：报数量 + 首项名
    for key in ("pois", "hotels", "stays", "steps", "items", "routes", "places"):
        v = data.get(key)
        if isinstance(v, list) and v:
            first = v[0]
            label = ""
            if isinstance(first, dict):
                label = str(
                    first.get("name")
                    or first.get("title")
                    or first.get("poi")
                    or ""
                ).strip()
            extra = f"：{_clip(label, 80)}" if label else ""
            return f"【{name}】完成 · {len(v)} 项{extra}"

    # 最后：挑几个标量字段拼一句
    bits: List[str] = []
    for k, v in list(data.items())[:8]:
        if k in ("ok", "raw", "debug", "_upstream"):
            continue
        if isinstance(v, (str, int, float, bool)) and str(v).strip():
            bits.append(f"{k}={_clip(str(v), 40)}")
        if len(bits) >= 3:
            break
    if bits:
        return f"【{name}】完成 · {' · '.join(bits)}"
    return f"【{name}】完成"


def summarize_tool_observation(name: str, raw: str) -> str:
    """任意工具观察 → 短可读进度（给 UI，不给模型）。"""
    n = (name or "tool").strip() or "tool"
    text = (raw or "").strip()
    if not text:
        return f"【{n}】完成（无输出）"

    if n == "rag_search":
        return summarize_rag_observation(text)

    data: Optional[Dict[str, Any]] = None
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                data = parsed
            elif isinstance(parsed, list):
                return f"【{n}】完成 · {len(parsed)} 项"
        except Exception:
            data = None

    if data is None:
        return f"【{n}】{_clip(text, 220)}"

    if n in ("web_search", "bocha_search", "search_web"):
        return _summarize_web_search(data)
    if n in ("get_weather", "weather"):
        return _summarize_weather(data)
    if "forecast" in data or "daily" in data:
        return _summarize_weather(data)
    if any(k in data for k in ("results", "snippets", "items")) and n.endswith(
        "search"
    ):
        return _summarize_web_search(data)
    return _summarize_generic(n, data)


def emit_custom_progress(text: str, *, node: str = "tools_exec", tool: str = "") -> None:
    """图节点内推 custom → 外层 astream(custom) 转成 SSE progress。"""
    t = (text or "").strip()
    if not t:
        return
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        writer(
            {
                "type": "progress",
                "text": t,
                "node": node,
                "tool": tool or None,
            }
        )
    except Exception:
        # 非图内调用 / 无 writer：静默
        pass
