"""
意图识别（规则版，可测、可解释）。

区分：拒绝写操作 / 口径文档 / 指标查询 / 自由 NL2SQL。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from app.agents.askdata.metrics import MetricHit, match_metric

IntentKind = Literal["refuse_write", "metric_doc", "metric", "nl2sql"]


@dataclass
class Intent:
    kind: IntentKind
    metric: Optional[MetricHit] = None
    reason: str = ""


_WRITE_HINTS = (
    "删除",
    "删掉",
    "清空",
    "更新订单",
    "修改订单",
    "插入",
    "truncate",
    "drop table",
    "update ",
    "delete ",
)


def classify_intent(question: str) -> Intent:
    q = (question or "").strip()
    ql = q.lower()

    for h in _WRITE_HINTS:
        if h in ql or h in q:
            return Intent(
                kind="refuse_write",
                reason="智能问数仅支持只读查询，不能增删改数据。",
            )

    hit = match_metric(q)
    if hit:
        if hit.is_doc:
            return Intent(kind="metric_doc", metric=hit, reason="口径咨询")
        return Intent(kind="metric", metric=hit, reason="命中固化指标")

    return Intent(kind="nl2sql", reason="未命中固化指标，走 NL2SQL")
