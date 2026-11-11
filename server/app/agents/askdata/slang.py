"""
行业黑话加载与匹配。

权威文件：``docs/askdata/slang.yaml``。
探查无数据时，把命中的黑话 hint 返回给主模型，用于向用户澄清语义。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SLANG_PATH = _REPO_ROOT / "docs" / "askdata" / "slang.yaml"


@lru_cache
def _load_entries() -> list[dict[str, Any]]:
    if not _SLANG_PATH.is_file():
        return []
    raw = yaml.safe_load(_SLANG_PATH.read_text(encoding="utf-8")) or {}
    return list(raw.get("slang") or [])


def reload_slang() -> None:
    _load_entries.cache_clear()


def match_slang(question: str) -> list[dict[str, Any]]:
    """问句中出现的黑话条目（保序去重）。"""
    q = question or ""
    ql = q.lower()
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in _load_entries():
        term = str(e.get("term") or "").strip()
        if not term or term in seen:
            continue
        if term.lower() in ql or term in q:
            seen.add(term)
            hits.append(
                {
                    "term": term,
                    "maps_to": e.get("maps_to"),
                    "hint": e.get("hint") or "",
                }
            )
    return hits


def format_slang_hints(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return ""
    lines = ["可能的语义转义（请向用户确认是否理解有误）："]
    for h in hits:
        lines.append(f"- 「{h['term']}」→ {h.get('maps_to')}：{h.get('hint')}")
    return "\n".join(lines)
