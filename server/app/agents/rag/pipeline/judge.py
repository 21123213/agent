"""
LLM-as-a-Judge：RAG 三要素打分。

1. faithfulness（忠诚度）：答案是否可由检索上下文支持
2. context_relevance（上下文相关性）：检索片段相对问题是否有用
3. answer_relevance（答案相关性）：答案是否回应了问题

另保留 gold_coverage（相对金标要点覆盖），便于有标注集时对照。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from app.common.llm_client import Purpose, invoke_chat
from app.common.llm_text import normalize_llm_text


JUDGE_SYSTEM = """你是严谨的 RAG 评测裁判。根据「用户问题 / 检索上下文 / 系统回答 /（可选）金标」打分。
只输出一个 JSON 对象，不要 markdown，不要解释性前后文。

评分规则（每项 0.0–1.0，可保留一位小数）：
1. faithfulness（忠诚度）：答案中的事实主张是否都能被检索上下文支持。
   - 1.0：全部可追溯；0.5：部分可追溯或含轻微推断；0.0：明显幻觉/与上下文矛盾。
2. context_relevance（上下文相关性）：检索片段对回答该问题有多相关。
   - 1.0：片段高度相关且覆盖问题要点；0.5：部分相关；0.0：基本无关。
3. answer_relevance（答案相关性）：回答是否针对问题本身。
   - 1.0：直接、完整回应问题；0.5：部分回应；0.0：跑题或拒答不当。
4. gold_coverage（金标覆盖，无金标时填 null）：回答是否覆盖金标关键要点。
   - 从宽：改述/扩写可接受，只要要点在。

同时给出 brief 理由与 gap（缺口）。JSON schema:
{
  "faithfulness": 0.0,
  "context_relevance": 0.0,
  "answer_relevance": 0.0,
  "gold_coverage": 0.0或null,
  "overall": 0.0,
  "labels": {
    "faithfulness": "supported|partial|hallucinated",
    "context_relevance": "high|medium|low",
    "answer_relevance": "on_point|partial|off_topic"
  },
  "evidence": {
    "supported_claims": ["..."],
    "unsupported_claims": ["..."],
    "useful_context_ids": ["1","2"],
    "missing_aspects": ["..."]
  },
  "reason": "一两句总评",
  "gap": "主要缺口；无明显缺口写「无」"
}

overall = 三要素算术平均（不含 gold_coverage）；有金标时也可自行加权，但仍要填 arithmetic mean of the three。
"""


def _clip_contexts(chunks: List[Dict[str, Any]], max_chars: int = 12000) -> str:
    parts: List[str] = []
    used = 0
    for i, c in enumerate(chunks, start=1):
        text = (c.get("content") or c.get("chunk_text") or "").strip()
        if not text:
            continue
        block = f"[{i}] {text}"
        if used + len(block) > max_chars:
            remain = max_chars - used
            if remain > 200:
                parts.append(block[:remain] + "…")
            break
        parts.append(block)
        used += len(block) + 2
    return "\n\n".join(parts) if parts else "（无检索上下文）"


def _parse_json(text: str) -> Dict[str, Any]:
    text = normalize_llm_text(text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("裁判输出无法解析为 JSON")
    return json.loads(text[start : end + 1])


def _clamp01(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, x))


def judge_rag(
    *,
    query: str,
    answer: str,
    contexts: List[Dict[str, Any]],
    gold: Optional[str] = None,
) -> Dict[str, Any]:
    """对单次 RAG 结果做三要素裁判。"""
    empty: Dict[str, Any] = {
        "faithfulness": 0.0,
        "context_relevance": 0.0,
        "answer_relevance": 0.0,
        "gold_coverage": None,
        "overall": 0.0,
        "labels": {},
        "evidence": {},
        "reason": "",
        "gap": "",
        "raw": "",
        "ok": False,
    }
    human_parts = [
        f"用户问题:\n{(query or '').strip()}",
        f"检索上下文:\n{_clip_contexts(contexts)}",
        f"系统回答:\n{(answer or '').strip()}",
    ]
    if gold and gold.strip():
        human_parts.append(f"金标/标准要点:\n{gold.strip()}")
    else:
        human_parts.append("金标/标准要点:\n（无，请将 gold_coverage 设为 null）")

    try:
        raw = invoke_chat(
            [
                SystemMessage(content=JUDGE_SYSTEM),
                HumanMessage(content="\n\n".join(human_parts) + "\n"),
            ],
            purpose=Purpose.JUDGE,
            temperature=0.0,
            reason="step",
        )
        empty["raw"] = raw
        data = _parse_json(raw)
        f = _clamp01(data.get("faithfulness")) or 0.0
        c = _clamp01(data.get("context_relevance")) or 0.0
        a = _clamp01(data.get("answer_relevance")) or 0.0
        g = _clamp01(data.get("gold_coverage"))
        overall = _clamp01(data.get("overall"))
        if overall is None:
            overall = round((f + c + a) / 3.0, 3)
        return {
            "faithfulness": f,
            "context_relevance": c,
            "answer_relevance": a,
            "gold_coverage": g,
            "overall": overall,
            "labels": data.get("labels") or {},
            "evidence": data.get("evidence") or {},
            "reason": str(data.get("reason") or "").strip(),
            "gap": str(data.get("gap") or "").strip(),
            "raw": raw,
            "ok": True,
        }
    except Exception as e:
        empty["reason"] = f"裁判失败: {e}"
        empty["raw"] = str(e)
        empty["gap"] = "裁判调用失败"
        return empty


def format_score_bar(score: Optional[float], width: int = 10) -> str:
    if score is None:
        return "n/a"
    s = max(0.0, min(1.0, float(score)))
    filled = int(round(s * width))
    return f"{'█' * filled}{'░' * (width - filled)} {s:.2f}"
