"""
ContractNLI 风格 RAG 评测主脚本。

只做：RAG 回答（脚本侧要求英文）+ LLM 语义裁判（金标 vs 回答）。
不做 hit@20；检索命中率放到 review_failures 复核时再算。

示例（在 backend 目录下）:
  python -m test_code.eval_rag --dataset test_artifacts/contractnli.json --user-id 1 --out test_artifacts/eval_runs/run1 --limit 50
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

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from dotenv import load_dotenv

load_dotenv(_BACKEND_ROOT.parent / '.env')
load_dotenv(_BACKEND_ROOT / '.env')

from langchain_core.messages import HumanMessage, SystemMessage

from app.core import rag_trace
from app.core.llm_utils import make_dashscope_chat, normalize_llm_text
from app.core.rag import rag_service
from app.core.retrieval_pipeline import _retrieve_for_single_query
from app.models.file import FileMetadataModel
from test_code.eval_lib import (
    build_filename_to_file_id,
    gold_answers_and_file_key,
    load_eval_json,
    resolve_file_id,
)

# 仅改脚本提问文案，不改 RAG 内核
EN_ANSWER_HINT = 'Please answer in English.'


def with_english_answer_hint(query: str) -> str:
    q = (query or '').strip()
    if not q:
        return EN_ANSWER_HINT
    if EN_ANSWER_HINT.lower() in q.lower():
        return q
    return f'{q}\n\n{EN_ANSWER_HINT}'


def _judge_chat():
    model = (os.getenv('DASHSCOPE_FAST_MODEL') or 'qwen-turbo').strip()
    return make_dashscope_chat(model=model, temperature=0.0)


def llm_judge_answer(gold: str, rag_answer: str, *, query: str = '') -> Dict[str, Any]:
    """
    只根据「金标 vs RAG 回答」做语义判断（可带问题作语境，不传检索片段）。
    """
    system = SystemMessage(content=(
        "你是 RAG 评测裁判。只根据「标准答案（金标证据）」与「系统回答（RAG）」判断是否命中。\n"
        "判定标准（从宽、以覆盖为准）：\n"
        "- correct=true：系统回答已经包含金标的关键信息/要点即可"
        "（允许改述、扩写、语序不同；不要求逐字相同；多答一些不相关内容也可，只要关键点在）。\n"
        "- correct=false：系统回答基本未覆盖金标要点、与金标矛盾、或明确声称找不到信息。\n"
        "不要因为措辞不同、详略不同、或回答比金标更长而判错。\n"
        "error_type 取其一：none | partial | contradiction | irrelevant | not_found | other\n"
        "只输出一个 JSON（不要 markdown）：\n"
        "{\n"
        '  "correct": true或false,\n'
        '  "error_type": "none|partial|contradiction|irrelevant|not_found|other",\n'
        '  "gold_summary": "金标关键结论",\n'
        '  "answer_summary": "系统回答中与金标相关的部分",\n'
        '  "gap": "未覆盖点；正确则写「无」",\n'
        '  "reason": "判定理由"\n'
        "}"
    ))
    human_parts = []
    if query.strip():
        human_parts.append(f"用户问题（仅供语境）:\n{query.strip()}")
    human_parts.append(f"标准答案/金标:\n{gold}")
    human_parts.append(f"系统回答:\n{rag_answer}")
    human = HumanMessage(content='\n\n'.join(human_parts) + '\n')

    empty = {
        'correct': False,
        'error_type': 'other',
        'gold_summary': '',
        'answer_summary': '',
        'gap': '',
        'reason': '',
        'raw': '',
    }
    try:
        chat = _judge_chat()
        resp = chat.invoke([system, human])
        text = normalize_llm_text(resp.content)
        empty['raw'] = text
        start = text.find('{')
        end = text.rfind('}')
        if start < 0 or end <= start:
            empty['reason'] = '裁判输出无法解析为 JSON'
            return empty
        data = json.loads(text[start:end + 1])
        return {
            'correct': bool(data.get('correct')),
            'error_type': str(data.get('error_type') or 'other').strip(),
            'gold_summary': str(data.get('gold_summary') or '').strip(),
            'answer_summary': str(data.get('answer_summary') or '').strip(),
            'gap': str(data.get('gap') or '').strip(),
            'reason': str(data.get('reason') or '').strip(),
            'raw': text,
        }
    except Exception as e:
        empty['reason'] = f'裁判调用失败: {e}'
        empty['raw'] = str(e)
        return empty


def run_one(
    *,
    user_id: int,
    query: str,
    gold: str,
    file_ids: Optional[List[int]],
) -> Dict[str, Any]:
    request_id = rag_trace.new_request_id()
    rag_trace.set_trace_context(request_id, user_id=user_id, session_id=f'eval-{request_id}')
    answer_k = int(os.getenv('ANSWER_TOP_K', '10'))
    query_for_gen = with_english_answer_hint(query)
    row: Dict[str, Any] = {
        'request_id': request_id,
        'query': query,
        'query_sent_to_generator': query_for_gen,
        'gold': gold,
        'file_ids': file_ids,
    }
    try:
        rag_trace.emit(
            'request_start',
            data={
                'query': rag_trace.truncate_text(query),
                'mode': 'eval',
                'file_ids': file_ids,
                'english_answer': True,
            },
        )
        # 检索仍用原问题；生成时用带英文作答提示的问题（仅脚本侧）
        fused = _retrieve_for_single_query(user_id, query, file_ids)
        gen_chunks = fused[:answer_k]
        if not gen_chunks:
            rag_answer = (
                'Sorry, I could not find relevant information in your documents. '
                'Please confirm the files are uploaded and processed, or rephrase the question.'
            )
            row['skip_answer'] = True
        else:
            rag_answer = rag_service.generate_answer(
                query_for_gen, gen_chunks, history=None
            )
            row['skip_answer'] = False
        row['rag_answer'] = rag_answer

        judge = llm_judge_answer(gold, rag_answer, query=query)
        row['correct'] = bool(judge.get('correct'))
        row['judge'] = judge
        row['judge_reason'] = judge.get('reason') or ''
        row['judge_error_type'] = judge.get('error_type') or ''
        row['judge_raw'] = judge.get('raw') or ''

        rag_trace.finish(
            status='ok',
            query=rag_trace.truncate_text(query),
            correct=row['correct'],
            judge_error_type=row['judge_error_type'],
            answer_preview=rag_trace.truncate_text(rag_answer),
        )
        return row
    except Exception as e:
        row['error'] = str(e)
        row['correct'] = False
        row['rag_answer'] = ''
        row['judge'] = {
            'correct': False,
            'error_type': 'other',
            'reason': f'运行错误: {e}',
            'raw': str(e),
        }
        row['judge_reason'] = f'运行错误: {e}'
        row['judge_error_type'] = 'other'
        row['judge_raw'] = str(e)
        rag_trace.finish(status='error', message=str(e), error_type=type(e).__name__)
        return row
    finally:
        rag_trace.clear_trace_context()


def _export_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """导出用精简结构：去掉重复字段与裁判 raw，方便人眼阅读。"""
    judge = row.get('judge') or {}
    out: Dict[str, Any] = {
        'index': row.get('index'),
        'request_id': row.get('request_id'),
        'correct': row.get('correct'),
        'error_type': row.get('judge_error_type') or judge.get('error_type'),
        'file_key': row.get('file_key'),
        'file_id': row.get('file_id'),
        'file_mapped': row.get('file_mapped'),
        'latency_ms': row.get('latency_ms'),
        'query': row.get('query'),
        'gold': row.get('gold'),
        'rag_answer': row.get('rag_answer'),
        'judge': {
            'correct': judge.get('correct'),
            'error_type': judge.get('error_type'),
            'gold_summary': judge.get('gold_summary'),
            'answer_summary': judge.get('answer_summary'),
            'gap': judge.get('gap'),
            'reason': judge.get('reason') or row.get('judge_reason'),
        },
        'file_ids': row.get('file_ids'),
        'skip_answer': row.get('skip_answer'),
    }
    if row.get('error'):
        out['error'] = row.get('error')
    return out


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(_export_row(row), ensure_ascii=False, default=str) + '\n')


def _write_results_json(path: Path, rows: List[Dict[str, Any]]) -> None:
    payload = [_export_row(r) for r in rows]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n',
        encoding='utf-8',
    )


def _results_md(rows: List[Dict[str, Any]]) -> str:
    """人眼可读的总览：表格 + 每题问答摘要。"""
    n = len(rows)
    n_ok = sum(1 for r in rows if r.get('correct'))
    lines = [
        '# RAG Eval Results',
        '',
        f'- n={n}  correct={n_ok}  answer_acc={round(n_ok / n, 4) if n else 0.0}',
        '',
        '| # | correct | error_type | file_key | latency_ms | request_id |',
        '|---:|:---:|:---|:---|---:|:---|',
    ]
    for r in rows:
        lines.append(
            f"| {r.get('index')} | {r.get('correct')} | "
            f"`{r.get('judge_error_type') or (r.get('judge') or {}).get('error_type') or '-'}` | "
            f"`{r.get('file_key') or '-'}` | {r.get('latency_ms')} | "
            f"`{r.get('request_id')}` |"
        )
    lines.extend(['', '---', ''])

    for r in rows:
        judge = r.get('judge') or {}
        lines.extend([
            f"## [{r.get('index')}] correct={r.get('correct')} · `{r.get('file_key')}`",
            '',
            f"- request_id: `{r.get('request_id')}`",
            f"- error_type: `{r.get('judge_error_type') or judge.get('error_type')}`",
            f"- latency_ms: {r.get('latency_ms')}",
            '',
            '### Query',
            '',
            r.get('query') or '',
            '',
            '### Gold',
            '',
            r.get('gold') or '',
            '',
            '### RAG Answer',
            '',
            r.get('rag_answer') or '',
            '',
            '### Judge',
            '',
            f"- reason: {judge.get('reason') or r.get('judge_reason') or '（无）'}",
            f"- gap: {judge.get('gap') or '（无）'}",
            '',
            '---',
            '',
        ])
    return '\n'.join(lines)


def _aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    def stats(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
        n = len(subset)
        if n == 0:
            return {'n': 0, 'answer_acc': 0.0}
        acc = sum(1 for r in subset if r.get('correct')) / n
        return {'n': n, 'answer_acc': round(acc, 4)}

    by_file: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_file[r.get('file_key') or '_unknown'].append(r)

    per_file = {
        fk: stats(items)
        for fk, items in sorted(by_file.items(), key=lambda x: (-len(x[1]), x[0]))
    }
    return {
        'overall': stats(rows),
        'per_file': per_file,
        'failure_count': sum(
            1 for r in rows if (not r.get('correct')) or r.get('error')
        ),
    }


def _summary_md(summary: Dict[str, Any]) -> str:
    overall = summary['overall']
    lines = [
        '# RAG Eval Summary',
        '',
        f"- overall n={overall['n']}",
        f"- answer_acc={overall['answer_acc']}  （LLM：金标 vs RAG 回答）",
        f"- failures={summary['failure_count']}",
        '',
        '> hit@20 不在主评测里计算；对失败题运行 `review_failures` 时会重新提问并写入该指标。',
        '',
        '## Per file',
        '',
        '| file_key | n | answer_acc |',
        '|---|---:|---:|',
    ]
    for fk, st in summary['per_file'].items():
        lines.append(f"| {fk} | {st['n']} | {st['answer_acc']} |")
    lines.append('')
    return '\n'.join(lines)


def _case_md(row: Dict[str, Any]) -> str:
    judge = row.get('judge') or {}
    lines = [
        f"# Case `{row.get('request_id')}`",
        '',
        f"- index: {row.get('index')}",
        f"- file_key: `{row.get('file_key')}`",
        f"- file_id: {row.get('file_id')}",
        f"- file_mapped: {row.get('file_mapped')}",
        f"- correct: **{row.get('correct')}**",
        f"- error_type: `{row.get('judge_error_type') or judge.get('error_type')}`",
        f"- latency_ms: {row.get('latency_ms')}",
        '',
        '## Query',
        '',
        row.get('query') or '',
        '',
        '## Gold',
        '',
        row.get('gold') or '',
        '',
        '## RAG Answer',
        '',
        row.get('rag_answer') or '',
        '',
        '## Judge',
        '',
        f"- correct: {judge.get('correct')}",
        f"- error_type: {judge.get('error_type')}",
        f"- gold_summary: {judge.get('gold_summary') or '（无）'}",
        f"- answer_summary: {judge.get('answer_summary') or '（无）'}",
        f"- gap: {judge.get('gap') or '（无）'}",
        f"- reason: {judge.get('reason') or row.get('judge_reason') or '（无）'}",
        '',
    ]
    if row.get('error'):
        lines.extend(['## Error', '', str(row.get('error')), ''])
    return '\n'.join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description='ContractNLI 风格 RAG 评测（答案语义准确率）')
    parser.add_argument('--dataset', required=True, help='评测 JSON 路径')
    parser.add_argument('--user-id', type=int, required=True)
    parser.add_argument('--out', required=True, help='输出目录')
    parser.add_argument('--limit', type=int, default=0, help='最多评测题数，0=全部')
    parser.add_argument('--offset', type=int, default=0, help='跳过前 N 题')
    parser.add_argument('--sleep', type=float, default=0.0, help='每题间隔秒数')
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    if not dataset_path.is_absolute():
        dataset_path = (_BACKEND_ROOT / dataset_path).resolve()

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = (_BACKEND_ROOT / out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    tests = load_eval_json(dataset_path)
    if args.offset:
        tests = tests[args.offset:]
    if args.limit and args.limit > 0:
        tests = tests[:args.limit]

    user_files = FileMetadataModel.get_files_by_user(args.user_id, page=1, size=10000)
    mapping = build_filename_to_file_id(user_files)
    print(f'用户 {args.user_id} 已映射文件名 {len(mapping)} 个键 / 原始记录 {len(user_files)}')

    config = {
        'dataset': str(dataset_path),
        'user_id': args.user_id,
        'limit': args.limit,
        'offset': args.offset,
        'english_answer_hint': EN_ANSWER_HINT,
        'answer_model': os.getenv('DASHSCOPE_ANSWER_MODEL'),
        'judge_model': os.getenv('DASHSCOPE_FAST_MODEL'),
        'metrics': ['answer_acc'],
        'note': 'hit@20 在 review_failures 复核时计算',
        'started_at': datetime.now().isoformat(timespec='seconds'),
        'mapped_keys': len(mapping),
        'n_tests': len(tests),
    }
    (out_dir / 'config.json').write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8'
    )

    results: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    cases_dir = out_dir / 'cases'
    cases_dir.mkdir(parents=True, exist_ok=True)

    for i, test in enumerate(tests, start=1):
        query = (test.get('query') or '').strip()
        gold, file_key, _snippets = gold_answers_and_file_key(test)
        file_id = resolve_file_id(file_key, mapping)
        file_mapped = file_id is not None
        file_ids = [file_id] if file_id is not None else None

        print(f'[{i}/{len(tests)}] file={file_key or "-"} mapped={file_mapped} q={query[:60]}...')
        t0 = time.perf_counter()
        row = run_one(
            user_id=args.user_id,
            query=query,
            gold=gold,
            file_ids=file_ids,
        )
        row['index'] = args.offset + i - 1
        row['file_key'] = file_key
        row['file_mapped'] = file_mapped
        row['file_id'] = file_id
        row['latency_ms'] = round((time.perf_counter() - t0) * 1000, 2)
        results.append(row)

        rid = str(row.get('request_id') or f'idx-{row["index"]}')
        (cases_dir / f'{rid}.md').write_text(_case_md(row), encoding='utf-8')
        (cases_dir / f'{rid}.json').write_text(
            json.dumps(_export_row(row), ensure_ascii=False, indent=2, default=str) + '\n',
            encoding='utf-8',
        )

        failed = (not row.get('correct')) or bool(row.get('error'))
        if failed:
            failures.append(row)

        print(
            f"  correct={row.get('correct')} type={row.get('judge_error_type')} "
            f"rid={row.get('request_id')} ({row['latency_ms']}ms)"
        )
        if args.sleep > 0:
            time.sleep(args.sleep)

    _write_jsonl(out_dir / 'results.jsonl', results)
    _write_jsonl(out_dir / 'failures.jsonl', failures)
    _write_results_json(out_dir / 'results.json', results)
    (out_dir / 'results.md').write_text(_results_md(results), encoding='utf-8')
    summary = _aggregate(results)
    summary['finished_at'] = datetime.now().isoformat(timespec='seconds')
    (out_dir / 'summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    (out_dir / 'summary.md').write_text(_summary_md(summary), encoding='utf-8')

    print('\n=== DONE ===')
    print(json.dumps(summary['overall'], ensure_ascii=False))
    print(f'结果总览: {out_dir / "results.md"}  （优先看这个）')
    print(f'结果 JSON: {out_dir / "results.json"}  （缩进，好读）')
    print(f'结果 jsonl: {out_dir / "results.jsonl"} （精简，给脚本用）')
    print(f'失败列表: {out_dir / "failures.jsonl"} ({len(failures)} 题)')
    print(f'每题报告: {cases_dir}/<request_id>.md')
    print(f'汇总:     {out_dir / "summary.md"}')
    print('失败题复核（会重新提问并计算 hit@20）:')
    print(
        f'  python -m test_code.review_failures '
        f'--failures {out_dir / "failures.jsonl"} '
        f'--user-id {args.user_id} '
        f'--trace logs/rag_trace.jsonl '
        f'--out {out_dir / "review"}'
    )


if __name__ == '__main__':
    main()
