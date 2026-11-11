"""
Reasoner 核心工具（始终注册，不依赖 skill）。

约定
----
- 用 LangChain ``@tool`` / StructuredTool；IO 工具一律 async。
- 每个工具可在 ``USER_REQUIRED_FIELDS`` 声明「只有用户能给的必要字段」；
  缺这些才 interrupt 问人，其它缺口走重试/改参。
- 实际执行包一层 ``call_with_retry_async`` + 熔断（dependency=``tool:<name>``）。
"""
from __future__ import annotations

import ast
import json
import logging
import operator
import time
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, Set

from langchain_core.tools import StructuredTool, tool

from app.agents.reasoner.travel.services import web_search as web_search_svc
from app.common.backoff import BackoffPolicy
from app.common.circuit_breaker import CircuitOpenError
from app.common.retry import RetryPolicy, call_with_retry, call_with_retry_async

logger = logging.getLogger(__name__)

# tool_name → 允许向用户追问的字段白名单（空 = 从不因缺参问人）
USER_REQUIRED_FIELDS: Dict[str, Set[str]] = {
    "get_time": set(),
    "calculator": set(),
    "echo": set(),
    "web_search": set(),
    "rag_search": set(),
}


def user_required_fields_for(tool_name: str) -> Set[str]:
    from app.agents.reasoner.tools.travel import TRAVEL_USER_REQUIRED_FIELDS

    if tool_name in USER_REQUIRED_FIELDS:
        return set(USER_REQUIRED_FIELDS[tool_name])
    return set(TRAVEL_USER_REQUIRED_FIELDS.get(tool_name, set()))


def _retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=3,
        backoff=BackoffPolicy(),
        retry_kind="transient",
        use_circuit_breaker=True,
    )


def _wrap_sync(tool_name: str, fn):
    """同步本地工具：熔断 + 重试。"""

    def wrapped(**kwargs: Any) -> str:
        dep = f"tool:{tool_name}"

        def _call() -> str:
            return fn(**kwargs)

        try:
            return call_with_retry(dep, _call, policy=_retry_policy())
        except CircuitOpenError as exc:
            logger.warning(
                "tool circuit open name=%s failures=%s",
                tool_name,
                getattr(exc, "failures", 0),
            )
            return (
                f"工具暂时不可用（熔断）：{exc.dependency}"
                f"（连续逻辑失败≈{getattr(exc, 'failures', 0)}；"
                "请改用其它工具或稍后重试，勿立即重复调用同一工具）"
            )
        except Exception as exc:
            logger.exception("tool failed name=%s", tool_name)
            return f"工具执行失败：{type(exc).__name__}: {exc}"

    return wrapped


async def _wrap_async(tool_name: str, coro_factory) -> str:
    dep = f"tool:{tool_name}"

    async def _call() -> str:
        raw = await coro_factory()
        if isinstance(raw, str):
            return raw
        return json.dumps(raw, ensure_ascii=False)

    try:
        return await call_with_retry_async(dep, _call, policy=_retry_policy())
    except CircuitOpenError as exc:
        logger.warning(
            "tool circuit open name=%s failures=%s",
            tool_name,
            getattr(exc, "failures", 0),
        )
        return (
            f"工具暂时不可用（熔断）：{exc.dependency}"
            f"（连续逻辑失败≈{getattr(exc, 'failures', 0)}；"
            "请改用其它工具或稍后重试，勿立即重复调用同一工具）"
        )
    except Exception as exc:
        logger.exception("tool failed name=%s", tool_name)
        return f"工具执行失败：{type(exc).__name__}: {exc}"


def _get_time_impl() -> str:
    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now().astimezone()
    return (
        f"utc={now_utc.isoformat()} local={now_local.isoformat()} "
        f"epoch={int(time.time())}"
    )


def _calculator_impl(expression: str) -> str:
    expr = (expression or "").strip()
    if not expr:
        return "expression 不能为空"

    allowed_binops = (
        ast.Add,
        ast.Sub,
        ast.Mult,
        ast.Div,
        ast.FloorDiv,
        ast.Mod,
        ast.Pow,
    )
    allowed_unary = (ast.UAdd, ast.USub)

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, allowed_binops):
            left, right = _eval(node.left), _eval(node.right)
            op_map = {
                ast.Add: operator.add,
                ast.Sub: operator.sub,
                ast.Mult: operator.mul,
                ast.Div: operator.truediv,
                ast.FloorDiv: operator.floordiv,
                ast.Mod: operator.mod,
                ast.Pow: operator.pow,
            }
            return float(op_map[type(node.op)](left, right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, allowed_unary):
            val = _eval(node.operand)
            return float(val if isinstance(node.op, ast.UAdd) else -val)
        raise ValueError("仅支持基本算术表达式")

    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval(tree)
        return str(result)
    except Exception as exc:
        return f"计算失败: {exc}"


def _echo_impl(text: str) -> str:
    return f"echo: {(text or '')[:2000]}"


def build_core_tools() -> list[StructuredTool]:
    @tool("get_time")
    def get_time() -> str:
        """获取当前时间（UTC 与本地）。相对日期场景应先调用本工具锚定「今天」。"""
        return _wrap_sync("get_time", lambda: _get_time_impl())()

    @tool("calculator")
    def calculator(
        expression: Annotated[str, "算术表达式，如 (12+8)*3"],
    ) -> str:
        """计算简单算术表达式（加减乘除与括号）。"""
        return _wrap_sync("calculator", lambda: _calculator_impl(expression))()

    @tool("echo")
    def echo(text: Annotated[str, "要回显的文本"]) -> str:
        """回显文本（调试 / 并行演示）。"""
        return _wrap_sync("echo", lambda: _echo_impl(text))()

    @tool("web_search")
    async def web_search(
        query: Annotated[str, "搜索问题或关键词"],
        freshness: Annotated[
            str, "时效：oneDay/oneWeek/oneMonth/oneYear/noLimit"
        ] = "oneYear",
        count: Annotated[int, "返回条数（服务端会再截断）"] = 0,
    ) -> str:
        """通用网页搜索（博查）。返回已截断短摘要；一次搜准、够用即停。"""
        return await _wrap_async(
            "web_search",
            lambda: web_search_svc.web_search(
                query, freshness=freshness or "oneYear", count=int(count or 0)
            ),
        )

    return [get_time, calculator, echo, web_search]


def build_tools_for_skills(skill_ids: list[str] | None = None) -> list[StructuredTool]:
    """core ∪ skill 附加工具。多 skill 可并存（rag / travel / askdata）。"""
    tools = list(build_core_tools())
    ids = {str(s).strip() for s in (skill_ids or []) if str(s).strip()}
    if "travel" in ids:
        from app.agents.reasoner.tools.travel import build_travel_tools

        tools.extend(build_travel_tools())
    if "rag" in ids:
        from app.agents.reasoner.tools.rag_search import build_rag_tools

        tools.extend(build_rag_tools())
    if "askdata" in ids:
        from app.agents.reasoner.tools.askdata_tools import build_askdata_tools

        tools.extend(build_askdata_tools())
    return tools
