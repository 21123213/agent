# RAG Summary — full

- generated_at: ``
- dataset: ``
- unit_id= kb_id= user_id=
- pipelines: `['full']`
- hit_k: `10`
- full weights: vector=`0.4` bm25=`0.6`
- n_rows: **144**

## 按流水线均值

| pipeline | n | overall↑ | F | C | A | gold_cov | hit@K↑ | avg_ms |
|----------|---|----------|---|---|---|----------|--------|--------|
| full | 144 | 0.8629 | 0.8986 | 0.7917 | 0.9076 | 0.7243 | 0.7708 | 21118.7528 |

## 怎么读

- **hit@K**：金标文本是否出现在检索 top-K 切片中（有金标才计；覆盖即可）。
- **Faithfulness / Context / Answer**：LLM Judge 三要素。
- **Gold Coverage**：回答相对金标要点（从宽，语义覆盖）。
- 面试对照：baseline vs full 看 overall、hit@K、延迟差。
