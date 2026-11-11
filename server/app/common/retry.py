"""
统一重试封装（同步 + 异步）+ 熔断钩子。

设计要点
--------
1. LangGraph **不会**自动给业务工具做指数退避；本模块是显式策略。
2. 每次 attempt 打结构化日志：``event=retry_attempt`` / ``retry_recovered``，
   字段含 ``attempt``、``max_attempts``、``backoff_ms``、``dependency``、``retry_kind``。
3. metrics：``retry_attempts_total`` / ``retry_recovered_total`` / ``retry_exhausted_total``。
4. 可选熔断：``use_circuit_breaker=True`` 时走 ``CircuitBreaker.before_call/on_*``。
5. **解耦**：熔断只在「整次逻辑调用」结束时计 1 次——成功 ``on_success``；
   重试耗尽或不可重试错误 ``on_failure`` 一次。中间 attempt **不计**熔断失败。

返回值只有最终结果；中间失败只记日志，不抛给调用方（除非耗尽或熔断打开）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from app.common import metrics
from app.common.backoff import BackoffPolicy, compute_backoff_ms
from app.common.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    get_circuit_registry,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# 确定性业务/语法错误：不重试，整次调用只记 1 次熔断失败后抛出
_NON_RETRYABLE_TYPE_NAMES = frozenset(
    {
        "ProgrammingError",
        "DataError",
        "IntegrityError",
        "ValidationError",
        "ValueError",
        "TypeError",
        "KeyError",
        "AttributeError",
        "JSONDecodeError",
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """一次「可重试调用」的策略。"""

    max_attempts: int = 3
    backoff: Optional[BackoffPolicy] = None
    # 哪些异常类型触发重试（其它异常直接抛出）
    retry_on: tuple = (Exception,)
    use_circuit_breaker: bool = True
    # 日志用：transient | repair | 自定义
    retry_kind: str = "transient"


def _default_policy() -> RetryPolicy:
    return RetryPolicy(backoff=BackoffPolicy())


def _is_non_retryable(exc: BaseException) -> bool:
    return type(exc).__name__ in _NON_RETRYABLE_TYPE_NAMES


def call_with_retry(
    dependency: str,
    fn: Callable[[], T],
    policy: Optional[RetryPolicy] = None,
) -> T:
    """同步重试。"""
    policy = policy or _default_policy()
    breaker: Optional[CircuitBreaker] = None
    if policy.use_circuit_breaker:
        breaker = get_circuit_registry().get(dependency)

    last_exc: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        if breaker is not None:
            breaker.before_call()

        t0 = time.perf_counter()
        try:
            result = fn()
            latency_ms = (time.perf_counter() - t0) * 1000
            if breaker is not None:
                breaker.on_success()
            metrics.incr("call_success_total", dependency=dependency)
            metrics.observe_latency(
                "call_latency_ms", latency_ms, dependency=dependency
            )
            if attempt > 1:
                logger.info(
                    "event=retry_recovered dependency=%s attempt=%s "
                    "retry_kind=%s latency_ms=%.1f",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    latency_ms,
                )
                metrics.incr(
                    "retry_recovered_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
            return result
        except CircuitOpenError:
            raise
        except policy.retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            latency_ms = (time.perf_counter() - t0) * 1000
            metrics.incr(
                "retry_attempts_total",
                dependency=dependency,
                attempt=str(attempt),
                kind=policy.retry_kind,
            )
            # 不可重试：整次调用记 1 次失败后抛出
            if _is_non_retryable(exc):
                if breaker is not None:
                    breaker.on_failure()
                logger.error(
                    "event=retry_non_retryable dependency=%s attempt=%s "
                    "retry_kind=%s error_type=%s error=%s",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    type(exc).__name__,
                    exc,
                )
                metrics.incr(
                    "retry_exhausted_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
                raise

            if attempt >= policy.max_attempts:
                # 耗尽：整次逻辑调用才计 1 次熔断失败
                if breaker is not None:
                    breaker.on_failure()
                logger.error(
                    "event=retry_exhausted dependency=%s attempt=%s "
                    "retry_kind=%s error_type=%s error=%s",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    type(exc).__name__,
                    exc,
                )
                metrics.incr(
                    "retry_exhausted_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
                break

            backoff_ms = 0.0
            if policy.backoff is not None:
                backoff_ms = compute_backoff_ms(attempt, policy.backoff)
            logger.warning(
                "event=retry_attempt dependency=%s attempt=%s max_attempts=%s "
                "backoff_ms=%.1f retry_kind=%s error_type=%s error=%s "
                "latency_ms=%.1f",
                dependency,
                attempt,
                policy.max_attempts,
                backoff_ms,
                policy.retry_kind,
                type(exc).__name__,
                exc,
                latency_ms,
            )
            if backoff_ms > 0:
                time.sleep(backoff_ms / 1000.0)

    assert last_exc is not None
    raise last_exc


async def call_with_retry_async(
    dependency: str,
    fn: Callable[[], Awaitable[T]],
    policy: Optional[RetryPolicy] = None,
) -> T:
    """异步重试（reasoner 工具包装主路径）。"""
    policy = policy or _default_policy()
    breaker: Optional[CircuitBreaker] = None
    if policy.use_circuit_breaker:
        breaker = get_circuit_registry().get(dependency)

    last_exc: Optional[BaseException] = None
    for attempt in range(1, policy.max_attempts + 1):
        if breaker is not None:
            breaker.before_call()

        t0 = time.perf_counter()
        try:
            result = await fn()
            latency_ms = (time.perf_counter() - t0) * 1000
            if breaker is not None:
                breaker.on_success()
            metrics.incr("call_success_total", dependency=dependency)
            metrics.observe_latency(
                "call_latency_ms", latency_ms, dependency=dependency
            )
            if attempt > 1:
                logger.info(
                    "event=retry_recovered dependency=%s attempt=%s "
                    "retry_kind=%s latency_ms=%.1f",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    latency_ms,
                )
                metrics.incr(
                    "retry_recovered_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
            return result
        except CircuitOpenError:
            raise
        except policy.retry_on as exc:  # type: ignore[misc]
            last_exc = exc
            latency_ms = (time.perf_counter() - t0) * 1000
            metrics.incr(
                "retry_attempts_total",
                dependency=dependency,
                attempt=str(attempt),
                kind=policy.retry_kind,
            )
            if _is_non_retryable(exc):
                if breaker is not None:
                    breaker.on_failure()
                logger.error(
                    "event=retry_non_retryable dependency=%s attempt=%s "
                    "retry_kind=%s error_type=%s error=%s",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    type(exc).__name__,
                    exc,
                )
                metrics.incr(
                    "retry_exhausted_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
                raise

            if attempt >= policy.max_attempts:
                if breaker is not None:
                    breaker.on_failure()
                logger.error(
                    "event=retry_exhausted dependency=%s attempt=%s "
                    "retry_kind=%s error_type=%s error=%s",
                    dependency,
                    attempt,
                    policy.retry_kind,
                    type(exc).__name__,
                    exc,
                )
                metrics.incr(
                    "retry_exhausted_total",
                    dependency=dependency,
                    kind=policy.retry_kind,
                )
                break

            backoff_ms = 0.0
            if policy.backoff is not None:
                backoff_ms = compute_backoff_ms(attempt, policy.backoff)
            logger.warning(
                "event=retry_attempt dependency=%s attempt=%s max_attempts=%s "
                "backoff_ms=%.1f retry_kind=%s error_type=%s error=%s "
                "latency_ms=%.1f",
                dependency,
                attempt,
                policy.max_attempts,
                backoff_ms,
                policy.retry_kind,
                type(exc).__name__,
                exc,
                latency_ms,
            )
            if backoff_ms > 0:
                await asyncio.sleep(backoff_ms / 1000.0)

    assert last_exc is not None
    raise last_exc
