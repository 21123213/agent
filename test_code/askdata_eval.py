#!/usr/bin/env python
"""
AskData 规则评测（默认不连库、不调 LLM）。

用法（仓库根）::

    set PYTHONPATH=server
    .venv\\Scripts\\python test_code\\askdata_eval.py

可选 ``--with-db``：对指标模板做一次真实 SELECT（需已执行 003 + seed_askdata）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

EVAL_PATH = SERVER / "sql" / "askdata_eval.json"


def _load_cases() -> list[dict]:
    data = json.loads(EVAL_PATH.read_text(encoding="utf-8"))
    return list(data.get("cases") or [])


def run_offline() -> list[tuple[str, bool, str]]:
    from app.agents.askdata.intent import classify_intent
    from app.agents.askdata.metrics import match_metric
    from app.agents.askdata.permissions import (
        apply_metric_unit_placeholders,
        sql_mentions_denied_columns,
    )
    from app.agents.askdata.schema_catalog import all_business_tables
    from app.agents.askdata.sql_guard import guard_sql

    results: list[tuple[str, bool, str]] = []

    for case in _load_cases():
        cid = case["id"]
        exp = case.get("expect") or {}
        try:
            if "synthetic_sql" in case and "guard_ok" in exp:
                r = guard_sql(case["synthetic_sql"])
                ok = r.ok == bool(exp["guard_ok"])
                if ok and exp.get("sql_contains"):
                    for frag in exp["sql_contains"]:
                        if frag.upper() not in r.sql.upper():
                            ok = False
                            break
                results.append((cid, ok, "" if ok else r.reason or "guard mismatch"))
                continue

            if exp.get("column_denied"):
                user = {
                    "id": 0,
                    "unit_id": case.get("unit_id", 1),
                    "role_id": case.get("role_id", 4),
                }
                reason = sql_mentions_denied_columns(case["synthetic_sql"], user)
                ok = reason is not None
                results.append((cid, ok, "" if ok else "expected column deny"))
                continue

            if exp.get("tables_include_any"):
                tables = set(all_business_tables())
                need = set(exp["tables_include_any"])
                ok = need.issubset(tables)
                results.append(
                    (cid, ok, "" if ok else f"tables={sorted(tables)}")
                )
                continue

            q = case.get("question") or ""
            intent = classify_intent(q)
            if exp.get("route") == "refuse":
                ok = intent.kind == "refuse_write"
                results.append((cid, ok, intent.kind))
                continue
            if exp.get("route") == "metric_doc":
                ok = intent.kind == "metric_doc"
                results.append((cid, ok, intent.kind))
                continue
            if exp.get("route") == "metric":
                ok = intent.kind == "metric"
                if ok and exp.get("metric_id"):
                    ok = bool(intent.metric) and intent.metric.id == exp["metric_id"]
                if ok and intent.metric and intent.metric.sql:
                    user = {
                        "id": 0,
                        "unit_id": case.get("unit_id", 1),
                        "role_id": case.get("role_id", 4),
                    }
                    sql, params = apply_metric_unit_placeholders(
                        intent.metric.sql, user
                    )
                    for frag in exp.get("sql_contains") or []:
                        if frag not in sql and frag.upper() not in sql.upper():
                            # unit_id 字面或占位
                            if frag == "unit_id" and "unit_id" not in sql:
                                ok = False
                    for frag in exp.get("sql_not_contains") or []:
                        if frag in sql:
                            ok = False
                    if exp.get("admin_no_unit_filter"):
                        ok = ok and "%(unit_id)s" not in sql and not params
                    if exp.get("different_unit_filter") and case.get("compare_with"):
                        other = case["compare_with"]
                        _, p2 = apply_metric_unit_placeholders(
                            intent.metric.sql,
                            {
                                "id": 0,
                                "unit_id": other["unit_id"],
                                "role_id": other["role_id"],
                            },
                        )
                        ok = ok and params.get("unit_id") != p2.get("unit_id")
                results.append((cid, ok, intent.kind if not ok else "ok"))
                continue

            results.append((cid, False, "unhandled case shape"))
        except Exception as exc:  # noqa: BLE001
            results.append((cid, False, str(exc)))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="AskData eval")
    parser.add_argument(
        "--with-db",
        action="store_true",
        help="额外对今日 GMW 模板执行一次真实查询",
    )
    args = parser.parse_args()

    rows = run_offline()
    failed = 0
    for cid, ok, detail in rows:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {cid} {detail}".rstrip())

    if args.with_db:
        from app.agents.askdata import executor as ask_exec
        from app.agents.askdata.metrics import match_metric
        from app.agents.askdata.permissions import apply_metric_unit_placeholders
        from app.agents.askdata.sql_guard import guard_sql

        hit = match_metric("今日销售额")
        assert hit and hit.sql
        sql, params = apply_metric_unit_placeholders(
            hit.sql, {"id": 4, "unit_id": 1, "role_id": 4}
        )
        g = guard_sql(sql)
        assert g.ok, g.reason
        data = ask_exec.run_select(g.sql, params)
        print("[DB] viewer_east 今日销售额 ->", data)

    print(f"\nSummary: {len(rows) - failed}/{len(rows)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
