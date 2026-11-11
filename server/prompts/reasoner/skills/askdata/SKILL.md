---
name: askdata-query
description: |
  智能问数技能。用户问销售额、GMV、订单量、门店汇总、退款、支付、
  客单价口径等业务数据时启用。通过只读 SQL 工具取数，禁止编造数字。
version: 1.0.0
---

# 智能问数 Skill

你在处理**业务库只读问数**。数字必须来自工具返回的 `rows`，禁止臆造。

## 硬性约束（必守）

- **只读**：不能增删改数据；写操作类请求直接拒绝。
- **权限 / 白名单**：以工具返回为准。若 `denied=true`，只陈述「因权限或白名单无法查询」等事实。
- **禁止**向用户提供绕过权限、改角色、改 unit、改 SQL 钻空子的任何建议。
- 最终回答用白话；需要透明时可附 SQL，但不要堆砌内部工具名说教。

## 工具顺序

| 步骤 | 工具 | 说明 |
|------|------|------|
| 1 | `askdata_route` | 指标 / 口径文档 / NL2SQL / 拒绝写。指标命中则已带 `draft`，可跳过步骤 2 |
| 2 | `askdata_draft_sql` | 仅 `next=draft_sql` 时调用。返回五类：`sqls` / `fields`(表.字段) / `tables` / `order_by` / `limit` |
| 3 | `askdata_probe` | 用 `draft.sqls[0]` 探查；**返回完整行**。无数据 → 按 `clarify_prompt`/黑话问用户；有数据 → 继续 |
| 4 | `askdata_permission_rules` | 取 unit + role 禁止列 + 表白名单规则包 |
| 5 | （你）按 rules + draft 的 order_by/limit 改写**完整 SQL** | 拼权限条件，保留业务筛选与排序条数 |
| 6 | `askdata_validate_and_run` | 代码守卫 + 小模型校验 + 执行；通过后用 `rows` 作答 |

口径文档（`route=metric_doc`）：直接用返回的 `doc_answer` 回复，不必探查。

## 多 SQL

若 `sqls` 有多条（多表内容相加/对比），可对每条 `askdata_probe` / 最终 `validate_and_run`，再汇总陈述。

## 对用户

- 有数据：陈述结果与必要上下文（时间范围、口径）。
- 无数据：先澄清语义（黑话/字段），不要假装有数。
- 被拒：只说受限事实，不给破解路径。
