"""Reasoner / LangGraph 一次运行的 transcript / trace 双文件落盘。

目录约定（可用 REASONER_ARTIFACTS_DIR 覆盖）::

    logs/reasoner/
      transcript/YYYYMMDD/HHMMSS_<trace12>_<session12>.json
      trace/YYYYMMDD/HHMMSS_<trace12>_<session12>.json

统一外壳 schema=reasoner.run_artifact.v1。
指标 / 延迟分布交给 LangSmith（及后续 OTel→Jaeger），不再落本地 metrics JSON。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

SCHEMA = "reasoner.run_artifact.v1"
KINDS = ("transcript", "trace")


def artifacts_enabled() -> bool:
    return os.getenv("REASONER_ARTIFACTS_ENABLED", "true").lower() == "true"


def default_log_root() -> Path:
    env = (os.getenv("REASONER_ARTIFACTS_DIR") or "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()
    # 与 rag_trace 同级：默认 logs/reasoner（相对 cwd）
    raw = os.getenv("RAG_TRACE_DIR", "logs")
    base = Path(raw)
    if not base.is_absolute():
        base = Path.cwd() / base
    return (base / "reasoner").resolve()


@dataclass
class ArtifactPaths:
    transcript: Path
    trace: Path

    def as_dict(self) -> Dict[str, str]:
        return {
            "transcript": str(self.transcript),
            "trace": str(self.trace),
        }


def _stamp_parts(now: Optional[datetime] = None) -> tuple[str, str]:
    dt = now or datetime.now().astimezone()
    return dt.strftime("%Y%m%d"), dt.strftime("%H%M%S")


def _safe_id(value: str, n: int = 12) -> str:
    text = (value or "unknown").strip() or "unknown"
    return text[:n]


def _envelope(
    *,
    kind: str,
    trace_id: str,
    session_id: str,
    mode: str,
    pipeline: str,
    status: str,
    steps: int,
    error: str,
    user_input: str,
    payload: Any,
    saved_at: datetime,
) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "kind": kind,
        "saved_at": saved_at.isoformat(timespec="seconds"),
        "trace_id": trace_id,
        "session_id": session_id,
        "mode": mode,
        "pipeline": pipeline,
        "status": status,
        "steps": steps,
        "error": error or "",
        "user_input": user_input or "",
        "payload": payload,
    }


def _write_json(path: Path, data: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return path


def _truncate_args(args: Any, max_len: int = 800) -> Any:
    """工具入参截断，避免 transcript 爆炸。"""
    if args is None:
        return None
    if isinstance(args, dict):
        out: Dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str) and len(v) > max_len:
                out[str(k)] = v[:max_len] + "…"
            else:
                out[str(k)] = v
        return out
    text = str(args)
    return text if len(text) <= max_len else text[:max_len] + "…"


def coalesce_react_turns(
    transcript: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把相邻的 ``react_agent`` + ``tools_exec`` 收成一轮 ``react_turn``。

    便于人读：推理决策与工具执行同框；无工具的纯回答步也收成 turn。
    """
    out: List[Dict[str, Any]] = []
    i = 0
    n = len(transcript)
    while i < n:
        cur = transcript[i]
        phase = cur.get("phase")
        nxt = transcript[i + 1] if i + 1 < n else None
        if (
            phase == "react_agent"
            and isinstance(nxt, dict)
            and nxt.get("phase") == "tools_exec"
        ):
            agent_calls = cur.get("tool_calls") or []
            # 兼容旧格式：纯名字列表
            call_by_id: Dict[str, Dict[str, Any]] = {}
            call_list: List[Dict[str, Any]] = []
            if agent_calls and isinstance(agent_calls[0], dict):
                for c in agent_calls:
                    if not isinstance(c, dict):
                        continue
                    item = {
                        "name": c.get("name") or c.get("tool") or "",
                        "id": c.get("id") or "",
                        "args": _truncate_args(c.get("args")),
                    }
                    call_list.append(item)
                    if item["id"]:
                        call_by_id[str(item["id"])] = item
            else:
                for name in agent_calls:
                    call_list.append({"name": str(name), "id": "", "args": None})

            results = list(nxt.get("results") or [])
            tools_out: List[Dict[str, Any]] = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                rid = str(r.get("id") or "")
                base = dict(call_by_id.get(rid) or {})
                if not base.get("name"):
                    base["name"] = r.get("tool") or ""
                if not base.get("id"):
                    base["id"] = rid
                if base.get("args") is None and r.get("args") is not None:
                    base["args"] = _truncate_args(r.get("args"))
                base["ok"] = bool(r.get("ok"))
                base["content"] = r.get("content")
                if r.get("circuit"):
                    base["circuit"] = True
                tools_out.append(base)

            # 有结果但 agent 侧缺 args：用 tools_exec 补齐未匹配项
            if not tools_out and results:
                for r in results:
                    if isinstance(r, dict):
                        tools_out.append(
                            {
                                "name": r.get("tool") or "",
                                "id": r.get("id") or "",
                                "args": _truncate_args(r.get("args")),
                                "ok": bool(r.get("ok")),
                                "content": r.get("content"),
                            }
                        )

            out.append(
                {
                    "phase": "react_turn",
                    "step": cur.get("step"),
                    "node": "react_agent",
                    "purpose": cur.get("purpose"),
                    "provider": cur.get("provider"),
                    "model": cur.get("model"),
                    "force": cur.get("force"),
                    "answer_preview": cur.get("answer_preview") or "",
                    "tools": tools_out,
                    "ok_steps": nxt.get("ok_steps"),
                    "fail_steps": nxt.get("fail_steps"),
                    "failed_tools": nxt.get("failed_tools") or [],
                }
            )
            i += 2
            continue

        if phase == "react_agent":
            # 无工具：仍收成 turn，便于统一浏览
            calls = cur.get("tool_calls") or []
            out.append(
                {
                    "phase": "react_turn",
                    "step": cur.get("step"),
                    "node": "react_agent",
                    "purpose": cur.get("purpose"),
                    "provider": cur.get("provider"),
                    "model": cur.get("model"),
                    "force": cur.get("force"),
                    "answer_preview": cur.get("answer_preview") or "",
                    "tools": [],
                    "tool_calls": calls,
                }
            )
            i += 1
            continue

        out.append(cur)
        i += 1
    return out


