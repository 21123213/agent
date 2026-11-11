"""
对外错误文案：用户可见 vs 日志完整。

业务可理解的短错误（会话不存在、无权等）原样返回；
LLM / 网络 / SDK 等基础设施错误统一为「联系管理员」，完整细节只进日志。
"""
from __future__ import annotations

import logging
import re
from typing import Any

# 前端 / SSE 统一展示文案
PUBLIC_BUSY_MESSAGE = "系统繁忙，请联系管理员"

# 疑似内部 / 厂商报错，不应直接给用户
_INFRA_HINT = re.compile(
    r"(Error code|Traceback|model_not_found|invalid_request_error|"
    r"APIConnectionError|ConnectTimeout|ReadTimeout|httpx\.|openai\.|"
    r"Connection refused|Max retries|SSLError|RemoteDisconnected|"
    r"ECONNREFUSED|status_code|\"type\":\s*\"invalid)",
    re.IGNORECASE,
)

# 允许透出原文的业务异常
_SAFE_TYPES = (ValueError, PermissionError)


def looks_like_infra_error(message: str) -> bool:
    text = (message or "").strip()
    if not text:
        return True
    if _INFRA_HINT.search(text):
        return True
    if len(text) > 240 and ("{" in text or "\n" in text):
        return True
    return False


def public_error_message(
    exc: BaseException | str | None,
    *,
    request_id: str | None = None,
) -> str:
    """
    返回可给前端展示的短文案。
    - ValueError / PermissionError 且文案不像内部错误 → 截断后原样返回
    - 其它 → PUBLIC_BUSY_MESSAGE（可附 request_id）
    """
    raw = str(exc).strip() if exc is not None else ""
    if isinstance(exc, _SAFE_TYPES) and raw and not looks_like_infra_error(raw):
        msg = raw[:200]
    else:
        msg = PUBLIC_BUSY_MESSAGE

    rid = (request_id or "").strip()
    if rid and msg == PUBLIC_BUSY_MESSAGE:
        return f"{msg}（编号 {rid}）"
    return msg


def log_and_public(
    logger: logging.Logger,
    exc: BaseException,
    *,
    where: str,
    request_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """打完整异常日志（含 stack），返回对外文案。"""
    payload: dict[str, Any] = {
        "where": where,
        "error_type": type(exc).__name__,
        "error": str(exc),
    }
    if request_id:
        payload["request_id"] = request_id
    if extra:
        payload.update(extra)
    logger.exception("%s failed %s", where, payload)
    return public_error_message(exc, request_id=request_id)
