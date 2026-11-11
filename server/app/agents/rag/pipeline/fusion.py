"""RRF（Reciprocal Rank Fusion）融合多路检索结果。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Sequence


def chunk_key(hit: Dict[str, Any]) -> str:
    if hit.get("id"):
        return str(hit["id"])
    return f"{hit.get('file_id')}_{hit.get('chunk_index')}"


def rrf_fuse(
    ranked_lists: List[List[Dict[str, Any]]],
    top_n: int = 15,
    k: int = 60,
    key_fn: Callable[[Dict[str, Any]], str] = chunk_key,
    weights: Optional[Sequence[float]] = None,
) -> List[Dict[str, Any]]:
    if not ranked_lists:
        return []

    n = len(ranked_lists)
    if weights is None:
        ws = [1.0] * n
    else:
        ws = [float(w) for w in weights]
        if len(ws) < n:
            ws.extend([1.0] * (n - len(ws)))
        elif len(ws) > n:
            ws = ws[:n]
        if sum(max(0.0, w) for w in ws) <= 0:
            ws = [1.0] * n
        else:
            ws = [max(0.0, w) for w in ws]

    scores: Dict[str, float] = {}
    best_hit: Dict[str, Dict[str, Any]] = {}

    for ranked, weight in zip(ranked_lists, ws):
        if not ranked or weight <= 0:
            continue
        for rank, hit in enumerate(ranked, start=1):
            key = key_fn(hit)
            rrf_raw = 1.0 / (k + rank)
            scores[key] = scores.get(key, 0.0) + weight * rrf_raw
            if key not in best_hit:
                best_hit[key] = hit

    ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    results: List[Dict[str, Any]] = []
    for key, rrf_score in ordered[:top_n]:
        item = dict(best_hit[key])
        item["rrf_score"] = rrf_score
        results.append(item)
    return results
