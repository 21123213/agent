"""
旅游 / 搜索相关配置：只读进程环境变量，不进 ``app.settings.Settings``。

密钥类（BOCHA / AMAP / QWEATHER / DUTU）严禁写入 Settings 或提交到仓库。
非密钥调参（条数、半径、超时）同样走环境变量，便于本地与部署一致。

说明
----
``pydantic-settings`` 的 ``env_file`` 只会填充 Settings 已声明字段，
未声明的 ``BOCHA_API_KEY`` 等不会自动进 ``os.environ``。
因此本模块对仓库根 ``.env`` 做一次 ``load_dotenv``（不覆盖已有环境变量）。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# server/app/agents/reasoner/travel/env.py → parents[5] = 仓库根
_REPO_ROOT = Path(__file__).resolve().parents[5]
_DOTENV_LOADED = False


def _ensure_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    env_path = _REPO_ROOT / ".env"
    if env_path.is_file():
        # override=False：部署注入的环境变量优先于文件
        load_dotenv(env_path, override=False)
    _DOTENV_LOADED = True


def _s(name: str, default: str = "") -> str:
    _ensure_dotenv()
    return (os.getenv(name) or default).strip()


def _i(name: str, default: int) -> int:
    raw = _s(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    raw = _s(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@lru_cache(maxsize=1)
def travel_env() -> dict[str, object]:
    """快照当前环境（进程内缓存；改 env 后需清缓存或重启）。"""
    return {
        "bocha_api_key": _s("BOCHA_API_KEY"),
        "bocha_api_base": _s("BOCHA_API_BASE", "https://api.bochaai.com").rstrip("/"),
        "amap_key": _s("AMAP_KEY"),
        "amap_api_base": _s("AMAP_API_BASE", "https://restapi.amap.com").rstrip("/"),
        "qweather_key": _s("QWEATHER_KEY"),
        "qweather_host": _s("QWEATHER_HOST").rstrip("/"),
        "dutu_api_key": _s("DUTU_API_KEY") or _s("DOUR_API_KEY"),
        "dida_mcp_url": _s(
            "DIDA_MCP_URL", "https://mcp.rollinggo.cn/mcp"
        ).rstrip("/"),
        "stay_hotel_search_radius": _i("STAY_HOTEL_SEARCH_RADIUS", 3000),
        "default_hotel_tier": _s("DEFAULT_HOTEL_TIER", "comfort") or "comfort",
        "http_timeout": _f("TRAVEL_HTTP_TIMEOUT", 20.0),
        "web_search_default_count": _i("AGENT_WEB_SEARCH_DEFAULT_COUNT", 5),
        "web_search_snippet_max": _i("AGENT_WEB_SEARCH_SNIPPET_MAX", 5),
        "web_search_summary_chars": _i("AGENT_WEB_SEARCH_SUMMARY_CHARS", 500),
    }


def clear_travel_env_cache() -> None:
    travel_env.cache_clear()


def env_str(key: str, default: str = "") -> str:
    val = travel_env().get(key, default)
    return str(val if val is not None else default)


def env_int(key: str, default: int = 0) -> int:
    val = travel_env().get(key, default)
    try:
        return int(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float = 0.0) -> float:
    val = travel_env().get(key, default)
    try:
        return float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
