# RAG Summary — baseline

- generated_at: ``
- dataset: ``
- unit_id= kb_id= user_id=
- pipelines: `['baseline']`
- hit_k: `10`
- full weights: vector=`None` bm25=`None`
- n_rows: **144**

## 按流水线均值

| pipeline | n | overall↑ | F | C | A | gold_cov | hit@K↑ | avg_ms |
|----------|---|----------|---|---|---|----------|--------|--------|
| baseline | 144 | 0.9183 | 0.9208 | 0.8688 | 0.9778 | 0.9326 | 0.9514 | 10892.6524 |

## 怎么读

- **hit@K**：金标文本是否出现在检索 top-K 切片中（有金标才计；覆盖即可）。
- **Faithfulness / Context / Answer**：LLM Judge 三要素。
- **Gold Coverage**：回答相对金标要点（从宽，语义覆盖）。
- 面试对照：baseline vs full 看 overall、hit@K、延迟差。
