"""
可观测性：内存埋点（emit / span_tree）+ 可选 OTel / LangSmith；
Reasoner 另有 transcript/trace 产物（与 rag_trace.jsonl 无关）。

``rag_trace.jsonl`` 已停用：不再创建、不再追加。排障请看 OTel / LangSmith。

仍保留
------
- ``emit`` / ``span``：请求内 span 树事件 + OTel span（有 endpoint 则导出）
- Reasoner / AskData：``logs/reasoner/{transcript|trace}/...``（独立开关
  ``REASONER_ARTIFACTS_ENABLED``）

LangSmith：``LANGCHAIN_TRACING_V2`` + ``LANGCHAIN_API_KEY``。
OTel：``OTEL_EXPORTER_OTLP_ENDPOINT`` 有值时导出；无 endpoint 时 span 仍创建（NoOp exporter）。
"""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from app.common import run_artifacts
from app.common import span_tree

logger = logging.getLogger(__name__)

_request_id: ContextVar[Optional[str]] = ContextVar("rag_request_id", default=None)
_session_id: ContextVar[Optional[str]] = ContextVar("rag_session_id", default=None)
_user_id: ContextVar[Optional[int]] = ContextVar("rag_user_id", default=None)
_pipeline: ContextVar[Optional[str]] = ContextVar("rag_pipeline", default=None)
_started_at: ContextVar[Optional[float]] = ContextVar("rag_started_at", default=None)
# 本请求内 emit 缓冲（仅 RAG_TRACE_ENABLED 时用于控制台；不落盘）
_event_buf: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "rag_event_buf", default=None
)
# LangGraph transcript 相位列表
_transcript: ContextVar[Optional[list[dict[str, Any]]]] = ContextVar(
    "rag_transcript", default=None
)
# 运行元数据：mode / user_input / status / steps / error
_run_meta: ContextVar[Optional[dict[str, Any]]] = ContextVar(
    "rag_run_meta", default=None
)
_artifacts_saved: ContextVar[bool] = ContextVar("rag_artifacts_saved", default=False)

_otel_ready = False
_tracer = None
_ARTIFACT_PIPELINES = frozenset({"reasoner", "askdata"})


def _enabled() -> bool:
    """历史开关：曾控制 jsonl/控制台。默认关闭；即使打开也不再写 jsonl。"""
    return os.getenv("RAG_TRACE_ENABLED", "false").lower() == "true"


def _full_mode() -> bool:
    return os.getenv("RAG_TRACE_FULL", "false").lower() == "true"


def full_mode() -> bool:
    return _full_mode()


def _trace_dir() -> Path:
    """供 run_artifacts 等复用；不再用于 rag_trace.jsonl。"""
    raw = os.getenv("RAG_TRACE_DIR", "logs")
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def setup_otel() -> None:
    """可选：有 OTLP endpoint 时初始化 TracerProvider。可重复调用。"""
    global _otel_ready, _tracer
    if _otel_ready:
        return
    endpoint = (os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
    service = (os.getenv("OTEL_SERVICE_NAME") or "agents-server").strip()
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service})
        provider = TracerProvider(resource=resource)
        if endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
                provider.add_span_processor(BatchSpanProcessor(exporter))
                logger.info("OTel OTLP exporter -> %s", endpoint)
            except Exception as exc:
                logger.warning("OTel exporter init failed (埋点仍可用): %s", exc)
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("agents.rag")
        _otel_ready = True
    except Exception as exc:
        logger.debug("OTel SDK unavailable: %s", exc)
        _otel_ready = True
        _tracer = None


def get_tracer():
    if not _otel_ready:
        setup_otel()
    return _tracer


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def get_request_id() -> Optional[str]:
    return _request_id.get()


def set_trace_context(
    request_id: str,
    *,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    pipeline: Optional[str] = None,
) -> None:
    _request_id.set(request_id)
    if user_id is not None:
        _user_id.set(user_id)
    if session_id is not None:
        _session_id.set(session_id)
    if pipeline is not None:
        _pipeline.set(pipeline)
    _started_at.set(time.perf_counter())
    _event_buf.set([])
    _transcript.set([])
    _run_meta.set(
        {
            "mode": "",
            "user_input": "",
            "status": "ok",
            "steps": 0,
            "error": "",
        }
    )
    _artifacts_saved.set(False)
    span_tree.reset_span_tree(trace_id=request_id)


