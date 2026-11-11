"""
会话短记忆：ConversationSummary 风格。

规则（与 docs/rag-chat-memory-plan.md 对齐）
------------------------------------------
1. 热路径：Redis ``session:{sid}:context``（TTL 见 Settings.context_cache_ttl）。
2. 窗口：最近 ``max_context_turns`` 轮（默认 3，即最多 6 条 user/assistant）**全文**。
3. 更早对话：压成一条 ``role=system``、正文以 ``[此前对话摘要]`` 开头。
4. 总 token 仍超限：硬截断（LLM 摘要失败时同样回退）。
5. 写 Redis 时调用 ``enforce_memory_limits``；MySQL 仍存全量，不受此压缩影响。
6. 压缩 LLM 超时约 30s；失败则硬截断。调用方若在 async 路径须 ``to_thread``，
   避免同步 ``invoke_chat`` 堵死 uvicorn 事件循环（表现为整站接口无响应）。

Prompt 注入顺序由 chain / agent 负责：长期记忆 → 本窗口（摘要+近 3 轮）→ 本轮问题+文档。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import Purpose, invoke_chat
from app.settings import get_settings

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = "[此前对话摘要]"


def max_context_turns() -> int:
    return int(get_settings().max_context_turns)


def max_context_tokens() -> int:
    return int(get_settings().max_context_tokens)


def estimate_tokens(text: str) -> int:
    """粗估 token：CJK≈1/字，其它约 4 字符≈1；偏保守。"""
    if not text:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cjk
    return cjk + (max(1, (other + 3) // 4) if other else 0)


def estimate_messages_tokens(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        total += estimate_tokens(str(msg.get("content") or ""))
        total += 4  # 角色/分隔开销
    return total


def _split_summary_and_dialogue(
    messages: List[Dict[str, Any]],
) -> tuple[str | None, List[Dict[str, Any]]]:
    """抽出已有摘要正文（无前缀）与对话消息列表。"""
    summary: str | None = None
    dialogue: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        content = str(msg.get("content") or "")
        if role == "system" and content.startswith(SUMMARY_PREFIX):
            summary = content[len(SUMMARY_PREFIX) :].lstrip("\n")
            continue
        if role in ("user", "assistant", "system"):
            dialogue.append(dict(msg))
    return summary, dialogue


def trim_to_turns(
    messages: List[Dict[str, Any]], turns: int | None = None
) -> List[Dict[str, Any]]:
    """
    保留已有摘要（若有）+ 最近 turns 轮对话。
    若对话超过 turns*2，把更早部分交给 compress 做成摘要（本函数只截对话条数时丢弃更早——
    正式路径请走 enforce_memory_limits，会先摘要再截）。
    """
    turns = turns if turns is not None else max_context_turns()
    existing_summary, dialogue = _split_summary_and_dialogue(messages)
    max_msgs = max(1, turns) * 2
    kept = dialogue[-max_msgs:] if len(dialogue) > max_msgs else list(dialogue)
    out: List[Dict[str, Any]] = []
    if existing_summary:
        out.append(
            {
                "role": "system",
                "content": f"{SUMMARY_PREFIX}\n{existing_summary}",
                "compressed": True,
            }
        )
    out.extend(kept)
    return out


def _hard_truncate_messages(
    messages: List[Dict[str, Any]], max_tokens: int
) -> List[Dict[str, Any]]:
    result = [dict(m) for m in messages]
    while result and estimate_messages_tokens(result) > max_tokens:
        first = result[0]
        content = str(first.get("content") or "")
        overflow = estimate_messages_tokens(result) - max_tokens
        if overflow <= 0:
            break
        cut = max(50, overflow + 50)
        if len(content) <= cut:
            result.pop(0)
            continue
        first = dict(first)
        first["content"] = content[: max(0, len(content) - cut)] + "…"
        first["truncated"] = True
        result[0] = first
    return result


# 会话压缩走热路径；调用方须 to_thread。超时短于主问答 90s，失败则硬截断。
_COMPRESS_TIMEOUT_SEC = 30.0


def _summarize_block(older_block: str, char_budget: int) -> str:
    """FAST 模型压缩对话；失败抛异常由调用方硬截断。"""
    system = SystemMessage(
        content=(
            "你是对话记忆压缩助手。将多轮问答压缩为简洁中文摘要，"
            "保留问题要点、关键事实与结论，去掉重复与寒暄。"
            "只输出摘要正文，不要标题或解释。"
        )
    )
    human = HumanMessage(
        content=f"请将以下对话压缩到约 {char_budget} 字以内：\n\n{older_block}"
    )
    summary = invoke_chat(
        [system, human],
        purpose=Purpose.FAST,
        temperature=0.1,
        reason="step",
        timeout=_COMPRESS_TIMEOUT_SEC,
    ).strip()
    if not summary:
        raise ValueError("empty summary")
    return summary


def compress_older_turns(
    messages: List[Dict[str, Any]],
    *,
    turns: int | None = None,
    max_tokens: int | None = None,
) -> List[Dict[str, Any]]:
    """
    将「超出最近 turns 轮」的对话压进一条 [此前对话摘要]，保留近 turns 轮全文。
    已有摘要会与更早对话一并再压缩。
    """
    turns = turns if turns is not None else max_context_turns()
    max_tokens = max_tokens if max_tokens is not None else max_context_tokens()
    existing_summary, dialogue = _split_summary_and_dialogue(messages)
    max_msgs = max(1, turns) * 2

    if len(dialogue) <= max_msgs and not existing_summary:
        # 无需摘要；若仍超 token 再硬截
        if estimate_messages_tokens(dialogue) <= max_tokens:
            return list(dialogue)
        return _hard_truncate_messages(dialogue, max_tokens)

    if len(dialogue) <= max_msgs:
        # 仅有旧摘要 + 近 N 轮：检查 token
        out = trim_to_turns(messages, turns)
        if estimate_messages_tokens(out) <= max_tokens:
            return out
        return _hard_truncate_messages(out, max_tokens)

    older = dialogue[:-max_msgs]
    latest = dialogue[-max_msgs:]
    lines: List[str] = []
    if existing_summary:
        lines.append(f"旧摘要: {existing_summary}")
    for msg in older:
        lines.append(f"{msg.get('role', 'user')}: {msg.get('content') or ''}")
    older_block = "\n".join(lines)

    latest_tokens = estimate_messages_tokens(latest)
    summary_budget = max(200, max_tokens - latest_tokens - 100)
    summary_char_budget = max(200, summary_budget)

    try:
        summary = _summarize_block(older_block, summary_char_budget)
        if estimate_tokens(summary) > summary_budget:
            while estimate_tokens(summary) > summary_budget and len(summary) > 20:
                summary = summary[: int(len(summary) * 0.8)] + "…"
        compressed: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": f"{SUMMARY_PREFIX}\n{summary}",
                "compressed": True,
            },
            *latest,
        ]
        if estimate_messages_tokens(compressed) > max_tokens:
            return _hard_truncate_messages(compressed, max_tokens)
        return compressed
    except Exception as exc:
        logger.warning("session_memory 压缩失败，回退截断: %s", exc)
        # 回退：丢弃更早轮，只留近 N 轮（可带旧摘要截断）
        fallback = trim_to_turns(messages, turns)
        return _hard_truncate_messages(fallback, max_tokens)


def enforce_memory_limits(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    写 Redis 窗口时的统一入口：先保证「摘要 + 近 3 轮」，再按 token 上限压缩。
    """
    turns = max_context_turns()
    max_tokens = max_context_tokens()
    _, dialogue = _split_summary_and_dialogue(messages)
    # 对话超过窗口，或已有摘要需要与超窗部分合并 → compress
    if len(dialogue) > turns * 2:
        return compress_older_turns(messages, turns=turns, max_tokens=max_tokens)
    trimmed = trim_to_turns(messages, turns)
    if estimate_messages_tokens(trimmed) <= max_tokens:
        return trimmed
    return compress_older_turns(trimmed, turns=turns, max_tokens=max_tokens)
