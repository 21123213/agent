"""
Reasoner LangGraph 状态定义。

说明
----
- ``messages``：LangChain 消息列表（含 tool_calls / ToolMessage），供 ReAct + ToolNode。
- ``mode``：``react`` | ``plan_execute``。
- ``skill_ids``：本轮启用的技能；由 resolve + Redis 粘滞合并。
- ``budget``：软/硬 token 闸与工具配额累计。
- ``pending_user_fields``：若判定为 user_required，ask_user 节点 interrupt 前写入。
- 其它字段供 PE / 可观测 / 持久化使用。
"""
from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Optional, Sequence, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class BudgetState(TypedDict, total=False):
    """单次用户提问内的累计成本（不跨下一问，除非 resume 同一次 interrupt）。"""

    prompt_tokens: int
    completion_tokens: int
    web_search_calls: int
    rag_search_calls: int
    soft_nudged: bool


def empty_budget() -> BudgetState:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "web_search_calls": 0,
        "rag_search_calls": 0,
        "soft_nudged": False,
    }


class ReasonerState(TypedDict, total=False):
    """
    图状态。

    ``messages`` 使用 ``add_messages`` reducer：节点返回增量消息即可合并，
    不会整表覆盖（这对 ToolNode 追加 ToolMessage 很重要）。
    跨轮由 ``load_context`` 用 ``RemoveMessage(REMOVE_ALL_MESSAGES)`` 重建干净窗口；
    同轮 interrupt/resume 不经过 load_context，轨迹得以保留。
    """

    messages: Annotated[Sequence[BaseMessage], add_messages]
    mode: Literal["react", "plan_execute"]
    skill_ids: list[str]
    # 强制启用的技能（如 Tab 问数 → ["askdata"]），load_context 会并入 skill_ids
    force_skills: list[str]
    # 会话 / 租户 / 角色（askdata 工具需要 role_id 做列权限）
    session_id: str
    user_id: int
    unit_id: int
    role_id: int
    # 回答档位：quick（快速/baseline/4万）| deep（深度/full/8万）
    answer_mode: Literal["quick", "deep"]
    # 本轮 token 预算覆盖（由 answer_mode 写入；budget 节点读取）
    token_budget: Optional[int]
    # 知识库（RAG Tab / 绑 KB 的会话）；空则不挂 rag_search
    knowledge_base_id: Optional[int]
    file_ids: Optional[list[int]]
    vector_weight: Optional[float]
    bm25_weight: Optional[float]
    # 本轮 rag_search 累积的完整切片（落库 sources）
    rag_chunk_hits: list[dict[str, Any]]
    # 预算
    budget: BudgetState
    # 软闸注入的提示是否已进 system（与 budget.soft_nudged 呼应）
    soft_hint: str
    # 强制收口（硬闸 / 步数用尽）
    force_final: bool
    # PE：结构化计划（JSON 可序列化 dict）
    plan: Optional[dict[str, Any]]
    plan_round: int
    # 每步执行结果（PE 分层 / ReAct 并行后写入），供逐步 review
    # 元素：{id, tool, ok, content, layer, error, input?}
    step_results: list[dict[str, Any]]
    # review 逐步对照表：{id, status, note}
    step_reviews: list[dict[str, Any]]
    # 缺参：仅 user_required 时填充，供 ask_user interrupt
    needs_user_questions: list[dict[str, Any]]
    # 最终答案缓存（finalize 写入，API 读取）
    final_answer: str
    # 运行状态
    run_status: Literal["running", "needs_input", "ok", "error"]
    error: str
    # 步数（ReAct agent 轮次）
    step: int
