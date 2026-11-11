"""请求内嵌套 Span 树（对齐 agent/observability/tracer.py，按 ContextVar 隔离）。"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, Iterator, List, Optional


@dataclass
class Span:
    """一次有起止时间的操作区间。"""

    name: str
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_span_id: Optional[str] = None
    trace_id: str = ""
    start_ms: float = field(default_factory=lambda: time.time() * 1000)
    end_ms: Optional[float] = None
    status: str = "ok"
    attrs: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    children: List["Span"] = field(default_factory=list)

    @property
    def latency_ms(self) -> float:
        end = self.end_ms if self.end_ms is not None else time.time() * 1000
        return max(0.0, end - self.start_ms)

    def add_event(self, name: str, **attrs: Any) -> None:
        self.events.append(
            {
                "name": name,
                "ts_ms": time.time() * 1000,
                "attrs": attrs,
            }
        )

    def end(self, status: str = "ok", **attrs: Any) -> None:
        self.end_ms = time.time() * 1000
        self.status = status
        if attrs:
            self.attrs.update(attrs)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "trace_id": self.trace_id,
            "latency_ms": round(self.latency_ms, 2),
            "status": self.status,
            "attrs": self.attrs,
            "events": self.events,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class _SpanState:
    stack: List[Span] = field(default_factory=list)
    root: Optional[Span] = None
    trace_id: str = ""


_span_state: ContextVar[Optional[_SpanState]] = ContextVar(
    "reasoner_span_state", default=None
)


def reset_span_tree(trace_id: str = "") -> None:
    """请求开始时换一份空树。"""
    _span_state.set(_SpanState(trace_id=trace_id or ""))


def clear_span_tree() -> None:
    _span_state.set(None)


def current_span() -> Optional[Span]:
    state = _span_state.get()
    if not state or not state.stack:
        return None
    return state.stack[-1]


def root_span() -> Optional[Span]:
    state = _span_state.get()
    return state.root if state else None


def root_span_dict() -> Optional[Dict[str, Any]]:
    root = root_span()
    return root.to_dict() if root else None


@contextmanager
def tree_span(
    name: str,
    *,
    parent: Optional[Span] = None,
    use_stack: bool = True,
    **attrs: Any,
) -> Generator[Span, None, None]:
    """创建子 span；异常时标记 status=error。

    use_stack=False：并行工具调用时用，避免兄弟节点误嵌套。
    """
    state = _span_state.get()
    if state is None:
        state = _SpanState()
        _span_state.set(state)

    if parent is None:
        parent = state.stack[-1] if state.stack else None
    sp = Span(
        name=name,
        parent_span_id=parent.span_id if parent else None,
        trace_id=state.trace_id or (parent.trace_id if parent else ""),
        attrs=dict(attrs),
    )
    if parent is None:
        state.root = sp
    else:
        parent.children.append(sp)
    if use_stack:
        state.stack.append(sp)
    try:
        yield sp
        if sp.end_ms is None:
            sp.end(status="ok")
    except Exception as exc:
        sp.end(status="error", error_class=type(exc).__name__, error=str(exc))
        raise
    finally:
        if use_stack and state.stack and state.stack[-1] is sp:
            state.stack.pop()
