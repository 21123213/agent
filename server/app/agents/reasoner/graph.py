"""
编译 Reasoner StateGraph。

拓扑
----
START → load_context → route_mode
  ├─ react:
  │     react_agent → (tool_calls? → gate_user → ask_user|tools_exec → react_agent) → finalize
  └─ plan_execute:
        plan → pe_gate → ask_user|tools_exec → review → (replan→plan | finalize)

工具执行
--------
``tools_exec``（``tool_exec.py``）：
- ReAct：同批 tool_calls **分批并行**（``reasoner_max_parallel_tools``）
- PE：按 ``depends_on`` **分层**；同层并行、层间串行 + 上游注入

人工
----
ReAct：``gate_user`` 看 AIMessage.tool_calls
PE：``pe_gate`` 看整张 plan；resume 后若仍有 plan → 回 ``tools_exec``（不重规划）

Checkpointer：Redis；禁止 MemorySaver。
"""
from __future__ import annotations

import logging
from typing import Literal

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph

from app.agents.reasoner import nodes as N
from app.agents.reasoner.checkpoint_redis import get_checkpointer
from app.agents.reasoner.state import ReasonerState
from app.agents.reasoner.tool_exec import tools_exec

logger = logging.getLogger(__name__)

_compiled = None


def _is_pe(state: ReasonerState) -> bool:
    mode = (state.get("mode") or "react").strip().lower()
    return mode in ("plan_execute", "plan", "pe")


def _route_mode(state: ReasonerState) -> Literal["react_agent", "plan"]:
    return "plan" if _is_pe(state) else "react_agent"


def _after_agent(state: ReasonerState) -> Literal["gate_user", "finalize"]:
    if state.get("force_final"):
        return "finalize"
    msgs = list(state.get("messages") or [])
    last = msgs[-1] if msgs else None
    if isinstance(last, AIMessage) and (getattr(last, "tool_calls", None) or []):
        return "gate_user"
    return "finalize"


def _after_gate(state: ReasonerState) -> Literal["ask_user", "tools_exec"]:
    if state.get("needs_user_questions"):
        return "ask_user"
    return "tools_exec"


def _after_pe_gate(state: ReasonerState) -> Literal["ask_user", "tools_exec"]:
    if state.get("needs_user_questions"):
        return "ask_user"
    return "tools_exec"


def _after_ask_user(state: ReasonerState) -> Literal["react_agent", "tools_exec", "plan"]:
    """
    补参后续跑：
    - PE 且已有 plan → 直接 tools_exec（避免无意义重规划）
    - PE 无 plan → plan
    - ReAct → react_agent
    """
    if _is_pe(state):
        if state.get("plan"):
            return "tools_exec"
        return "plan"
    return "react_agent"


def _after_tools(state: ReasonerState) -> Literal["react_agent", "review"]:
    if _is_pe(state):
        return "review"
    return "react_agent"


def _after_review(state: ReasonerState) -> Literal["plan", "finalize"]:
    if state.get("force_final") or state.get("final_answer"):
        return "finalize"
    return "plan"


def build_graph():
    g = StateGraph(ReasonerState)

    g.add_node("load_context", N.load_context)
    g.add_node("react_agent", N.agent_node)
    g.add_node("gate_user", N.gate_user)
    g.add_node("ask_user", N.ask_user)
    g.add_node("tools_exec", tools_exec)
    g.add_node("plan", N.plan_node)
    g.add_node("pe_gate", N.pe_gate)
    g.add_node("review", N.review_node)
    g.add_node("finalize", N.finalize)

    g.add_edge(START, "load_context")
    g.add_conditional_edges(
        "load_context",
        _route_mode,
        {"react_agent": "react_agent", "plan": "plan"},
    )

    # ReAct
    g.add_conditional_edges(
        "react_agent",
        _after_agent,
        {"gate_user": "gate_user", "finalize": "finalize"},
    )
    g.add_conditional_edges(
        "gate_user",
        _after_gate,
        {"ask_user": "ask_user", "tools_exec": "tools_exec"},
    )
    g.add_conditional_edges(
        "ask_user",
        _after_ask_user,
        {
            "react_agent": "react_agent",
            "tools_exec": "tools_exec",
            "plan": "plan",
        },
    )
    g.add_conditional_edges(
        "tools_exec",
        _after_tools,
        {"react_agent": "react_agent", "review": "review"},
    )

    # Plan-Execute：plan → pe_gate → ask_user|tools_exec → review → …
    g.add_edge("plan", "pe_gate")
    g.add_conditional_edges(
        "pe_gate",
        _after_pe_gate,
        {"ask_user": "ask_user", "tools_exec": "tools_exec"},
    )
    g.add_conditional_edges(
        "review",
        _after_review,
        {"plan": "plan", "finalize": "finalize"},
    )

    g.add_edge("finalize", END)
    return g


def get_compiled_graph():
    global _compiled
    if _compiled is None:
        checkpointer = get_checkpointer()
        _compiled = build_graph().compile(checkpointer=checkpointer)
        logger.info(
            "Reasoner graph compiled: tools_exec + pe_gate + step review + Redis ckpt"
        )
    return _compiled


def reset_compiled_graph() -> None:
    global _compiled
    _compiled = None
