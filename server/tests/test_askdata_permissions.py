"""行权限占位符与列权限单测。"""
from __future__ import annotations

import unittest

from app.agents.askdata.metrics import match_metric
from app.agents.askdata.permissions import (
    apply_metric_unit_placeholders,
    sql_mentions_denied_columns,
)
from app.agents.askdata.schema_catalog import all_business_tables
from app.settings import get_settings


class TestAskdataPermissions(unittest.TestCase):
    def setUp(self) -> None:
        # 确保 admin_role_id=1
        self.assertEqual(get_settings().admin_role_id, 1)

    def test_viewer_no_unit_filter(self) -> None:
        """访客问数可跨全部 unit，不注入 unit 过滤。"""
        hit = match_metric("今日销售额")
        assert hit and hit.sql
        user = {"id": 4, "unit_id": 1, "role_id": 4}
        sql, params = apply_metric_unit_placeholders(hit.sql, user)
        self.assertNotIn("%(unit_id)s", sql)
        self.assertEqual(params, {})

    def test_admin_no_unit_filter(self) -> None:
        hit = match_metric("今日销售额")
        assert hit and hit.sql
        user = {"id": 1, "unit_id": 1, "role_id": 1}
        sql, params = apply_metric_unit_placeholders(hit.sql, user)
        self.assertNotIn("%(unit_id)s", sql)
        self.assertEqual(params, {})

    def test_manager_east_vs_north_params_differ(self) -> None:
        hit = match_metric("今日销售额")
        assert hit and hit.sql
        sql_e, p_e = apply_metric_unit_placeholders(
            hit.sql, {"id": 2, "unit_id": 1, "role_id": 2}
        )
        sql_n, p_n = apply_metric_unit_placeholders(
            hit.sql, {"id": 5, "unit_id": 2, "role_id": 2}
        )
        self.assertEqual(sql_e, sql_n)
        self.assertIn("unit_id = %(unit_id)s", sql_e)
        self.assertEqual(p_e["unit_id"], 1)
        self.assertEqual(p_n["unit_id"], 2)

    def test_viewer_phone_denied(self) -> None:
        user = {"id": 4, "unit_id": 1, "role_id": 4}
        reason = sql_mentions_denied_columns(
            "SELECT name, phone FROM biz_customer", user
        )
        self.assertIsNotNone(reason)

    def test_admin_phone_allowed_by_column_rule(self) -> None:
        user = {"id": 1, "unit_id": 1, "role_id": 1}
        reason = sql_mentions_denied_columns(
            "SELECT name, phone FROM biz_customer", user
        )
        self.assertIsNone(reason)

    def test_all_business_tables_include_core(self) -> None:
        tables = set(all_business_tables())
        # 全量注入：退款/商品/订单等业务表都应在 catalog 中
        for name in (
            "biz_refund",
            "biz_product",
            "biz_sales_order",
            "biz_sales_order_item",
            "biz_daily_sales_agg",
        ):
            self.assertIn(name, tables)


if __name__ == "__main__":
    unittest.main()
