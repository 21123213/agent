# RAG Compare Summary（baseline vs full）

- generated_at: `2026-10-28T23:05:14`
- dataset: `test\contractnli_1.json`
- unit_id=1 kb_id=3 user_id=1
- pipelines: `['baseline', 'full']`
- hit_k: `10`
- full weights: vector=`0.4` bm25=`0.6`
- n_rows: **288**

## 分目录（both 时按流水线拆开）

- **baseline** → `baseline/`（summary / results / cases）
- **full** → `full/`（summary / results / cases）

## 按流水线均值

| pipeline | n | overall↑ | F | C | A | gold_cov | hit@K↑ | avg_ms |
|----------|---|----------|---|---|---|----------|--------|--------|
| baseline | 144 | 0.9183 | 0.9208 | 0.8688 | 0.9778 | 0.9326 | 0.9514 | 10892.6524 |
| full | 144 | 0.8629 | 0.8986 | 0.7917 | 0.9076 | 0.7243 | 0.7708 | 21118.7528 |

## 怎么读

- **hit@K**：金标文本是否出现在检索 top-K 切片中（有金标才计；覆盖即可）。
- **Faithfulness / Context / Answer**：LLM Judge 三要素。
- **Gold Coverage**：回答相对金标要点（从宽，语义覆盖）。
- 面试对照：baseline vs full 看 overall、hit@K、延迟差。