def clear_trace_context() -> None:
    _request_id.set(None)
    _session_id.set(None)
    _user_id.set(None)
    _pipeline.set(None)
    _started_at.set(None)
    _event_buf.set(None)
    _transcript.set(None)
    _run_meta.set(None)
    _artifacts_saved.set(False)
    span_tree.clear_span_tree()


def set_run_meta(**fields: Any) -> None:
    """更新本请求元数据（mode / user_input / status / steps / error）。"""
    meta = _run_meta.get()
    if meta is None:
        return
    for k, v in fields.items():
        if v is None:
            continue
        meta[k] = v


def append_phase(phase: str, **fields: Any) -> None:
    """追加一条 LangGraph transcript 相位（无请求上下文时静默忽略）。"""
    buf = _transcript.get()
    if buf is None:
        return
    item: dict[str, Any] = {"phase": phase}
    for k, v in fields.items():
        if v is not None:
            item[k] = v
    buf.append(item)
    if phase in ("react_agent", "plan"):
        meta = _run_meta.get()
        if meta is not None:
            meta["steps"] = int(meta.get("steps") or 0) + 1


@contextmanager
def rag_request(
    *,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    request_id: Optional[str] = None,
    pipeline: Optional[str] = None,
) -> Iterator[str]:
    rid = request_id or new_request_id()
    set_trace_context(
        rid, user_id=user_id, session_id=session_id, pipeline=pipeline
    )
    pipe = (pipeline or "").strip() or "request"
    root_cm = span_tree.tree_span(
        f"{pipe}.run",
        pipeline=pipe,
        session_id=session_id or "",
    )
    root_cm.__enter__()
    try:
        yield rid
    except Exception as exc:
        set_run_meta(status="error", error=f"{type(exc).__name__}: {exc}")
        try:
            root_cm.__exit__(type(exc), exc, exc.__traceback__)
        except Exception:
            pass
        raise
    else:
        try:
            root_cm.__exit__(None, None, None)
        except Exception:
            pass
    finally:
        # 无论是否显式 finish，退出请求时按 request 聚合落盘
        flush_request_trace()
        clear_trace_context()


