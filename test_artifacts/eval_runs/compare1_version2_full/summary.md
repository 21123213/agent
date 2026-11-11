# RAG Compare Summary（baseline vs full）

- generated_at: `2026-10-29T12:05:33`
- dataset: `test\contractnli_1.json`
- unit_id=1 kb_id=3 user_id=1
- pipelines: `['full']`
- hit_k: `12`
- full weights: vector=`0.4` bm25=`0.6`
- n_rows: **12**

## 按流水线均值

| pipeline | n | overall↑ | F | C | A | gold_cov | hit@K↑ | avg_ms |
|----------|---|----------|---|---|---|----------|--------|--------|
| full | 12 | 0.9167 | 0.9417 | 0.8667 | 0.9417 | 0.8417 | 0.8333 | 17899.7125 |

## 怎么读

- **hit@K**：金标文本是否出现在检索 top-K 切片中（有金标才计；覆盖即可）。
- **Faithfulness / Context / Answer**：LLM Judge 三要素。
- **Gold Coverage**：回答相对金标要点（从宽，语义覆盖）。
- 面试对照：baseline vs full 看 overall、hit@K、延迟差。
