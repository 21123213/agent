"""
失败题复核：导出原 trace，并重新提问一次，计算 hit@20。

示例（在 backend 目录下）:
  python -m test_code.review_failures \\
    --failures test_artifacts/eval_runs/run1/failures.jsonl \\
    --user-id 1 \\
    --trace logs/rag_trace.jsonl \\
    --out test_artifacts/eval_runs/run1/review
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from dotenv import load_dotenv

load_dotenv(_BACKEND_ROOT.parent / '.env')
load_dotenv(_BACKEND_ROOT / '.env')

from app.core import rag_trace
from app.core.rag import rag_service
from app.core.retrieval_pipeline import _retrieve_for_single_query
from test_code.eval_lib import (
    events_for_stage,
    filter_events_by_request_id,
    hit_at_k,
    parse_trace_events,
    summarize_retrieved,
)
from test_code.eval_rag import EN_ANSWER_HINT, with_english_answer_hint

REVIEW_TOP_K = 20


def _load_failures(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def _chunks_md(events: List[Dict[str, Any]]) -> str:
    finals = events_for_stage(events, 'rrf_final')
    lines = ['# 原评测 request 的 rrf_final（来自 rag_trace）', '']
    if not finals:
        lines.append('_无 rrf_final 事件_')
        lines.append('')
        return '\n'.join(lines)

    data = finals[-1].get('data') or {}
    chunks = data.get('chunks') or []
    lines.append(f"- hits: {data.get('hits', len(chunks))}")
    lines.append(f"- query: {data.get('query', '')}")
    lines.append('')
    for i, c in enumerate(chunks, start=1):
        lines.append(
            f"## {i}. file_id={c.get('file_id')} chunk_index={c.get('chunk_index')} "
            f"score={c.get('score')} source={c.get('source')}"
        )
        lines.append('')
        lines.append(c.get('preview') or c.get('content') or '')
        lines.append('')
    return '\n'.join(lines)


def _ensure_retrieval_depth(top_k: int = REVIEW_TOP_K) -> None:
    os.environ['RETRIEVE_TOP_K_PER_PATH'] = str(max(
        top_k, int(os.getenv('RETRIEVE_TOP_K_PER_PATH', '20'))
    ))
    os.environ['RRF_PER_QUERY'] = str(max(
        top_k, int(os.getenv('RRF_PER_QUERY', '15'))
    ))


def reask_with_hit(
    *,
    user_id: int,
    query: str,
    gold: str,
    file_ids: Optional[List[int]],
    top_k: int = REVIEW_TOP_K,
) -> Dict[str, Any]:
    """复核时重新问一遍：英文作答 + hit@20。"""
    request_id = rag_trace.new_request_id()
    rag_trace.set_trace_context(
        request_id, user_id=user_id, session_id=f'review-{request_id}'
    )
    answer_k = int(os.getenv('ANSWER_TOP_K', '10'))
    query_for_gen = with_english_answer_hint(query)
    out: Dict[str, Any] = {
        'review_request_id': request_id,
        'query': query,
        'query_sent_to_generator': query_for_gen,
        'english_answer_hint': EN_ANSWER_HINT,
        'top_k': top_k,
    }
    try:
        rag_trace.emit(
            'request_start',
            data={
                'query': rag_trace.truncate_text(query),
                'mode': 'review_reask',
                'file_ids': file_ids,
                'top_k': top_k,
            },
        )
        fused = _retrieve_for_single_query(user_id, query, file_ids)
        top_chunks = fused[:top_k]
        hit = hit_at_k(gold, top_chunks, k=top_k)
        out['hit_at_20'] = hit
        out['retrieved_count'] = len(top_chunks)
        out['retrieved'] = summarize_retrieved(top_chunks, limit=top_k)

        gen_chunks = fused[:answer_k]
        if not gen_chunks:
            answer = (
                'Sorry, I could not find relevant information in your documents. '
                'Please confirm the files are uploaded and processed, or rephrase the question.'
            )
            out['skip_answer'] = True
        else:
            answer = rag_service.generate_answer(query_for_gen, gen_chunks, history=None)
            out['skip_answer'] = False
        out['review_rag_answer'] = answer

        rag_trace.finish(
            status='ok',
            query=rag_trace.truncate_text(query),
            hit_at_20=hit,
            answer_preview=rag_trace.truncate_text(answer),
        )
        return out
    except Exception as e:
        out['hit_at_20'] = False
        out['review_rag_answer'] = ''
        out['error'] = str(e)
        rag_trace.finish(status='error', message=str(e), error_type=type(e).__name__)
        return out
    finally:
        rag_trace.clear_trace_context()


def main() -> None:
    parser = argparse.ArgumentParser(description='失败题复核：trace 导出 + 重新提问算 hit@20')
    parser.add_argument('--failures', required=True, help='failures.jsonl 路径')
    parser.add_argument('--user-id', type=int, required=True, help='与评测相同的 user_id')
    parser.add_argument(
        '--trace',
        default='logs/rag_trace.jsonl',
        help='rag_trace.jsonl 路径',
    )
    parser.add_argument('--out', required=True, help='复核输出目录')
    parser.add_argument(
        '--top-k',
        type=int,
        default=REVIEW_TOP_K,
        help=f'复核时 hit@K 的 K（默认 {REVIEW_TOP_K}）',
    )
    parser.add_argument(
        '--skip-reask',
        action='store_true',
        help='只导出原 trace，不重新提问',
    )
    args = parser.parse_args()

    failures_path = Path(args.failures)
    if not failures_path.is_absolute():
        failures_path = (_BACKEND_ROOT / failures_path).resolve()
    trace_path = Path(args.trace)
    if not trace_path.is_absolute():
        trace_path = (_BACKEND_ROOT / trace_path).resolve()
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (_BACKEND_ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    failures = _load_failures(failures_path)
    print(f'加载失败题 {len(failures)} 条，解析 trace: {trace_path}')
    all_events = parse_trace_events(trace_path)
    print(f'trace 事件总数: {len(all_events)}')

    if not args.skip_reask:
        _ensure_retrieval_depth(args.top_k)

    index_lines = [
        '# Failure Review Index',
        '',
        f'- failures: {len(failures)}',
        f'- trace: `{trace_path}`',
        f'- reask: {not args.skip_reask}',
        f'- hit@k: {args.top_k}',
        '',
        '| # | request_id | file_key | answer_fail | hit_at_20 | review_rid |',
        '|---:|---|---|---|---|---|',
    ]

    review_rows: List[Dict[str, Any]] = []
    hit_true = 0
    hit_total = 0

    for i, row in enumerate(failures, start=1):
        rid = str(row.get('request_id') or '').strip() or f'missing-{i}'
        case_dir = out_dir / rid
        case_dir.mkdir(parents=True, exist_ok=True)

        file_ids = row.get('file_ids')
        if file_ids is None and row.get('file_id') is not None:
            file_ids = [row['file_id']]

        reask: Dict[str, Any] = {}
        if not args.skip_reask:
            print(f'[{i}/{len(failures)}] reask {rid} ...')
            reask = reask_with_hit(
                user_id=args.user_id,
                query=row.get('query') or '',
                gold=row.get('gold') or '',
                file_ids=file_ids,
                top_k=args.top_k,
            )
            hit_total += 1
            if reask.get('hit_at_20'):
                hit_true += 1

        meta = {
            'request_id': rid,
            'index': row.get('index'),
            'file_key': row.get('file_key'),
            'file_id': row.get('file_id'),
            'file_mapped': row.get('file_mapped'),
            'query': row.get('query'),
            'gold': row.get('gold'),
            'rag_answer': row.get('rag_answer'),
            'correct': row.get('correct'),
            'judge': row.get('judge'),
            'judge_reason': row.get('judge_reason'),
            'judge_error_type': row.get('judge_error_type'),
            'judge_raw': row.get('judge_raw'),
            'error': row.get('error'),
            'reask': reask,
            'hit_at_20': reask.get('hit_at_20'),
            'review_request_id': reask.get('review_request_id'),
            'review_rag_answer': reask.get('review_rag_answer'),
        }
        (case_dir / 'meta.json').write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        review_rows.append({
            'request_id': rid,
            'file_key': row.get('file_key'),
            'correct': row.get('correct'),
            'hit_at_20': reask.get('hit_at_20'),
            'review_request_id': reask.get('review_request_id'),
            'review_rag_answer': reask.get('review_rag_answer'),
            'error': reask.get('error') or row.get('error'),
        })

        judge = row.get('judge') or {}
        case_md = [
            f'# Review `{rid}`',
            '',
            f"- 原评测 correct(answer_acc): **{row.get('correct')}**",
            f"- 复核 hit_at_20: **{reask.get('hit_at_20')}**"
            if reask else '- 复核 hit_at_20: （未重新提问）',
            f"- judge_error_type: `{row.get('judge_error_type') or judge.get('error_type')}`",
            f"- file_key: `{row.get('file_key')}`",
            f"- review_request_id: `{reask.get('review_request_id')}`",
            '',
            '## 问题',
            '',
            row.get('query') or '',
            '',
            '## 金标',
            '',
            row.get('gold') or '',
            '',
            '## 原评测 RAG 回答',
            '',
            row.get('rag_answer') or '',
            '',
            '## 复核重新提问的 RAG 回答（英文提示）',
            '',
            reask.get('review_rag_answer') or '（未重新提问）',
            '',
            '## 原裁判分析',
            '',
            f"- gold_summary: {judge.get('gold_summary')}",
            f"- answer_summary: {judge.get('answer_summary')}",
            f"- gap: {judge.get('gap')}",
            f"- reason: {judge.get('reason') or row.get('judge_reason')}",
            '',
            '## 裁判原始全文',
            '',
            '```',
            row.get('judge_raw') or judge.get('raw') or '',
            '```',
            '',
            '## 复核检索 top 摘要（用于 hit@20）',
            '',
        ]
        for j, c in enumerate(reask.get('retrieved') or [], start=1):
            case_md.append(
                f"### {j}. file_id={c.get('file_id')} chunk={c.get('chunk_index')} "
                f"score={c.get('score')} source={c.get('source')}"
            )
            case_md.append('')
            case_md.append(c.get('preview') or '')
            case_md.append('')
        (case_dir / 'case.md').write_text('\n'.join(case_md), encoding='utf-8')

        events = filter_events_by_request_id(all_events, rid)
        (case_dir / 'trace.json').write_text(
            json.dumps(events, ensure_ascii=False, indent=2, default=str),
            encoding='utf-8',
        )
        (case_dir / 'rrf_final_chunks.md').write_text(
            _chunks_md(events), encoding='utf-8'
        )

        index_lines.append(
            f"| {i} | `{rid}` | `{row.get('file_key')}` | "
            f"{(not row.get('correct'))} | {reask.get('hit_at_20')} | "
            f"`{reask.get('review_request_id') or '-'}` |"
        )
        print(
            f"[{i}/{len(failures)}] {rid} "
            f"correct={row.get('correct')} hit@20={reask.get('hit_at_20')} "
            f"events={len(events)}"
        )

    index_lines.append('')
    if hit_total:
        rate = round(hit_true / hit_total, 4)
        index_lines.extend([
            '## hit@20（仅失败题复核重问）',
            '',
            f'- n={hit_total}',
            f'- hit_at_20={rate} ({hit_true}/{hit_total})',
            '',
        ])
    (out_dir / 'index.md').write_text('\n'.join(index_lines), encoding='utf-8')

    with (out_dir / 'review_results.jsonl').open('w', encoding='utf-8') as f:
        for r in review_rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + '\n')

    summary = {
        'failure_count': len(failures),
        'reask': not args.skip_reask,
        'top_k': args.top_k,
        'hit_at_20': {
            'n': hit_total,
            'hits': hit_true,
            'rate': round(hit_true / hit_total, 4) if hit_total else None,
        },
    }
    (out_dir / 'review_summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    print(f'完成: {out_dir / "index.md"}')
    print(f'hit@20 summary: {summary["hit_at_20"]}')


if __name__ == '__main__':
    main()