def truncate_text(text: Any, max_len: int = 200) -> str:
    s = "" if text is None else str(text)
    if _full_mode():
        return s
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def summarize_chunks(
    chunks: Optional[list[dict[str, Any]]],
    *,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    if not chunks:
        return []
    out: list[dict[str, Any]] = []
    iterable = chunks if limit is None else chunks[:limit]
    for hit in iterable:
        text = hit.get("content") or hit.get("chunk_text") or ""
        meta = hit.get("metadata") or {}
        score = hit.get("rerank_score")
        if score is None:
            score = hit.get("rrf_score") or hit.get("score") or 0
        item: dict[str, Any] = {
            "id": hit.get("id"),
            "file_id": hit.get("file_id") or meta.get("file_id"),
            "chunk_index": (
                hit.get("chunk_index")
                if hit.get("chunk_index") is not None
                else meta.get("chunk_index")
            ),
            "score": float(score),
            "source": hit.get("source") or meta.get("retrieval") or "",
            "preview": truncate_text(text),
        }
        if hit.get("rerank_score") is not None and hit.get("rrf_score") is not None:
            item["rrf_score"] = float(hit["rrf_score"])
            item["rerank_score"] = float(hit["rerank_score"])
        if _full_mode():
            item["content"] = text
        out.append(item)
    return out


def _console_summary(event: dict[str, Any]) -> str:
    """
    终端一行摘要：除 stage/latency 外，尽量带上「在干什么」。

    常见字段（有则打印）：purpose/model、mode、skills、tools、goal、
    decision、ok_steps、tool_calls 等——避免只看到 load_context/agent 空壳。
    """
    stage = event.get("stage", "")
    rid = event.get("request_id", "-")
    status = event.get("status", "ok")
    latency = event.get("latency_ms")
    data = event.get("data") or {}
    parts = [f"[rag] request_id={rid} stage={stage} status={status}"]
    if event.get("pipeline"):
        parts.append(f"pipeline={event['pipeline']}")
    if latency is not None:
        parts.append(f"latency_ms={latency}")
    # —— 可观测关键字段（按出现顺序，避免刷屏）——
    for key in (
        "purpose",
        "model",
        "provider",
        "mode",
        "answer_mode",
        "reason",
    ):
        if data.get(key) not in (None, "", []):
            parts.append(f"{key}={data[key]}")
    if data.get("skills") is not None:
        skills = data["skills"]
        if isinstance(skills, list):
            parts.append(f"skills={','.join(str(x) for x in skills) or '-'}")
        else:
            parts.append(f"skills={skills}")
    if data.get("tool"):
        parts.append(f"tool={data['tool']}")
    if data.get("tools") is not None:
        tools = data["tools"]
        if isinstance(tools, list):
            parts.append(f"tools={','.join(str(x) for x in tools) or '-'}")
    if data.get("failed_tools"):
        ft = data["failed_tools"]
        if isinstance(ft, list) and ft:
            parts.append(f"failed={','.join(str(x) for x in ft)}")
    if data.get("tool_calls") is not None:
        tc = data["tool_calls"]
        if isinstance(tc, list):
            parts.append(f"tool_calls={','.join(str(x) for x in tc) or '0'}")
        else:
            parts.append(f"tool_calls={tc}")
    if data.get("goal"):
        g = str(data["goal"]).replace("\n", " ")
        parts.append(f"goal={g[:60]}{'…' if len(g) > 60 else ''}")
    if data.get("decision"):
        parts.append(f"decision={data['decision']}")
    if data.get("ok_steps") is not None or data.get("fail_steps") is not None:
        parts.append(
            f"steps_ok={data.get('ok_steps', 0)}/{data.get('fail_steps', 0)}fail"
        )
    if "hits" in data:
        parts.append(f"hits={data['hits']}")
    elif "chunks" in data and isinstance(data["chunks"], list):
        parts.append(f"hits={len(data['chunks'])}")
    if data.get("hist_msgs") is not None:
        parts.append(f"hist={data['hist_msgs']}")
    if data.get("mem_hits") is not None:
        parts.append(f"mem={data['mem_hits']}")
    if data.get("step") is not None:
        parts.append(f"step={data['step']}")
    if data.get("layer") is not None:
        parts.append(f"layer={data['layer']}")
    if data.get("count") is not None and stage.startswith("tools"):
        parts.append(f"count={data['count']}")
    if status == "error" and event.get("message"):
        parts.append(f"error={event['message'][:200]}")
    return " ".join(parts)


def _should_save_artifacts() -> bool:
    pipe = (_pipeline.get() or "").strip().lower()
    return pipe in _ARTIFACT_PIPELINES


def _save_reasoner_artifacts() -> None:
    """把 transcript / span 树写成双文件（同请求只写一次）。"""
    if not _should_save_artifacts() or _artifacts_saved.get():
        return
    rid = _request_id.get() or "-"
    meta = _run_meta.get() or {}
    paths = run_artifacts.save_run_artifacts(
        trace_id=rid,
        session_id=_session_id.get() or "",
        mode=str(meta.get("mode") or ""),
        pipeline=_pipeline.get() or "",
        status=str(meta.get("status") or "ok"),
        steps=int(meta.get("steps") or 0),
        error=str(meta.get("error") or ""),
        user_input=str(meta.get("user_input") or ""),
        transcript=list(_transcript.get() or []),
        trace_tree=span_tree.root_span_dict(),
    )
    if paths is not None:
        _artifacts_saved.set(True)


def flush_request_trace() -> None:
    """请求结束：写 reasoner transcript/trace（若适用），清空事件缓冲；不写 jsonl。"""
    try:
        _save_reasoner_artifacts()
    except Exception as exc:
        logger.warning("reasoner artifacts flush failed: %s", exc)

    _event_buf.set([])


def _annotate_span_from_emit(
    stage: str,
    *,
    status: str,
    latency_ms: Optional[float],
    data: dict[str, Any],
) -> None:
    """把关键 emit（llm_call / circuit.*）挂到本地 span 树 events，供 trace JSON 阅读。"""
    cur = span_tree.current_span()
    if cur is None:
        return
    d = data or {}
    if stage == "llm_call":
        cur.add_event(
            "llm_usage",
            model=str(d.get("model") or ""),
            purpose=str(d.get("purpose") or ""),
            latency_ms=latency_ms,
            ok=status == "ok",
        )
        return
    if stage.startswith("circuit."):
        cur.add_event(
            stage,
            dependency=str(d.get("dependency") or ""),
            event=str(d.get("event") or ""),
            state=str(d.get("state") or ""),
            failures=d.get("failures"),
            ok=status == "ok",
        )


def emit(
    stage: str,
    *,
    status: str = "ok",
    latency_ms: Optional[float] = None,
    data: Optional[dict[str, Any]] = None,
    message: str = "",
    error_type: str = "",
) -> None:
    if not _enabled() and _transcript.get() is None:
        return
    payload = data or {}
    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "request_id": _request_id.get() or "-",
        "session_id": _session_id.get(),
        "user_id": _user_id.get(),
        "pipeline": _pipeline.get(),
        "stage": stage,
        "status": status,
        "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        "data": payload,
    }
    if message:
        event["message"] = message
    if error_type:
        event["error_type"] = error_type

    _annotate_span_from_emit(
        stage, status=status, latency_ms=latency_ms, data=payload
    )

    # 可选控制台摘要；永不落盘 rag_trace.jsonl
    if _enabled():
        print(_console_summary(event))
        buf = _event_buf.get()
        if buf is not None:
            buf.append(event)


