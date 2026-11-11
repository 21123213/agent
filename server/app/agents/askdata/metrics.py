"""
指标优先路由：命中 YAML 则走模板 SQL / 口径文档，避免模型自由乱算。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import yaml

_DATA = Path(__file__).resolve().parent / "data" / "metrics.yaml"

# 口径咨询短文（与 docs/askdata-metrics.md 对齐的摘要）
_DOC_SNIPPETS: dict[str, str] = {
    "aov": (
        "客单价 = 销售额(GMV) ÷ 订单量。"
        "GMV 为有效订单（paid/shipped/completed）实付合计（含运费，单位元）；"
        "订单量为 0 时不应计算客单价。详见 docs/askdata-metrics.md。"
    ),
}


@dataclass
class MetricHit:
    """命中结果。"""

    id: str
    name: str
    description: str
    source: str  # view | tables | doc
    sql: Optional[str]
    doc_key: Optional[str] = None

    @property
    def is_doc(self) -> bool:
        return self.source == "doc" or not self.sql


@lru_cache
def _load_raw() -> list[dict[str, Any]]:
    with _DATA.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return list(data.get("metrics") or [])


def reload_metrics() -> None:
    """测试或热更新时清缓存。"""
    _load_raw.cache_clear()


def match_metric(question: str) -> Optional[MetricHit]:
    """
    子串匹配（先精确长词优先：按 match 字符串长度降序）。

    返回第一个命中；无命中返回 None → 交给 NL2SQL。
    """
    q = (question or "").strip().lower()
    if not q:
        return None

    candidates: list[tuple[int, dict[str, Any], str]] = []
    for m in _load_raw():
        for phrase in m.get("match") or []:
            p = str(phrase).lower()
            if p and p in q:
                candidates.append((len(p), m, p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: -x[0])
    m = candidates[0][1]
    sql = m.get("sql")
    if isinstance(sql, str):
        sql = sql.strip() or None
    return MetricHit(
        id=str(m.get("id")),
        name=str(m.get("name") or m.get("id")),
        description=str(m.get("description") or ""),
        source=str(m.get("source") or "view"),
        sql=sql,
        doc_key=m.get("doc_key"),
    )


def doc_answer(doc_key: str) -> str:
    return _DOC_SNIPPETS.get(doc_key, "请参阅 docs/askdata-metrics.md。")
