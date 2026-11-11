"""指标匹配与意图分类单测（不连库、不调 LLM）。"""
from __future__ import annotations

import unittest

from app.agents.askdata.intent import classify_intent
from app.agents.askdata.metrics import match_metric


class TestMetricsIntent(unittest.TestCase):
    def test_today_gmv(self) -> None:
        hit = match_metric("今日销售额是多少")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit.id, "today_gmv")
        self.assertFalse(hit.is_doc)

    def test_aov_doc(self) -> None:
        hit = match_metric("什么是客单价")
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertTrue(hit.is_doc)

    def test_refuse_write(self) -> None:
        intent = classify_intent("删除所有订单")
        self.assertEqual(intent.kind, "refuse_write")

    def test_metric_route(self) -> None:
        intent = classify_intent("本月按门店销售额")
        self.assertEqual(intent.kind, "metric")
        self.assertIsNotNone(intent.metric)
        assert intent.metric is not None
        self.assertEqual(intent.metric.id, "month_gmv_by_store")

    def test_nl2sql_fallback(self) -> None:
        intent = classify_intent("各品牌已完成退款的订单号列表")
        self.assertEqual(intent.kind, "nl2sql")


if __name__ == "__main__":
    unittest.main()