def save_run_artifacts(
    *,
    trace_id: str,
    session_id: str = "",
    mode: str = "",
    pipeline: str = "",
    status: str = "ok",
    steps: int = 0,
    error: str = "",
    user_input: str = "",
    transcript: Optional[List[Dict[str, Any]]] = None,
    trace_tree: Any = None,
    log_root: Optional[Path] = None,
) -> Optional[ArtifactPaths]:
    """写出 transcript / trace；关闭时返回 None。"""
    if not artifacts_enabled():
        return None

    root = Path(log_root) if log_root is not None else default_log_root()
    day, hms = _stamp_parts()
    saved_at = datetime.now().astimezone()
    stem = f"{hms}_{_safe_id(trace_id)}_{_safe_id(session_id)}.json"

    transcript_payload: List[Dict[str, Any]] = list(transcript or [])
    if user_input and not (
        transcript_payload and transcript_payload[0].get("phase") == "user"
    ):
        transcript_payload = [
            {"phase": "user", "content": user_input},
            *transcript_payload,
        ]
    # 人读：agent 决策 + 工具结果并成 react_turn
    transcript_payload = coalesce_react_turns(transcript_payload)

    common = dict(
        trace_id=trace_id or "-",
        session_id=session_id or "",
        mode=mode or "",
        pipeline=pipeline or "",
        status=status or "ok",
        steps=int(steps or 0),
        error=error or "",
        user_input=user_input or "",
        saved_at=saved_at,
    )

    try:
        paths = ArtifactPaths(
            transcript=_write_json(
                root / "transcript" / day / stem,
                _envelope(kind="transcript", payload=transcript_payload, **common),
            ),
            trace=_write_json(
                root / "trace" / day / stem,
                _envelope(kind="trace", payload=trace_tree, **common),
            ),
        )
        logger.info(
            "run_artifacts saved transcript=%s trace=%s",
            paths.transcript,
            paths.trace,
        )
        return paths
    except Exception as exc:
        logger.warning("run_artifacts write failed: %s", exc)
        return None
