"""
对比评测：baseline（向量 top-K）vs full（理解/扩写/双路 RRF/重排）。

both 时输出分目录，避免混在一起：
  <out>/summary.md          # 总对比
  <out>/baseline/...        # 仅 baseline
  <out>/full/...            # 仅 full

示例:
  python -m test_code.eval_compare ^
    --dataset test\\eval_sample.json ^
    --unit-id 1 --kb-id 1 --user-id 1 ^
    --pipeline both --limit 5 ^
    --vector-weight 0.4 --bm25-weight 0.6 ^
    --out test\\eval_runs\\compare1
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SERVER = _REPO_ROOT / "server"
for p in (str(_SERVER), str(_REPO_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")
load_dotenv(_SERVER / ".env")

os.environ.setdefault(
    "LANGCHAIN_TRACING_V2",
    os.getenv("LANGCHAIN_TRACING_V2", "false"),
)
os.environ.setdefault("RAG_TRACE_DIR", str(_REPO_ROOT / "logs"))

from app.agents.rag.agent import run_rag
from app.agents.rag.pipeline.judge import format_score_bar, judge_rag
from app.common import tracing as rag_trace
from app.common.observability import bootstrap_observability
from app.infra import file_repo
from app.settings import get_settings
from test_code.eval_lib import (
    build_filename_to_file_id,
    gold_answers_and_file_key,
    hit_at_k,
    load_eval_json,
    resolve_file_id,
)


def _list_kb_files(unit_id: int, kb_id: int) -> List[Dict[str, Any]]:
    return file_repo.list_by_kb(kb_id, unit_id=unit_id, page=1, size=5000)


def _avg(vals: List[float]) -> Optional[float]:
    if not vals:
        return None
    return round(sum(vals) / len(vals), 4)


def _hit_rate(rows: List[Dict[str, Any]]) -> Optional[float]:
    scored = [r for r in rows if r.get("hit_at_k") is not None]
    if not scored:
        return None
    return round(sum(1 for r in scored if r.get("hit_at_k")) / len(scored), 4)


def run_one(
    *,
    unit_id: int,
    kb_id: int,
    user_id: int,
    query: str,
    gold: str,
    pipeline: str,
    file_ids: Optional[List[int]],
    vector_weight: Optional[float],
    bm25_weight: Optional[float],
    hit_k: int,
) -> Dict[str, Any]:
    t0 = time.perf_counter()
    row: Dict[str, Any] = {
        "pipeline": pipeline,
        "query": query,
        "gold": gold,
        "file_ids": file_ids,
        "hit_k": hit_k,
    }
    try:
        result = run_rag(
            unit_id=unit_id,
            knowledge_base_id=kb_id,
            query=query,
            pipeline=pipeline,  # type: ignore[arg-type]
            file_ids=file_ids,
            user_id=user_id,
            vector_weight=vector_weight if pipeline == "full" else None,
            bm25_weight=bm25_weight if pipeline == "full" else None,
        )
        answer = result.get("answer") or ""
        chunks = result.get("chunks") or []
        judge = judge_rag(
            query=query, answer=answer, contexts=chunks, gold=gold or None
        )
        # 有金标才算 hit@K：金标文本是否被 top-K 切片覆盖
        hit: Optional[bool]
        if gold and gold.strip():
            hit = hit_at_k(gold, chunks, k=hit_k)
        else:
            hit = None

        row.update(
            {
                "request_id": result.get("request_id"),
                "standalone_query": result.get("standalone_query"),
                "expanded_queries": result.get("expanded_queries"),
                "skip_answer": result.get("skip_answer"),
                "rag_answer": answer,
                "chunks_preview": rag_trace.summarize_chunks(chunks, limit=5),
                "chunk_count": len(chunks),
                "judge": judge,
                "faithfulness": judge.get("faithfulness"),
                "context_relevance": judge.get("context_relevance"),
                "answer_relevance": judge.get("answer_relevance"),
                "gold_coverage": judge.get("gold_coverage"),
                "overall": judge.get("overall"),
                "hit_at_k": hit,
                "vector_weight": result.get("vector_weight"),
                "bm25_weight": result.get("bm25_weight"),
                "latency_ms": result.get("latency_ms")
                or round((time.perf_counter() - t0) * 1000, 2),
                "error": None,
            }
        )
        return row
    except Exception as e:
        row.update(
            {
                "request_id": None,
                "rag_answer": "",
                "chunks_preview": [],
                "chunk_count": 0,
                "judge": {
                    "ok": False,
                    "reason": str(e),
                    "faithfulness": 0,
                    "context_relevance": 0,
                    "answer_relevance": 0,
                    "overall": 0,
                },
                "faithfulness": 0,
                "context_relevance": 0,
                "answer_relevance": 0,
                "gold_coverage": None,
                "overall": 0,
                "hit_at_k": False if (gold and gold.strip()) else None,
                "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                "error": str(e),
            }
        )
        return row


def _write_case_md(path: Path, row: Dict[str, Any]) -> None:
    j = row.get("judge") or {}
    hit = row.get("hit_at_k")
    hit_s = "n/a" if hit is None else ("HIT" if hit else "MISS")
    lines = [
        f"# Case `{row.get('request_id') or '-'}`",
        "",
        f"- pipeline: **{row.get('pipeline')}**",
        f"- latency_ms: `{row.get('latency_ms')}`",
        f"- file_key: `{row.get('file_key') or '-'}` mapped=`{row.get('file_mapped')}`",
        f"- overall: `{row.get('overall')}`",
        f"- hit@{row.get('hit_k', 10)}: **{hit_s}**",
        "",
        "## 三要素 + 召回",
        "",
        "| 指标 | 分数 | 条形 |",
        "|------|------|------|",
        f"| Faithfulness 忠诚度 | {row.get('faithfulness')} | `{format_score_bar(row.get('faithfulness'))}` |",
        f"| Context Relevance 上下文相关性 | {row.get('context_relevance')} | `{format_score_bar(row.get('context_relevance'))}` |",
        f"| Answer Relevance 答案相关性 | {row.get('answer_relevance')} | `{format_score_bar(row.get('answer_relevance'))}` |",
        f"| Gold Coverage 金标覆盖 | {row.get('gold_coverage')} | `{format_score_bar(row.get('gold_coverage'))}` |",
        f"| hit@{row.get('hit_k', 10)} 金标是否进上下文 | {hit_s} | — |",
        "",
        f"- labels: `{json.dumps(j.get('labels') or {}, ensure_ascii=False)}`",
        f"- reason: {j.get('reason') or '（无）'}",
        f"- gap: {j.get('gap') or '（无）'}",
        "",
        "## Query",
        "",
        "```",
        str(row.get("query") or ""),
        "```",
        "",
    ]
    if row.get("standalone_query") and row.get("standalone_query") != row.get("query"):
        lines += [
            "## Standalone query",
            "",
            "```",
            str(row.get("standalone_query")),
            "```",
            "",
        ]
    if row.get("expanded_queries"):
        lines += ["## Expanded queries", ""]
        for q in row["expanded_queries"]:
            lines.append(f"- {q}")
        lines.append("")
    lines += [
        "## Gold",
        "",
        "```",
        str(row.get("gold") or "（无）"),
        "```",
        "",
        "## Answer",
        "",
        "```",
        str(row.get("rag_answer") or ""),
        "```",
        "",
        "## Top chunks",
        "",
    ]
    for c in row.get("chunks_preview") or []:
        lines.append(
            f"- id=`{c.get('id')}` file=`{c.get('file_id')}` "
            f"score=`{c.get('score')}` src=`{c.get('source')}`"
        )
        lines.append(f"  - {c.get('preview')}")
    evidence = j.get("evidence") or {}
    if evidence:
        lines += [
            "",
            "## Judge evidence",
            "",
            "```json",
            json.dumps(evidence, ensure_ascii=False, indent=2),
            "```",
        ]
    if row.get("error"):
        lines += ["", "## Error", "", str(row["error"])]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_results_md(path: Path, rows: List[Dict[str, Any]], *, title: str) -> None:
    lines = [
        f"# {title}",
        "",
        "| # | overall | F | C | A | gold | hit@K | ms | query |",
        "|---|---------|---|---|---|------|-------|----|-------|",
    ]
    for i, r in enumerate(rows):
        q = (r.get("query") or "").replace("|", "\\|")
        if len(q) > 48:
            q = q[:48] + "…"
        hit = r.get("hit_at_k")
        hit_s = "n/a" if hit is None else ("Y" if hit else "N")
        lines.append(
            f"| {i} | {r.get('overall')} | "
            f"{r.get('faithfulness')} | {r.get('context_relevance')} | "
            f"{r.get('answer_relevance')} | {r.get('gold_coverage')} | "
            f"{hit_s} | {r.get('latency_ms')} | {q} |"
        )
    lines.append("")
    for i, r in enumerate(rows):
        j = r.get("judge") or {}
        hit = r.get("hit_at_k")
        hit_s = "n/a" if hit is None else ("HIT" if hit else "MISS")
        lines += [
            f"## [{i}] overall={r.get('overall')} hit={hit_s}",
            "",
            f"- request_id: `{r.get('request_id')}`",
            f"- F=`{format_score_bar(r.get('faithfulness'))}` "
            f"C=`{format_score_bar(r.get('context_relevance'))}` "
            f"A=`{format_score_bar(r.get('answer_relevance'))}`",
            f"- reason: {j.get('reason') or '（无）'}",
            f"- gap: {j.get('gap') or '（无）'}",
            "",
            "**Q:** " + str(r.get("query") or ""),
            "",
            "**A:** " + str(r.get("rag_answer") or "")[:800],
            "",
            "---",
            "",
        ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _pipe_stats(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "n": len(items),
        "overall": _avg([float(x.get("overall") or 0) for x in items]),
        "faithfulness": _avg([float(x.get("faithfulness") or 0) for x in items]),
        "context_relevance": _avg(
            [float(x.get("context_relevance") or 0) for x in items]
        ),
        "answer_relevance": _avg(
            [float(x.get("answer_relevance") or 0) for x in items]
        ),
        "gold_coverage": _avg(
            [
                float(x["gold_coverage"])
                for x in items
                if x.get("gold_coverage") is not None
            ]
        ),
        "hit_at_k_rate": _hit_rate(items),
        "avg_ms": _avg([float(x.get("latency_ms") or 0) for x in items]),
    }


def _write_summary(
    path: Path,
    rows: List[Dict[str, Any]],
    config: Dict[str, Any],
    *,
    title: str,
    per_pipe_dirs: Optional[Dict[str, str]] = None,
) -> None:
    by_pipe: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pipe[str(r.get("pipeline"))].append(r)

    lines = [
        f"# {title}",
        "",
        f"- generated_at: `{config.get('generated_at')}`",
        f"- dataset: `{config.get('dataset')}`",
        f"- unit_id={config.get('unit_id')} kb_id={config.get('kb_id')} "
        f"user_id={config.get('user_id')}",
        f"- pipelines: `{config.get('pipelines')}`",
        f"- hit_k: `{config.get('hit_k')}`",
        f"- full weights: vector=`{config.get('vector_weight')}` "
        f"bm25=`{config.get('bm25_weight')}`",
        f"- n_rows: **{len(rows)}**",
        "",
    ]
    if per_pipe_dirs:
        lines += ["## 分目录（both 时按流水线拆开）", ""]
        for pipe, rel in sorted(per_pipe_dirs.items()):
            lines.append(f"- **{pipe}** → `{rel}/`（summary / results / cases）")
        lines.append("")

    lines += [
        "## 按流水线均值",
        "",
        "| pipeline | n | overall↑ | F | C | A | gold_cov | hit@K↑ | avg_ms |",
        "|----------|---|----------|---|---|---|----------|--------|--------|",
    ]
    for pipe, items in sorted(by_pipe.items()):
        st = _pipe_stats(items)
        lines.append(
            f"| {pipe} | {st['n']} | {st['overall']} | {st['faithfulness']} | "
            f"{st['context_relevance']} | {st['answer_relevance']} | "
            f"{st['gold_coverage']} | {st['hit_at_k_rate']} | {st['avg_ms']} |"
        )
    lines += [
        "",
        "## 怎么读",
        "",
        "- **hit@K**：金标文本是否出现在检索 top-K 切片中（有金标才计；覆盖即可）。",
        "- **Faithfulness / Context / Answer**：LLM Judge 三要素。",
        "- **Gold Coverage**：回答相对金标要点（从宽，语义覆盖）。",
        "- 面试对照：baseline vs full 看 overall、hit@K、延迟差。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _dump_pipeline_dir(pipe_dir: Path, rows: List[Dict[str, Any]], pipe: str) -> None:
    pipe_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = pipe_dir / "cases"
    cases_dir.mkdir(exist_ok=True)
    for row in rows:
        case_name = f"{int(row.get('index') or 0):04d}_{row.get('request_id') or 'err'}.md"
        _write_case_md(cases_dir / case_name, row)

    (pipe_dir / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    with (pipe_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            slim = dict(r)
            j = dict(slim.get("judge") or {})
            j.pop("raw", None)
            slim["judge"] = j
            f.write(json.dumps(slim, ensure_ascii=False, default=str) + "\n")

    _write_results_md(
        pipe_dir / "results.md",
        rows,
        title=f"RAG Results — {pipe}",
    )
    _write_summary(
        pipe_dir / "summary.md",
        rows,
        {
            "generated_at": "",
            "dataset": "",
            "unit_id": "",
            "kb_id": "",
            "user_id": "",
            "pipelines": [pipe],
            "hit_k": rows[0].get("hit_k") if rows else 10,
            "vector_weight": rows[0].get("vector_weight") if rows else None,
            "bm25_weight": rows[0].get("bm25_weight") if rows else None,
        },
        title=f"RAG Summary — {pipe}",
    )


def main() -> None:
    s = get_settings()
    parser = argparse.ArgumentParser(description="Baseline vs Full RAG 对比评测")
    parser.add_argument("--dataset", required=True, help="评测 JSON（tests[]）")
    parser.add_argument("--unit-id", type=int, required=True)
    parser.add_argument("--kb-id", type=int, required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument(
        "--pipeline",
        choices=["baseline", "full", "both"],
        default="both",
    )
    parser.add_argument("--limit", type=int, default=0, help="0=全部")
    parser.add_argument("--out", required=True, help="输出根目录")
    parser.add_argument(
        "--scope-files",
        action="store_true",
        help="按金标文件名收窄 file_ids（若能映射）",
    )
    parser.add_argument(
        "--vector-weight",
        type=float,
        default=None,
        help=f"full 向量路权重（默认 Settings={s.rrf_vector_weight}）",
    )
    parser.add_argument(
        "--bm25-weight",
        type=float,
        default=None,
        help=f"full BM25 路权重（默认 Settings={s.rrf_bm25_weight}）",
    )
    parser.add_argument(
        "--hit-k",
        type=int,
        default=None,
        help=f"hit@K 的 K（默认 ANSWER_TOP_K={s.answer_top_k}）",
    )
    args = parser.parse_args()

    bootstrap_observability()

    vector_weight = (
        args.vector_weight if args.vector_weight is not None else s.rrf_vector_weight
    )
    bm25_weight = (
        args.bm25_weight if args.bm25_weight is not None else s.rrf_bm25_weight
    )
    hit_k = args.hit_k if args.hit_k is not None else s.answer_top_k

    tests = load_eval_json(args.dataset)
    if args.limit and args.limit > 0:
        tests = tests[: args.limit]

    kb_files = _list_kb_files(args.unit_id, args.kb_id)
    mapping = build_filename_to_file_id(kb_files)

    pipelines = (
        ["baseline", "full"] if args.pipeline == "both" else [args.pipeline]
    )
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = _REPO_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": str(args.dataset),
        "unit_id": args.unit_id,
        "kb_id": args.kb_id,
        "user_id": args.user_id,
        "pipelines": pipelines,
        "limit": args.limit,
        "scope_files": args.scope_files,
        "vector_weight": vector_weight,
        "bm25_weight": bm25_weight,
        "hit_k": hit_k,
        "settings": {
            "answer_top_k": s.answer_top_k,
            "query_expand_n": s.query_expand_n,
            "rerank_enabled": s.rerank_enabled,
            "embedding_model": s.dashscope_embedding_model,
            "rrf_vector_weight_default": s.rrf_vector_weight,
            "rrf_bm25_weight_default": s.rrf_bm25_weight,
        },
    }
    (out_dir / "config.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    rows: List[Dict[str, Any]] = []
    for idx, test in enumerate(tests):
        query = (test.get("query") or "").strip()
        gold, file_key, _ = gold_answers_and_file_key(test)
        file_id = resolve_file_id(file_key, mapping) if file_key else None
        file_ids = [file_id] if (args.scope_files and file_id) else None
        for pipe in pipelines:
            print(
                f"[{idx + 1}/{len(tests)}] pipeline={pipe} "
                f"q={query[:60]!r} file_mapped={bool(file_id)}"
            )
            row = run_one(
                unit_id=args.unit_id,
                kb_id=args.kb_id,
                user_id=args.user_id,
                query=query,
                gold=gold,
                pipeline=pipe,
                file_ids=file_ids,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight,
                hit_k=hit_k,
            )
            row["index"] = idx
            row["file_key"] = file_key
            row["file_id"] = file_id
            row["file_mapped"] = bool(file_id)
            rows.append(row)
            hit = row.get("hit_at_k")
            hit_s = "n/a" if hit is None else ("HIT" if hit else "MISS")
            print(
                f"  -> overall={row.get('overall')} hit@{hit_k}={hit_s} "
                f"F={row.get('faithfulness')} C={row.get('context_relevance')} "
                f"A={row.get('answer_relevance')} ms={row.get('latency_ms')}"
            )

    by_pipe: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_pipe[str(r.get("pipeline"))].append(r)

    per_pipe_dirs: Dict[str, str] = {}
    for pipe, items in by_pipe.items():
        pipe_dir = out_dir / pipe
        _dump_pipeline_dir(pipe_dir, items, pipe)
        per_pipe_dirs[pipe] = pipe

    # 根目录总对比 + 全量 json
    (out_dir / "results.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    _write_summary(
        out_dir / "summary.md",
        rows,
        config,
        title="RAG Compare Summary（baseline vs full）",
        per_pipe_dirs=per_pipe_dirs if len(pipelines) > 1 else None,
    )
    print(f"\nDone. Compare: {out_dir / 'summary.md'}")
    for pipe, rel in sorted(per_pipe_dirs.items()):
        print(f"  {pipe}: {out_dir / rel / 'summary.md'}")


if __name__ == "__main__":
    main()
