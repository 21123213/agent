"""
Schema Catalog（轻量 ER + 字段注释）。

权威数据：``docs/askdata/catalog.yaml``（表/字段/同义词/joins）。
问数不再做业务域 / 表路由裁剪，NL2SQL 草稿直接注入目录中的全部业务表。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# repo 根：server/app/agents/askdata → parents[4]
_REPO_ROOT = Path(__file__).resolve().parents[4]
_DOCS = _REPO_ROOT / "docs" / "askdata"


@lru_cache
def _load_catalog_tables() -> dict[str, dict[str, Any]]:
    path = _DOCS / "catalog.yaml"
    if not path.is_file():
        raise FileNotFoundError(
            f"缺少 Schema Catalog 文档: {path}。"
            "请维护 docs/askdata/catalog.yaml。"
        )
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    tables = raw.get("tables") or {}
    if not isinstance(tables, dict) or not tables:
        raise ValueError(f"{path} 中 tables 为空或格式错误")
    return dict(tables)


def reload_catalog() -> None:
    """测试或热更新时清缓存。"""
    _load_catalog_tables.cache_clear()


# 兼容旧代码：``from schema_catalog import CATALOG``
# 注意：这是加载时快照；改 yaml 后需 reload_catalog() 或进程重启。
CATALOG: dict[str, dict[str, Any]] = {}


def _ensure_catalog_bound() -> dict[str, dict[str, Any]]:
    global CATALOG
    tables = _load_catalog_tables()
    if not CATALOG or set(CATALOG) != set(tables):
        CATALOG = tables
    return CATALOG


def get_catalog() -> dict[str, dict[str, Any]]:
    """始终返回最新缓存的表目录。"""
    return _ensure_catalog_bound()


def all_business_tables() -> list[str]:
    """返回 catalog 中全部业务表/视图名（保持 yaml 声明顺序）。"""
    return list(get_catalog().keys())


def format_catalog_for_prompt(tables: list[str] | None = None) -> str:
    """生成注入 NL2SQL / draft 工具 prompt 的文本块；默认全量业务表。"""
    catalog = get_catalog()
    selected = list(tables) if tables else all_business_tables()
    blocks: list[str] = []
    joins: list[str] = []
    for t in selected:
        meta = catalog.get(t)
        if not meta:
            continue
        cols = ", ".join(
            f"{c}({desc})" for c, desc in (meta.get("columns") or {}).items()
        )
        blocks.append(
            f"- {t} [{meta.get('kind')}]: {meta.get('comment')}\n  字段: {cols}"
        )
        joins.extend(meta.get("joins") or [])
    join_txt = "\n".join(f"  * {j}" for j in dict.fromkeys(joins))
    return "表与字段:\n" + "\n".join(blocks) + "\n关联:\n" + (join_txt or "  （无）")


# 模块 import 时预热，保持 CATALOG 符号可用
try:
    _ensure_catalog_bound()
except FileNotFoundError:
    # 允许文档尚未就位时 import（测试环境应有 yaml）
    pass
