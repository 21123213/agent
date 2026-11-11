"""sql_guard 单测：只读、单语句、表白名单、自动 LIMIT。"""
from __future__ import annotations

import unittest

from app.agents.askdata.sql_guard import guard_sql


class TestSqlGuard(unittest.TestCase):
    def test_allow_select_and_limit(self) -> None:
        r = guard_sql("SELECT gmv FROM biz_daily_sales_agg WHERE dt = CURDATE()")
        self.assertTrue(r.ok, r.reason)
        self.assertIn("LIMIT", r.sql.upper())

    def test_reject_delete(self) -> None:
        r = guard_sql("DELETE FROM biz_sales_order WHERE id=1")
        self.assertFalse(r.ok)

    def test_reject_multi_statement(self) -> None:
        r = guard_sql("SELECT 1; DROP TABLE biz_sales_order")
        self.assertFalse(r.ok)

    def test_reject_unknown_table(self) -> None:
        r = guard_sql("SELECT * FROM mysql.user")
        self.assertFalse(r.ok)

    def test_reject_into_outfile(self) -> None:
        r = guard_sql(
            "SELECT * FROM biz_customer INTO OUTFILE '/tmp/x.csv'"
        )
        self.assertFalse(r.ok)

    def test_with_cte_allowed(self) -> None:
        sql = (
            "WITH t AS (SELECT unit_id, SUM(gmv) g FROM biz_daily_sales_agg GROUP BY unit_id) "
            "SELECT * FROM t"
        )
        r = guard_sql(sql)
        self.assertTrue(r.ok, r.reason)


if __name__ == "__main__":
    unittest.main()
