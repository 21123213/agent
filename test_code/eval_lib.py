"""RAG 评测共用工具：数据集、文件映射、hit@K、trace 解析。"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


def load_eval_json(path: str | Path) -> List[Dict[str, Any]]:
    """加载 ContractNLI 风格数据集，返回 tests 列表。"""
    raw = Path(path).read_text(encoding='utf-8')
    data = json.loads(raw)
    if isinstance(data, dict) and 'tests' in data:
        tests = data['tests']
    elif isinstance(data, list):
        tests = data
    else:
        raise ValueError(f'无法识别数据集结构: {path}')
    if not isinstance(tests, list):
        raise ValueError('tests 必须是数组')
    return tests


def basename_of(file_path: str) -> str:
    return Path(str(file_path).replace('\\', '/')).name


def gold_answers_and_file_key(test: Dict[str, Any]) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    返回 (拼接金标答案, 主源文件 basename, snippets 列表)。
    多文件时取出现次数最多的 basename。
    """
    snippets = test.get('snippets') or []
    answers: List[str] = []
    names: List[str] = []
    for sn in snippets:
        ans = (sn.get('answer') or '').strip()
        if ans:
            answers.append(ans)
        fp = sn.get('file_path') or ''
        if fp:
            names.append(basename_of(fp))
    gold = '\n'.join(answers)
    if not names:
        return gold, '', snippets
    file_key = Counter(names).most_common(1)[0][0]
    return gold, file_key, snippets


def _norm_text(text: str) -> str:
    text = (text or '').lower()
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def hit_at_k(
    gold_answer: str,
    chunks: List[Dict[str, Any]],
    *,
    k: int = 20,
    overlap_threshold: float = 0.45,
) -> bool:
    """
    金标文本是否被 top-k 召回块覆盖。
    命中条件：归一化后互相包含，或较短串在较长串中的字符覆盖率 >= 阈值。
    """
    gold = _norm_text(gold_answer)
    if not gold:
        return False
    for chunk in (chunks or [])[:k]:
        body = _norm_text(chunk.get('content') or chunk.get('chunk_text') or '')
        if not body:
            continue
        if gold in body or body in gold:
            return True
        shorter, longer = (gold, body) if len(gold) <= len(body) else (body, gold)
        if len(shorter) < 12:
            continue
        # 滑动窗口粗算：shorter 有多少字符比例出现在 longer
        hits = sum(1 for i in range(0, len(shorter), 8) if shorter[i:i + 24] in longer)
        windows = max(1, (len(shorter) + 7) // 8)
        if hits / windows >= overlap_threshold:
            return True
    return False


def summarize_retrieved(chunks: List[Dict[str, Any]], limit: int = 3) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for hit in (chunks or [])[:limit]:
        meta = hit.get('metadata') or {}
        text = hit.get('content') or hit.get('chunk_text') or ''
        out.append({
            'id': hit.get('id'),
            'file_id': hit.get('file_id') or meta.get('file_id'),
            'chunk_index': (
                hit.get('chunk_index')
                if hit.get('chunk_index') is not None
                else meta.get('chunk_index')
            ),
            'score': float(hit.get('rrf_score') or hit.get('score') or 0),
            'source': hit.get('source') or meta.get('retrieval') or '',
            'preview': (text[:200] + '…') if len(text) > 200 else text,
        })
    return out


def build_filename_to_file_id(user_files: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    original_filename -> file_id。
    同时登记 basename 与 `__` 后后缀，便于文件夹上传名对齐。
    仅收录 status=processed（若有 status 字段）。
    """
    mapping: Dict[str, int] = {}
    for f in user_files:
        status = (f.get('status') or '').lower()
        if status and status not in ('processed', 'completed', 'done'):
            continue
        fid = int(f['id'])
        original = f.get('original_filename') or f.get('filename') or ''
        if not original:
            continue
        base = basename_of(original)
        mapping[base.lower()] = fid
        mapping[original.lower()] = fid
        if '__' in base:
            mapping[base.split('__')[-1].lower()] = fid
    return mapping


def resolve_file_id(file_key: str, mapping: Dict[str, int]) -> Optional[int]:
    if not file_key:
        return None
    key = file_key.lower()
    if key in mapping:
        return mapping[key]
    # 模糊：任意映射键以 file_key 结尾
    for name, fid in mapping.items():
        if name.endswith(key) or key.endswith(basename_of(name).lower()):
            return fid
    return None


def parse_trace_events(trace_path: str | Path) -> List[Dict[str, Any]]:
    """
    解析 rag_trace.jsonl：
    - 旧格式：每行一个 JSON
    - 新格式：# [stage] ... 后跟多行 JSON 对象
    """
    path = Path(trace_path)
    if not path.exists():
        return []
    text = path.read_text(encoding='utf-8')
    events: List[Dict[str, Any]] = []
    # 先尝试按「# 行 + JSON」块
    blocks = re.split(r'(?m)^# \[', text)
    if len(blocks) > 1:
        for block in blocks[1:]:
            # 恢复被 split 吃掉的前缀，找第一个 {
            brace = block.find('{')
            if brace < 0:
                continue
            # 从第一个 { 起取完整 JSON（可能多行）
            json_part = block[brace:].strip()
            # 若后面还有下一个事件的空行+内容，用括号平衡截取
            obj_text = _extract_first_json_object(json_part)
            if not obj_text:
                continue
            try:
                events.append(json.loads(obj_text))
            except json.JSONDecodeError:
                continue
        if events:
            return events

    # 回退：逐行 JSONL
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _extract_first_json_object(text: str) -> Optional[str]:
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def filter_events_by_request_id(
    events: Iterable[Dict[str, Any]],
    request_id: str,
) -> List[Dict[str, Any]]:
    rid = (request_id or '').strip()
    if not rid:
        return []
    return [e for e in events if str(e.get('request_id') or '') == rid]


def events_for_stage(events: List[Dict[str, Any]], stage: str) -> List[Dict[str, Any]]:
    return [e for e in events if e.get('stage') == stage]
