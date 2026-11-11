"""进程内简易 metrics（面试可讲；后续可换 Prometheus）。"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from typing import Any


_lock = threading.Lock()
_counters: dict[str, int] = defaultdict(int)
_latency_sum_ms: dict[str, float] = defaultdict(float)
_latency_count: dict[str, int] = defaultdict(int)
_latency_max_ms: dict[str, float] = defaultdict(float)


def incr(name: str, value: int = 1, **labels: Any) -> None:
    key = _key(name, labels)
    with _lock:
        _counters[key] += value


def observe_latency(name: str, latency_ms: float, **labels: Any) -> None:
    key = _key(name, labels)
    with _lock:
        _latency_sum_ms[key] += float(latency_ms)
        _latency_count[key] += 1
        if float(latency_ms) > _latency_max_ms[key]:
            _latency_max_ms[key] = float(latency_ms)


def snapshot() -> dict[str, Any]:
    with _lock:
        latencies: dict[str, Any] = {}
        for key, total in _latency_sum_ms.items():
            n = _latency_count[key] or 1
            latencies[key] = {
                "count": _latency_count[key],
                "avg_ms": round(total / n, 2),
                "max_ms": round(_latency_max_ms[key], 2),
            }
        return {
            "ts": time.time(),
            "counters": dict(_counters),
            "latencies": latencies,
        }


def _key(name: str, labels: dict[str, Any]) -> str:
    if not labels:
        return name
    parts = ",".join(f"{k}={labels[k]}" for k in sorted(labels))
    return f"{name}{{{parts}}}"
