"""
指数退避 + 抖动（对照参考 ``agent/resilience/backoff.py``）。

用途
----
供 ``common.retry.call_with_retry*`` 在两次 attempt 之间休眠。
日志 / Span 里会带上本次 ``backoff_ms``，方便在 Jaeger / 本地日志确认「在等退避」。

公式（attempt 从 1 起，表示「第 1 次失败后、准备第 2 次调用前」的等待）::

    raw = base_ms * factor ** (attempt - 1)
    capped = min(raw, max_ms)
    若 jitter：Uniform(0, capped) 全抖动，减轻惊群
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class BackoffPolicy:
    """退避策略参数（不可变，便于作为 RetryPolicy 字段共享）。"""

    # 首次失败后的基准等待（毫秒）
    base_ms: float = 200.0
    # 指数因子；第 n 次约为 base * factor^(n-1)
    factor: float = 2.0
    # 单次等待上限，避免无限放大
    max_ms: float = 8_000.0
    # 是否加随机抖动
    jitter: bool = True


def compute_backoff_ms(attempt: int, policy: BackoffPolicy) -> float:
    """
    根据「即将进行的 attempt 编号」（从 1 开始）计算休眠毫秒。

    例：attempt=1 → 第一次失败后的等待；attempt=2 → 第二次失败后的等待。
    """
    if attempt < 1:
        attempt = 1
    raw = policy.base_ms * (policy.factor ** (attempt - 1))
    capped = min(raw, policy.max_ms)
    if not policy.jitter:
        return float(capped)
    # 全抖动：在 [0, capped] 均匀取样
    return float(random.uniform(0, capped))
