# RAG 评测怎么用（ContractNLI）

> **新后端对比评测（推荐）**：见同目录 [`EVAL_COMPARE_README.md`](EVAL_COMPARE_README.md)  
> （`server` + baseline/full + 三要素 Judge）。下文为参考 `backend` 旧脚本说明。

在 **`backend` 目录**下执行（先 `cd backend`）。

---

## 两套指标，分开算（省钱）

| 阶段 | 算什么 | 怎么算 |
|------|--------|--------|
| **主评测 `eval_rag`** | **answer_acc** | 只把「标准答案」和「RAG 回答」交给裁判 LLM 做语义对错 |
| **复核 `review_failures`** | **hit@20** | 对失败题**再问一遍**，看检索前 20 条里有没有盖住金标文字 |

主评测**不再**算 hit@20，也**不再**把检索片段塞给裁判（避免又贵又长）。

---

## 你要先准备什么

1. MySQL / Redis / Milvus / DashScope（`.env`）可用。  
2. 评测用的 txt 已上传且「已处理」。  
3. 知道 `user_id`（常见 `1`）。  
4. 上传文件名能对上金标 basename，例如金标  
   `contractnli/CopAcc_NDA-and-ToP-Mentors_2.0_2017.txt`  
   → 库里要有同名或 `xxx__CopAcc_NDA-and-ToP-Mentors_2.0_2017.txt`。

---

## 第一步：主评测（英文作答 + 语义判分）

```bash
cd d:\RAG-FOR-BEGINNERS\backend

python -m test_code.eval_rag ^
  --dataset test_artifacts/contractnli_1.json ^
  --user-id 1 ^
  --out test_artifacts/eval_runs/run1 ^
  --limit 20

python -m test_code.eval_rag ^
  --dataset test_artifacts/contractnli_2.json ^
  --user-id 1 ^
  --out test_artifacts/eval_runs/run2
```

题库已拆成 4 份（结构相同，后缀 `_1`…`_4`）：

| 文件 | 约题数 | 说明 |
|------|--------|------|
| `test_artifacts/contractnli_1.json` | 144 | 已跑完（run1 cases index 0–143） |
| `test_artifacts/contractnli_2.json` | 345 | 含原 `_1` 未跑 101 题 + 原 `_2` |
| `test_artifacts/contractnli_3.json` | 244 | |
| `test_artifacts/contractnli_4.json` | 244 | |

原完整题库仍保留为 `test_artifacts/contractnli.json`（977 题）。

参数：

| 参数 | 含义 |
|------|------|
| `--dataset` | 题库 JSON（可用拆分后的 `_1`…`_4`） |
| `--user-id` | 文档所属用户 |
| `--out` | 本次输出目录 |
| `--limit 20` | 只跑 20 题；全量用 `--limit 0` 或不写 |

脚本会：

1. 用**原问题**做检索；  
2. 生成时在问题后追加一句（**只改脚本，不改 RAG 内核**）：  
   `Please answer in English.`  
3. 裁判 LLM **只看**：「标准答案」vs「RAG 回答」（问题仅作语境）。  
   判分从宽：RAG 回答**包含/覆盖金标要点**即可 `correct=true`，不要求措辞一致。

### 输出目录 `test_artifacts/eval_runs/run1/`

| 文件 | 作用 |
|------|------|
| **`results.md`** | **优先看**：总表 + 每题 Query / Gold / Answer / Judge |
| **`summary.md`** | 全局 / 按文件的 **answer_acc** |
| **`results.json`** | 全量结果（缩进 JSON，好读） |
| `results.jsonl` | 精简每题一行（给脚本 / review 用） |
| `failures.jsonl` | 仅答案判错或报错的题 |
| **`cases/<request_id>.md`** | 单题报告 |
| `config.json` | 本次配置 |

失败条件（主评测）：`correct=false` 或运行报错。  
**不再**因为 hit@20 把题标失败。

---

## 第二步：失败题复核（重新提问 + hit@20）

```bash
cd d:\RAG-FOR-BEGINNERS\backend

python -m test_code.review_failures ^
  --failures test_artifacts/eval_runs/run1/failures.jsonl ^
  --user-id 1 ^
  --trace logs/rag_trace.jsonl ^
  --out test_artifacts/eval_runs/run1/review
```

这一步会对**每道失败题再跑一次 RAG**（同样追加英文作答），并计算：

- **hit@20**：重新检索后的前 20 条里，是否覆盖金标文本  
  - `true`：证据能召到，答案错更像生成/理解问题  
  - `false`：证据根本没进前 20，更像切分/检索问题  

### 复核目录 `review/`

| 路径 | 内容 |
|------|------|
| `index.md` | 失败题表 + 本次失败题的 hit@20 汇总 |
| `review_summary.json` | hit@20 数字汇总 |
| `review_results.jsonl` | 每题复核结果 |
| `<原request_id>/case.md` | 原答 + 复核新答 + 裁判 + 检索摘要 |
| `<原request_id>/meta.json` | 完整字段（含 `hit_at_20`、`review_rag_answer`） |
| `<原request_id>/trace.json` | 原评测那次的 trace |
| `<原request_id>/rrf_final_chunks.md` | 原评测进上下文的切片 |

只导出旧 trace、不重问：

```bash
python -m test_code.review_failures ... --skip-reask
```

---

## 看错题怎么走

1. 打开 `summary.md` → 哪个文件 `answer_acc` 低。  
2. 打开对应 `cases/<id>.md` → 看金标 vs 英文回答，再看 `judge_raw`。  
3. 对失败题跑 `review_failures` → 看 `hit_at_20`：  
   - 召回失败还是生成失败。

---

## 换题库

JSON 根结构保持：

```json
{ "tests": [ { "query": "...", "snippets": [ { "file_path": "...", "span": [0,1], "answer": "..." } ] } ] }
```

改 `--dataset` 路径即可。按 `file_path` 文件名分组统计。

---

## 常见问题

**`file_mapped=false` 很多**  
→ 上传文件名对不上金标 basename。

**答案是中文**  
→ 检查 cases 里「发给生成模型的问题」是否带有 `Please answer in English.`；模型仍可能偶尔不遵守。

**想看检索是否命中**  
→ 主评测不算；对失败题跑第二步复核。

**全量 977 题贵**  
→ 先 `--limit 20`。