@contextmanager
def span(stage: str, **extra: Any) -> Iterator[dict[str, Any]]:
    """本地 emit + 请求内 span 树 + 可选 OTel span。"""
    data: dict[str, Any] = dict(extra)
    t0 = time.perf_counter()
    status = "ok"
    message = ""
    error_type = ""
    tracer = get_tracer()
    otel_cm = None
    otel_span = None
    tree_cm = None
    tree_sp = None

    # 仅简单 attrs 进树，避免不可序列化对象
    tree_attrs = {
        k: v
        for k, v in extra.items()
        if isinstance(v, (str, int, float, bool, type(None)))
        or (
            isinstance(v, (list, tuple))
            and all(isinstance(x, (str, int, float, bool)) for x in v)
        )
    }
    if span_tree.root_span() is not None or _transcript.get() is not None:
        try:
            tree_cm = span_tree.tree_span(stage, **tree_attrs)
            tree_sp = tree_cm.__enter__()
        except Exception:
            tree_cm = None
            tree_sp = None

    if tracer is not None:
        try:
            from opentelemetry import trace as otel_trace

            otel_cm = tracer.start_as_current_span(f"rag.{stage}")
            otel_span = otel_cm.__enter__()
            if otel_span is not None:
                otel_span.set_attribute("rag.request_id", _request_id.get() or "")
                if _pipeline.get():
                    otel_span.set_attribute("rag.pipeline", _pipeline.get() or "")
        except Exception:
            otel_cm = None
            otel_span = None
    try:
        yield data
    except Exception as exc:
        status = "error"
        message = str(exc)
        error_type = type(exc).__name__
        data.setdefault("error", True)
        if otel_span is not None:
            try:
                from opentelemetry import trace as otel_trace

                otel_span.set_status(otel_trace.Status(otel_trace.StatusCode.ERROR))
                otel_span.record_exception(exc)
            except Exception:
                pass
        if tree_cm is not None:
            try:
                tree_cm.__exit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            tree_cm = None
        emit(
            stage,
            status=status,
            latency_ms=(time.perf_counter() - t0) * 1000,
            data=data,
            message=message,
            error_type=error_type,
        )
        raise
    else:
        if tree_sp is not None:
            for k, v in data.items():
                if k in tree_sp.attrs:
                    continue
                if isinstance(v, (str, int, float, bool, type(None))):
                    tree_sp.attrs[k] = v
                elif isinstance(v, list) and all(
                    isinstance(x, (str, int, float, bool)) for x in v
                ):
                    tree_sp.attrs[k] = v
        emit(
            stage,
            status=status,
            latency_ms=(time.perf_counter() - t0) * 1000,
            data=data,
            message=message,
            error_type=error_type,
        )
    finally:
        if tree_cm is not None:
            try:
                tree_cm.__exit__(None, None, None)
            except Exception:
                pass
        if otel_cm is not None:
            try:
                otel_cm.__exit__(None, None, None)
            except Exception:
                pass


def finish(status: str = "ok", **data: Any) -> None:
    """写入 request_end 事件；真正落盘仍由 ``rag_request`` finally 的 flush 完成。"""
    started = _started_at.get()
    total_ms = (time.perf_counter() - started) * 1000 if started is not None else None
    message = str(data.pop("message", "") or "")
    error_type = str(data.pop("error_type", "") or "")
    payload = dict(data)
    if total_ms is not None:
        payload["total_latency_ms"] = round(total_ms, 2)
    set_run_meta(status=status, error=message or error_type or "")
    emit(
        "request_end",
        status=status,
        latency_ms=total_ms,
        data=payload,
        message=message,
        error_type=error_type,
    )
    # 显式 finish 时立刻刷盘；_artifacts_saved 防止 finally 重复写双文件
    flush_request_trace()
    if _request_id.get():
        _event_buf.set([])
