"""
按依赖维度的熔断器（对照 ``agent/resilience/circuit_breaker.py`` 迁入 server）。

key 建议
--------
- ``tool:web_search`` / ``tool:get_time``
- ``llm:dashscope`` / ``llm:zhipu``

状态机
------
CLOSED（正常）──连续**逻辑调用**失败达阈值──► OPEN（快速拒绝）
OPEN ──recovery_timeout 到期──► HALF_OPEN（放行探测）
HALF_OPEN ──成功达阈值──► CLOSED；失败──► OPEN

说明：与 ``retry`` 解耦后，一次「完整调用」（含内部多次 attempt）只记 **1** 次
失败；阈值默认读 Settings ``CIRCUIT_FAILURE_THRESHOLD``（env 可配）。

可观测
------
- 日志：打开 / 半开 / 关闭 / 拒绝
- metrics：``circuit_open_total`` / ``circuit_reject_total`` / …
- rag_trace.emit(stage=circuit.*) + 当前 OTel/LangSmith span event
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from app.common import metrics

logger = logging.getLogger(__name__)


class CircuitState(str, Enum):
    CLOSED = "closed"  # 正常放行
    OPEN = "open"  # 快速失败
    HALF_OPEN = "half_open"  # 探测恢复


class CircuitOpenError(RuntimeError):
    """熔断打开时抛出；调用方应降级或把错误写成 ToolMessage，勿静默吞掉。"""

    def __init__(self, dependency: str, *, failures: int = 0) -> None:
        self.dependency = dependency
        self.failures = int(failures or 0)
        super().__init__(f"熔断已打开，拒绝调用: {dependency}")


@dataclass
class CircuitBreakerConfig:
    """单个熔断器的阈值配置（可按 Settings / env 覆盖默认值）。"""

    failure_threshold: int = 5  # 连续逻辑失败多少次后打开
    recovery_timeout_sec: float = 30.0  # open 多久后进入 half_open
    half_open_success_threshold: int = 1  # half_open 成功几次后关闭


def _observe_circuit(
    event: str,
    dependency: str,
    *,
    state: str = "",
    failures: int = 0,
    **extra: Any,
) -> None:
    """打点到 metrics（调用方已做）之外：本地 transcript/trace + OTel/LangSmith span event。"""
    payload = {
        "dependency": dependency,
        "event": event,
        "state": state,
        "failures": failures,
        **{k: v for k, v in extra.items() if v is not None},
    }
    try:
        from app.common import tracing as rag_trace

        rag_trace.emit(
            f"circuit.{event}",
            status="error" if event in ("open", "reject") else "ok",
            data=payload,
            message=f"circuit {event}: {dependency}",
            error_type="CircuitOpen" if event in ("open", "reject") else "",
        )
    except Exception:
        logger.debug("circuit observe emit failed", exc_info=True)

    # OTel：挂到当前 span（若 LangSmith/OTel 已把工具 span 设为 current，会一起看见）
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        if span is not None and span.is_recording():
            attrs = {
                "circuit.dependency": dependency,
                "circuit.event": event,
                "circuit.state": state or "",
                "circuit.failures": failures,
            }
            for k, v in extra.items():
                if isinstance(v, (str, int, float, bool)):
                    attrs[f"circuit.{k}"] = v
            span.add_event(f"circuit.{event}", attributes=attrs)
            if event in ("open", "reject"):
                span.set_attribute("circuit.open", True)
                span.set_attribute("circuit.dependency", dependency)
    except Exception:
        logger.debug("circuit observe otel failed", exc_info=True)


class CircuitBreaker:
    """单个依赖的熔断器（线程安全）。"""

    def __init__(
        self, dependency: str, config: Optional[CircuitBreakerConfig] = None
    ) -> None:
        self.dependency = dependency
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0
        self._half_open_successes = 0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._maybe_transition_to_half_open()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def before_call(self) -> None:
        """调用前检查；若仍为 OPEN 则抛 ``CircuitOpenError``。"""
        half_open_now = False
        reject = False
        fails = 0
        with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._opened_at >= self.config.recovery_timeout_sec:
                    self._state = CircuitState.HALF_OPEN
                    self._half_open_successes = 0
                    half_open_now = True
                    logger.info(
                        "circuit_half_open dependency=%s", self.dependency
                    )
                    metrics.incr(
                        "circuit_half_open_total", dependency=self.dependency
                    )
                else:
                    metrics.incr(
                        "circuit_reject_total", dependency=self.dependency
                    )
                    logger.warning(
                        "circuit_reject dependency=%s state=open failures=%s",
                        self.dependency,
                        self._consecutive_failures,
                    )
                    reject = True
                    fails = self._consecutive_failures
        if half_open_now:
            _observe_circuit(
                "half_open",
                self.dependency,
                state="half_open",
                failures=fails,
            )
        if reject:
            # 在锁外打点，避免 emit 反取锁死锁
            _observe_circuit(
                "reject",
                self.dependency,
                state="open",
                failures=fails,
            )
            raise CircuitOpenError(self.dependency, failures=fails)

    def on_success(self) -> None:
        """一次成功调用后的状态迁移。"""
        closed_now = False
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._half_open_successes += 1
                if (
                    self._half_open_successes
                    >= self.config.half_open_success_threshold
                ):
                    self._to_closed()
                    closed_now = True
            else:
                self._consecutive_failures = 0
        if closed_now:
            _observe_circuit(
                "close",
                self.dependency,
                state="closed",
                failures=0,
            )

    def on_failure(self) -> None:
        """一次**逻辑调用**失败后的状态迁移（内部 retry 耗尽才应调用一次）。"""
        opened_now = False
        failures = 0
        with self._lock:
            self._consecutive_failures += 1
            failures = self._consecutive_failures
            if self._state == CircuitState.HALF_OPEN:
                self._to_open()
                opened_now = True
            elif self._consecutive_failures >= self.config.failure_threshold:
                self._to_open()
                opened_now = True
        if opened_now:
            _observe_circuit(
                "open",
                self.dependency,
                state="open",
                failures=failures,
                threshold=self.config.failure_threshold,
            )

    def _maybe_transition_to_half_open(self) -> None:
        """OPEN 且过了冷却时间 → HALF_OPEN（须持锁；仅供 state 属性探测）。"""
        if self._state != CircuitState.OPEN:
            return
        if time.time() - self._opened_at >= self.config.recovery_timeout_sec:
            self._state = CircuitState.HALF_OPEN
            self._half_open_successes = 0

    def _to_open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.time()
        logger.warning(
            "circuit_open dependency=%s failures=%s",
            self.dependency,
            self._consecutive_failures,
        )
        metrics.incr("circuit_open_total", dependency=self.dependency)

    def _to_closed(self) -> None:
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._half_open_successes = 0
        logger.info("circuit_close dependency=%s", self.dependency)
        metrics.incr("circuit_close_total", dependency=self.dependency)


class CircuitBreakerRegistry:
    """按 dependency 字符串管理多个熔断器（进程内单例注册表）。"""

    def __init__(self, config: Optional[CircuitBreakerConfig] = None) -> None:
        self._config = config or CircuitBreakerConfig()
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    @property
    def config(self) -> CircuitBreakerConfig:
        return self._config

    def reconfigure(self, config: CircuitBreakerConfig) -> None:
        """进程启动时用 Settings 覆盖默认；已创建的 breaker 保留旧 config。"""
        with self._lock:
            self._config = config
            # 清空以便新依赖用新阈值；已打开的旧 breaker 丢弃（重启语义）
            self._breakers.clear()

    def get(self, dependency: str) -> CircuitBreaker:
        with self._lock:
            if dependency not in self._breakers:
                self._breakers[dependency] = CircuitBreaker(
                    dependency, self._config
                )
            return self._breakers[dependency]


_registry_lock = threading.Lock()
_registry: Optional[CircuitBreakerRegistry] = None
_configured_from_settings = False


def configure_circuit_registry_from_settings() -> CircuitBreakerConfig:
    """从 Settings / env 读取阈值并应用到全局注册表。"""
    global _registry, _configured_from_settings
    try:
        from app.settings import get_settings

        s = get_settings()
        cfg = CircuitBreakerConfig(
            failure_threshold=max(1, int(s.circuit_failure_threshold or 5)),
            recovery_timeout_sec=float(s.circuit_recovery_timeout_sec or 30.0),
        )
    except Exception:
        logger.debug("circuit settings load failed; using defaults", exc_info=True)
        cfg = CircuitBreakerConfig()

    with _registry_lock:
        if _registry is None:
            _registry = CircuitBreakerRegistry(cfg)
        else:
            _registry.reconfigure(cfg)
        _configured_from_settings = True
    logger.info(
        "circuit_registry configured failure_threshold=%s recovery_timeout_sec=%s",
        cfg.failure_threshold,
        cfg.recovery_timeout_sec,
    )
    return cfg


def get_circuit_registry() -> CircuitBreakerRegistry:
    """全局注册表；reasoner 工具包装与 llm 重试共用。首次访问时懒加载 Settings。"""
    global _registry
    if _registry is None or not _configured_from_settings:
        configure_circuit_registry_from_settings()
    assert _registry is not None
    return _registry
