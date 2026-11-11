"""
推理 Agent（reasoner）— LangGraph + ToolNode + interrupt + Redis checkpointer。

入口：``agent.astream_reasoner``；HTTP：``/api/reasoner``。
设计文档：``docs/reasoner-agent-plan.md``。
"""

__all__ = ["agent"]
