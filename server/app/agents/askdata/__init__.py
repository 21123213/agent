"""
智能问数（askdata）。

- 线上：``agent.astream_askdata`` → Reasoner + skill ``askdata``
- 评测/兼容：``chain.answer_question``
- 库：permissions / sql_guard / executor / schema_catalog / metrics

设计见 docs/askdata-plan.md、docs/askdata-reasoner-skill.md。
HTTP 前缀仍为 /api/askdata（与智能问答 Tab 隔离）。
"""
