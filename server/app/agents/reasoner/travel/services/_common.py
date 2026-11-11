from __future__ import annotations

from typing import Any


def ok(**data: Any) -> dict[str, Any]:
    return {"ok": True, "error": None, "code": "", **data}


def fail(msg: str, code: str = "runtime_error", **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": msg, "code": code, **extra}


def require(payload: dict[str, Any], fields: list[str], tool: str) -> None:
    missing = []
    for name in fields:
        val = payload.get(name)
        if (
            val is None
            or (isinstance(val, str) and not val.strip())
            or val == []
            or val == {}
        ):
            missing.append(name)
    if missing:
        raise ValueError(f"{tool} 缺少: {', '.join(missing)}")
