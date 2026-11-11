"""
Skill 注册表：id → 匹配器 + 提示词路径 + 工具名列表。

扩展新领域
----------
1. 在 ``server/prompts/reasoner/skills/<id>/SKILL.md`` 写说明书
2. 在本文件 ``SKILL_SPECS`` 加一项
3. 在 ``tools/`` 实现并挂到 ``tool_names``

图拓扑不用改。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

# prompts 根：server/prompts/reasoner
_PROMPTS_ROOT = Path(__file__).resolve().parents[4] / "prompts" / "reasoner"


@dataclass(frozen=True)
class SkillSpec:
    """单个技能的静态描述。"""

    id: str
    # 用户输入命中任一正则则启用
    patterns: tuple[re.Pattern[str], ...]
    # 相对 _PROMPTS_ROOT 的 SKILL.md 路径
    prompt_relpath: str
    # 该技能额外暴露的工具名（core 工具始终可用，不写在这里）
    tool_names: tuple[str, ...] = ()
    title: str = ""


def _pat(*exprs: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(e, re.IGNORECASE) for e in exprs)


# travel + rag + askdata；core 工具不依赖 skill。
# 绑 KB → 强制 rag；agent_type=askdata / force_skills → 强制 askdata（见 load_context）。
SKILL_SPECS: dict[str, SkillSpec] = {
    "rag": SkillSpec(
        id="rag",
        title="知识库问答",
        patterns=_pat(
            r"(知识库|文档|合同|协议|条款|材料|上传的|"
            r"根据(文件|文档|资料)|在(文件|文档)里|"
            r"nda|合同约定|说明书)"
        ),
        prompt_relpath="skills/rag/SKILL.md",
        tool_names=("rag_search",),
    ),
    "travel": SkillSpec(
        id="travel",
        title="旅游规划",
        patterns=_pat(
            r"(旅游|旅行|出行|行程|攻略|景点|酒店|民宿|机票|高铁|"
            r"几日游|日游|天游|自由行|度假|打卡|游玩|出游|"
            r"天两晚|晚游|规划.{0,8}(游|行|酒店|行程)|"
            r"\d+\s*天|\d+\s*日|"
            r"weather|itinerary|hotel|trip|travel)"
        ),
        prompt_relpath="skills/travel/SKILL.md",
        tool_names=(
            "get_weather",
            "get_culture",
            "geocode_places",
            "build_route",
            "search_stay",
            "plan_transport",
        ),
    ),
    "askdata": SkillSpec(
        id="askdata",
        title="智能问数",
        patterns=_pat(
            r"(销售额|营收|gmv|订单量|单量|客单价|"
            r"门店汇总|各门店|今日销售|本月销售|退款|支付|"
            r"问数|查一下.*(销售|订单|退款)|"
            r"多少单|卖了多少|退款金额)"
        ),
        prompt_relpath="skills/askdata/SKILL.md",
        tool_names=(
            "askdata_route",
            "askdata_draft_sql",
            "askdata_probe",
            "askdata_permission_rules",
            "askdata_validate_and_run",
        ),
    ),
}


def prompts_root() -> Path:
    return _PROMPTS_ROOT


def load_skill_markdown(skill_id: str) -> str:
    """读取 SKILL.md 正文（去掉 YAML front matter）。"""
    spec = SKILL_SPECS.get(skill_id)
    if not spec:
        return ""
    path = _PROMPTS_ROOT / spec.prompt_relpath
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2].strip()
    return text


def match_skills(user_input: str) -> list[str]:
    """根据用户原文解析应启用的 skill id（可多个）。"""
    text = (user_input or "").strip()
    if not text:
        return []
    hit: list[str] = []
    for sid, spec in SKILL_SPECS.items():
        if any(p.search(text) for p in spec.patterns):
            hit.append(sid)
    return hit


def merge_session_skills(
    previous: Sequence[str], current: Sequence[str]
) -> list[str]:
    """会话粘滞：本轮 ∪ 历史，保序去重（跟进句丢关键词也不掉 skill）。"""
    merged: list[str] = []
    for sid in list(previous) + list(current):
        s = str(sid or "").strip()
        if s and s not in merged:
            merged.append(s)
    return merged


def append_skills_to_system(base_prompt: str, skill_ids: Sequence[str]) -> str:
    """把已启用技能的 Markdown 拼进 system prompt。"""
    chunks = [base_prompt.rstrip()]
    for sid in skill_ids:
        body = load_skill_markdown(sid)
        if not body:
            continue
        title = SKILL_SPECS.get(sid).title if sid in SKILL_SPECS else sid
        chunks.append(f"## 已启用技能：{title or sid}\n\n{body}")
    return "\n\n".join(chunks) + "\n"
