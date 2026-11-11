"""
单轮运行预算：token 软/硬闸 + web_search / rag_search 配额。

对照参考 ``agent/runtime/budget.py``，挂在 LangGraph state.budget 上。
- token 硬闸：整轮收口（禁再规划 / 再绑工具）
- web/rag 配额：只停对应工具，仍走正常逐步评审
"""
from __future__ import annotations

from dataclasses import dataclass

from app.agents.reasoner.state import BudgetState, empty_budget
from app.settings import get_settings


@dataclass(frozen=True)
class RunBudgetConfig:
    token_budget: int = 80_000
    soft_ratio: float = 0.60
    hard_ratio: float = 0.85
    web_search_max: int = 8
    # 知识库检索默认最多 2 次（产品约定）
    rag_search_max: int = 2

    @property
    def soft_tokens(self) -> int:
        return max(1, int(self.token_budget * self.soft_ratio))

    @property
    def hard_tokens(self) -> int:
        return max(1, int(self.token_budget * self.hard_ratio))


def load_budget_config(
    *,
    token_budget: int | None = None,
    answer_mode: str | None = None,
) -> RunBudgetConfig:
    """
    读取预算配置。

    - ``token_budget`` 显式覆盖优先；
    - 否则 ``answer_mode=quick`` → ``reasoner_token_budget_quick``（默认 4 万）；
    - 否则深度默认 ``reasoner_token_budget``（8 万）。
    """
    s = get_settings()
    if token_budget is not None and int(token_budget) > 0:
        tb = int(token_budget)
    elif (answer_mode or "").strip().lower() == "quick":
        tb = int(getattr(s, "reasoner_token_budget_quick", 40_000) or 40_000)
    else:
        tb = int(getattr(s, "reasoner_token_budget", 80_000) or 80_000)
    return RunBudgetConfig(
        token_budget=tb,
        soft_ratio=float(getattr(s, "reasoner_token_soft_ratio", 0.60) or 0.60),
        hard_ratio=float(getattr(s, "reasoner_token_hard_ratio", 0.85) or 0.85),
        web_search_max=int(getattr(s, "reasoner_web_search_max", 8) or 8),
        rag_search_max=int(getattr(s, "reasoner_rag_search_max", 2) or 2),
    )


SOFT_HINT = (
    "【预算提示】本轮上下文消耗已较高：请尽量直接给出最终回答；"
    "若必须调用工具，避免再做网页搜索，优先使用已有观察结果；"
    "知识库检索也请克制（本轮最多 2 次）。"
)


def budget_config_from_state(state: dict | None) -> RunBudgetConfig:
    """从 ReasonerState 读取 answer_mode / token_budget 组装配置。"""
    st = state or {}
    tb = st.get("token_budget")
    try:
        tb_i = int(tb) if tb is not None else None
    except (TypeError, ValueError):
        tb_i = None
    return load_budget_config(
        token_budget=tb_i,
        answer_mode=str(st.get("answer_mode") or "") or None,
    )


def add_llm_usage(budget: BudgetState, prompt_tokens: int, completion_tokens: int) -> BudgetState:
    b = dict(budget or empty_budget())
    b["prompt_tokens"] = int(b.get("prompt_tokens") or 0) + max(0, int(prompt_tokens or 0))
    b["completion_tokens"] = int(b.get("completion_tokens") or 0) + max(
        0, int(completion_tokens or 0)
    )
    return b  # type: ignore[return-value]


def add_web_search(budget: BudgetState, n: int = 1) -> BudgetState:
    b = dict(budget or empty_budget())
    b["web_search_calls"] = int(b.get("web_search_calls") or 0) + max(0, int(n or 0))
    return b  # type: ignore[return-value]


def add_rag_search(budget: BudgetState, n: int = 1) -> BudgetState:
    b = dict(budget or empty_budget())
    b["rag_search_calls"] = int(b.get("rag_search_calls") or 0) + max(0, int(n or 0))
    return b  # type: ignore[return-value]


def tool_quota_exhausted(
    budget: BudgetState, cfg: RunBudgetConfig | None = None
) -> tuple[bool, str]:
    """
    工具配额是否用尽（web / rag）。

    只表示「不能再继续搜」，不等于整轮必须跳过 review。
    真正停搜靠工具自身拒呼（如 rag_search max_calls）。
    """
    cfg = cfg or load_budget_config()
    b = budget or empty_budget()
    if int(b.get("web_search_calls") or 0) >= cfg.web_search_max:
        return True, "web_search_max"
    if int(b.get("rag_search_calls") or 0) >= cfg.rag_search_max:
        return True, "rag_search_max"
    return False, ""


def should_force_final(budget: BudgetState, cfg: RunBudgetConfig | None = None) -> tuple[bool, str]:
    """硬闸：仅 token 触顶 → 强制收口（禁止再规划/再绑工具）。

    web/rag 次数上限见 ``tool_quota_exhausted``：停对应工具，仍走正常逐步评审。
    """
    cfg = cfg or load_budget_config()
    b = budget or empty_budget()
    if int(b.get("prompt_tokens") or 0) >= cfg.hard_tokens:
        return True, "token_hard"
    return False, ""


def maybe_soft_nudge(
    budget: BudgetState, cfg: RunBudgetConfig | None = None
) -> tuple[BudgetState, str]:
    """
    软闸：达 soft 且尚未 nudge → 返回提示文案，并标记 soft_nudged。
    仍允许继续调用工具。
    """
    cfg = cfg or load_budget_config()
    b = dict(budget or empty_budget())
    if b.get("soft_nudged"):
        return b, ""  # type: ignore[return-value]
    if int(b.get("prompt_tokens") or 0) >= cfg.soft_tokens:
        b["soft_nudged"] = True
        return b, SOFT_HINT  # type: ignore[return-value]
    return b, ""  # type: ignore[return-value]
