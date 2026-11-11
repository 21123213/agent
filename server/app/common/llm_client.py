"""
统一 LLM 路由：按 purpose 选模型，失败降级，结构化日志 + metrics。

Agent / pipeline 禁止散落直连厂商 SDK；一律经本模块。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_openai import ChatOpenAI

from app.common import metrics
from app.common.llm_text import normalize_llm_text
from app.common import tracing as rag_trace
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 进程内 ChatOpenAI 复用：避免每次 invoke/stream 都 new 包装对象。
# key = (provider, model, base_url, streaming, temperature, timeout)
_chat_cache: dict[tuple[Any, ...], ChatOpenAI] = {}
_chat_cache_lock = threading.Lock()


class Purpose(str, Enum):
    FAST = "fast"  # 深度 ReAct 中间步（默认 3.7-max，开思考）
    STRONG = "strong"  # 深度主模型别名（与 DEEP 同配，开思考）
    QUICK = "quick"  # 前端快速回答（默认 3.7-flash，关思考）
    DEEP = "deep"  # 深度收口 / Plan 主模型（默认 3.8-max，开思考）
    ENHANCE = "enhance"  # 切片多模态增强
    JUDGE = "judge"  # 评测裁判
    VALIDATE = "validate"  # AskData SQL 校验（默认 qwen3.6-flash，开思考）


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    api_key: str
    base_url: str
    models: dict[Purpose, str]


def purpose_for_answer_mode(answer_mode: str | None) -> Purpose:
    """前端/API 的 quick|deep → LLM purpose。"""
    if (answer_mode or "").strip().lower() == "quick":
        return Purpose.QUICK
    return Purpose.DEEP


def resolve_model_for_purpose(purpose: Purpose | str) -> tuple[str, str]:
    """
    解析当前主 provider 下该 purpose 对应的 (provider, model_id)。
    供 rag_trace / 控制台摘要标注「这一步实际会打哪个模型」，不发起调用。
    """
    purpose_e = Purpose(purpose) if isinstance(purpose, str) else purpose
    registry = _build_registry()
    if not registry:
        return ("", "")
    spec = registry[0]
    return (spec.name, spec.models.get(purpose_e) or "")


def _build_registry() -> list[ProviderSpec]:
    s = get_settings()
    registry: list[ProviderSpec] = []
    if s.dashscope_api_key:
        deep = (
            s.dashscope_deep_model
            or s.dashscope_answer_model
            or s.dashscope_strong_model
            or "qwen3.8-max"
        )
        quick = s.dashscope_quick_model or "qwen3.7-flash"
        fast = s.dashscope_fast_model or "qwen3.7-max"
        registry.append(
            ProviderSpec(
                name="dashscope",
                api_key=s.dashscope_api_key,
                base_url=s.dashscope_compatible_base.rstrip("/"),
                models={
                    Purpose.FAST: fast,
                    Purpose.STRONG: deep,
                    Purpose.QUICK: quick,
                    Purpose.DEEP: deep,
                    Purpose.ENHANCE: s.dashscope_chat_model or "qwen-vl-max",
                    Purpose.JUDGE: s.dashscope_judge_model or fast,
                    Purpose.VALIDATE: (
                        s.dashscope_validate_model
                        or s.dashscope_quick_model
                        or "qwen3.6-flash"
                    ),
                },
            )
        )
    if s.dashscope_api_key:
        # 兼容旧 zhipu 名：实际仍走同一兼容网关；校验优先 dashscope 主条目
        validate = (
            s.dashscope_validate_model
            or s.dashscope_quick_model
            or s.zhipu_validate_model
            or "qwen3.6-flash"
        )
        registry.append(
            ProviderSpec(
                name="zhipu",
                api_key=s.dashscope_api_key,
                base_url=s.dashscope_compatible_base.rstrip("/"),
                models={
                    Purpose.FAST: s.zhipu_fast_model or "glm-4-flash",
                    Purpose.STRONG: s.zhipu_strong_model or "glm-4",
                    Purpose.QUICK: s.zhipu_fast_model or "glm-4-flash",
                    Purpose.DEEP: s.zhipu_strong_model or "glm-4",
                    Purpose.ENHANCE: s.zhipu_enhance_model or "glm-4v",
                    Purpose.JUDGE: s.zhipu_fast_model or "glm-4-flash",
                    Purpose.VALIDATE: validate,
                },
            )
        )
    return registry


def _extra_body_for_purpose(purpose: Purpose) -> dict[str, Any] | None:
    """
    千问兼容网关非标参数。
    - QUICK（快速回答 / qwen3.6-flash）：关思考
    - FAST / DEEP / STRONG（Agent 主路径）：显式开思考
    - VALIDATE（AskData SQL 校验 / 同 flash 系）：显式开思考
    - 其它 purpose 不强制
    """
    if purpose == Purpose.QUICK:
        return {"enable_thinking": False}
    if purpose in (Purpose.FAST, Purpose.DEEP, Purpose.STRONG, Purpose.VALIDATE):
        return {"enable_thinking": True}
    return None


def _cache_key(
    spec: ProviderSpec,
    purpose: Purpose,
    *,
    temperature: float,
    streaming: bool,
    timeout: float,
) -> tuple[Any, ...]:
    extra = _extra_body_for_purpose(purpose)
    return (
        spec.name,
        spec.base_url,
        spec.models[purpose],
        bool(streaming),
        float(temperature),
        float(timeout),
        # api_key 参与 key，轮换密钥时自动失效旧实例
        spec.api_key,
        # 思考开关等会进请求体，必须参与缓存键
        tuple(sorted((extra or {}).items())),
    )


def _make_chat(
    spec: ProviderSpec,
    purpose: Purpose,
    *,
    temperature: float,
    streaming: bool,
    timeout: float,
) -> ChatOpenAI:
    """按配置取或创建进程内复用的 ChatOpenAI（不建连；HTTP 仍在 invoke/stream）。"""
    key = _cache_key(
        spec, purpose, temperature=temperature, streaming=streaming, timeout=timeout
    )
    cached = _chat_cache.get(key)
    if cached is not None:
        return cached
    with _chat_cache_lock:
        cached = _chat_cache.get(key)
        if cached is not None:
            return cached
        model = spec.models[purpose]
        extra_body = _extra_body_for_purpose(purpose)
        kwargs: dict[str, Any] = {
            "model": model,
            "api_key": spec.api_key,
            "base_url": spec.base_url,
            "temperature": temperature,
            "streaming": streaming,
            "timeout": timeout,
            "max_retries": 0,
        }
        if extra_body:
            kwargs["extra_body"] = extra_body
        chat = ChatOpenAI(**kwargs)
        _chat_cache[key] = chat
        return chat


def clear_chat_cache() -> None:
    """测试 / 热更新配置时清空客户端缓存。"""
    with _chat_cache_lock:
        _chat_cache.clear()


def get_chat(
    purpose: Purpose | str,
    *,
    temperature: float = 0.3,
    streaming: bool = False,
    timeout: float | None = None,
    provider: str | None = None,
) -> BaseChatModel:
    """按 purpose 取主模型（不做 invoke；仅构造）。"""
    purpose_e = Purpose(purpose) if isinstance(purpose, str) else purpose
    s = get_settings()
    to = float(timeout if timeout is not None else s.dashscope_timeout_sec)
    registry = _build_registry()
    if not registry:
        raise RuntimeError("未配置任何 LLM provider（需 DASHSCOPE_API_KEY 等）")
    if provider:
        for spec in registry:
            if spec.name == provider:
                return _make_chat(
                    spec, purpose_e, temperature=temperature, streaming=streaming, timeout=to
                )
        raise RuntimeError(f"未知 provider: {provider}")
    return _make_chat(
        registry[0], purpose_e, temperature=temperature, streaming=streaming, timeout=to
    )


def stream_chat(
    messages: Sequence[BaseMessage],
    *,
    purpose: Purpose | str,
    temperature: float = 0.3,
    timeout: float | None = None,
    reason: str = "step",
):
    """
    流式调用：yield 文本增量（已尽量归一化为 str）。
    失败时尝试下一 provider；若全部失败则 raise。
    注：部分兼容网关可能一次吐整段，此时只会 yield 一次。
    """
    purpose_e = Purpose(purpose) if isinstance(purpose, str) else purpose
    s = get_settings()
    to = float(timeout if timeout is not None else s.dashscope_timeout_sec)
    registry = _build_registry()
    if not registry:
        raise RuntimeError("未配置任何 LLM provider")

    errors: list[str] = []
    last_exc: Exception | None = None
    for idx, spec in enumerate(registry):
        call_reason = reason if idx == 0 else "fallback"
        model_id = spec.models[purpose_e]
        t0 = time.perf_counter()
        try:
            chat = _make_chat(
                spec,
                purpose_e,
                temperature=temperature,
                streaming=True,
                timeout=to,
            )
            yielded_any = False
            for chunk in chat.stream(list(messages)):
                piece = normalize_llm_text(
                    chunk.content if hasattr(chunk, "content") else chunk
                )
                if piece:
                    yielded_any = True
                    yield piece
            latency_ms = (time.perf_counter() - t0) * 1000
            _log_call(
                provider=spec.name,
                model=model_id,
                purpose=purpose_e.value,
                reason=call_reason,
                latency_ms=latency_ms,
                ok=True,
            )
            metrics.incr("llm_calls_total", purpose=purpose_e.value, status="ok")
            metrics.observe_latency(
                "llm_latency_ms", latency_ms, purpose=purpose_e.value
            )
            if not yielded_any:
                # 空流：当作失败尝试降级
                raise RuntimeError("empty stream")
            return
        except Exception as exc:
            last_exc = exc
            latency_ms = (time.perf_counter() - t0) * 1000
            errors.append(f"{spec.name}/{model_id}: {exc}")
            _log_call(
                provider=spec.name,
                model=model_id,
                purpose=purpose_e.value,
                reason="error",
                latency_ms=latency_ms,
                ok=False,
                error=str(exc),
            )
            metrics.incr("llm_calls_total", purpose=purpose_e.value, status="error")
            logger.warning(
                "llm_client stream fallback failed provider=%s model=%s: %s",
                spec.name,
                model_id,
                exc,
            )
            continue

    raise RuntimeError("LLM 流式全部失败: " + " | ".join(errors)) from last_exc


def invoke_chat(
    messages: Sequence[BaseMessage],
    *,
    purpose: Purpose | str,
    temperature: float = 0.3,
    timeout: float | None = None,
    reason: str = "step",
    provider: str | None = None,
) -> str:
    """
    调用 LLM：主 provider → 下一 provider 降级。
    ``provider`` 非空时优先该厂商（如 AskData 校验强制智谱），失败再兜底其它。
    返回归一化纯文本；打结构化日志与 metrics。
    """
    purpose_e = Purpose(purpose) if isinstance(purpose, str) else purpose
    s = get_settings()
    to = float(timeout if timeout is not None else s.dashscope_timeout_sec)
    registry = _build_registry()
    if not registry:
        raise RuntimeError("未配置任何 LLM provider")
    if provider:
        prefer = [sp for sp in registry if sp.name == provider]
        rest = [sp for sp in registry if sp.name != provider]
        if not prefer:
            raise RuntimeError(f"未知或未配置 provider: {provider}")
        registry = prefer + rest

    errors: list[str] = []
    last_exc: Exception | None = None
    for idx, spec in enumerate(registry):
        call_reason = reason if idx == 0 else "fallback"
        model_id = spec.models[purpose_e]
        t0 = time.perf_counter()
        try:
            chat = _make_chat(
                spec,
                purpose_e,
                temperature=temperature,
                streaming=False,
                timeout=to,
            )
            with rag_trace.span(
                "llm_invoke",
                purpose=purpose_e.value,
                provider=spec.name,
                model=model_id,
                reason=call_reason,
            ) as data:
                resp = chat.invoke(list(messages))
                text = normalize_llm_text(
                    resp.content if hasattr(resp, "content") else resp
                )
                data["answer_len"] = len(text)
                data["answer_preview"] = rag_trace.truncate_text(text)
            latency_ms = (time.perf_counter() - t0) * 1000
            _log_call(
                provider=spec.name,
                model=model_id,
                purpose=purpose_e.value,
                reason=call_reason,
                latency_ms=latency_ms,
                ok=True,
            )
            metrics.incr("llm_calls_total", purpose=purpose_e.value, status="ok")
            metrics.observe_latency(
                "llm_latency_ms", latency_ms, purpose=purpose_e.value
            )
            return text
        except Exception as exc:
            last_exc = exc
            latency_ms = (time.perf_counter() - t0) * 1000
            errors.append(f"{spec.name}/{model_id}: {exc}")
            _log_call(
                provider=spec.name,
                model=model_id,
                purpose=purpose_e.value,
                reason="error",
                latency_ms=latency_ms,
                ok=False,
                error=str(exc),
            )
            metrics.incr("llm_calls_total", purpose=purpose_e.value, status="error")
            logger.warning(
                "llm_client fallback candidate failed provider=%s model=%s: %s",
                spec.name,
                model_id,
                exc,
            )
            continue

    raise RuntimeError(
        "LLM 全部失败: " + " | ".join(errors)
    ) from last_exc


def _log_call(
    *,
    provider: str,
    model: str,
    purpose: str,
    reason: str,
    latency_ms: float,
    ok: bool,
    error: str = "",
) -> None:
    payload: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "purpose": purpose,
        "reason": reason,
        "latency_ms": round(latency_ms, 2),
        "trace_id": rag_trace.get_request_id(),
        "ok": ok,
    }
    if error:
        payload["error"] = error[:500]
    logger.info("llm_call %s", payload)
    rag_trace.emit(
        "llm_call",
        status="ok" if ok else "error",
        latency_ms=latency_ms,
        data=payload,
        message=error,
        error_type="" if ok else "LLMError",
    )
