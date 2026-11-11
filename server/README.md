# server — Agents 后端

产品名：**Agents**。参考 `backend/` 只读；新代码只写本目录。

专题方案：

- 架构定稿：[`docs/architecture-decisions.md`](../docs/architecture-decisions.md)
- **RAG 问答 / 会话记忆 / 长期记忆 / QA 缓存（已定稿）**：[`docs/rag-chat-memory-plan.md`](../docs/rag-chat-memory-plan.md)
- **智能问数 AskData（已定稿）**：[`docs/askdata-plan.md`](../docs/askdata-plan.md)、口径 [`docs/askdata-metrics.md`](../docs/askdata-metrics.md)
- **Reasoner 观测（transcript/trace / OTel / LangSmith）**：[`docs/reasoner-observability.md`](../docs/reasoner-observability.md)
- 目录规划：[`structure.md`](../structure.md)

## 启动

仓库根已 `uv sync` 后：

```cmd
cd /d d:\agent
.venv\Scripts\activate.bat
uvicorn app.main:app --reload --app-dir server --host 0.0.0.0 --port 8000
```

轮询类接口（`/status`、`/chunks`、`/api/files/list`）默认不写 uvicorn access 日志；改完 `main.py` 后需重启 uvicorn。

Celery Worker（上传向量化 + **后续用户记忆抽取**；Windows 用 solo；**需本机 Redis**）：

```cmd
cd /d d:\agent
.venv\Scripts\activate.bat
set PYTHONPATH=server
celery -A app.celery_app.celery_app worker --loglevel=info --pool=solo
```

若本机 **没有 Redis**：上传会回退到进程内线程跑 `process_file`。问答会话窗口与 QA 缓存依赖 Redis，联调问答请启动 Redis。

说明：`txt`/`md` 轻量解析；`pdf`/`docx` 用 Unstructured。列表知识库为空时会自动创建「默认知识库」。

文档：http://127.0.0.1:8000/docs  
健康检查：`GET /health`

## 本阶段已实现

### 认证

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册；默认 unit=1、role=4（viewer） |
| POST | `/api/auth/login` | JWT |
| POST | `/api/auth/reset-password` | 用户名+手机 |
| GET | `/api/auth/me` | 当前用户 |
| POST | `/api/auth/logout` | 登出 |

### 知识库（`/api/kb`）

| 方法 | 路径 | 权限 |
|------|------|------|
| GET | `/api/kb` | 同 unit 列表 |
| GET | `/api/kb/capabilities` | 前端显隐 |
| POST | `/api/kb` | 仅 admin 创建 |
| DELETE | `/api/kb/{id}` | 仅 admin |

### 文件入库到向量化（`/api/files`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/files/upload?knowledge_base_id=` | 多文件 → MySQL → Celery |
| GET | `/api/files/list` | 按 KB 列表 |
| GET | `/api/files/{id}/status` | 详情轮询（实时切片） |
| GET | `/api/files/{id}/chunks` | 切片 |
| DELETE | `/api/files/{id}` | 删 DB + 向量 |

### 检索 / 评测（代码已有，非 HTTP 问答）

- `agents/rag/pipeline/retrieve_full.py`：线上正式检索链路。
- `retrieve_baseline.py`：仅评测对照。
- `common/llm_client.py`：模型路由；`test_code/eval_compare.py`：三要素 + hit@K。

### RAG 问答 / 会话 / 记忆（`/api/rag`）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/rag/chat/stream` | SSE：`meta`/`status`/`delta`/`done`/`error`；必带 `knowledge_base_id` |
| GET | `/api/rag/sessions?knowledge_base_id=` | 当前用户在该 KB 下会话 |
| POST | `/api/rag/sessions` | 新建会话（欢迎语提示 KB 名） |
| GET | `/api/rag/history/{session_id}` | 消息历史 |
| DELETE | `/api/rag/session/{session_id}` | 删会话 + 清 Redis 窗口 |
| DELETE | `/api/rag/clear?knowledge_base_id=` | 清空该 KB 下本用户会话 |

配套：unit 级 QA 缓存（`cache_hit`）；会话 3 轮+摘要；长期记忆 Celery 抽取（`USER_MEMORY_ENABLED` 默认开）。

### 推理 Agent（`/api/reasoner`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/reasoner/sessions` | 会话列表 / 新建（无 KB） |
| GET | `/api/reasoner/history/{id}` | 历史 |
| DELETE | `/api/reasoner/session/{id}` | 删会话 + 清 Redis checkpoint/skills |
| POST | `/api/reasoner/chat/stream` | SSE：`meta`/`status`/`delta`/`needs_input`/`done`/`error` |

LangGraph + **tools_exec 并行/分层** + **interrupt** 人工补充；Redis checkpointer（无内存 PendingStore）。需本机 Redis。  
旅游 skill + `web_search`（博查）已挂载；密钥仅环境变量；道旅 MCP 见 `docs/reasoner-travel-tools.md`。设计：`docs/reasoner-agent-plan.md`。

### 观测落盘（Reasoner / AskData）

`rag_trace.jsonl` **已停写**；阶段埋点走 OTel / LangSmith，请在对应页面查看与导出。  
LangGraph 请求仍可落 **transcript + trace** 双文件（schema=`reasoner.run_artifact.v1`）；**不落 metrics JSON**。

```text
logs/reasoner/
  transcript/YYYYMMDD/HHMMSS_<request12>_<session12>.json   # phase 叙事
  trace/YYYYMMDD/HHMMSS_<request12>_<session12>.json        # 本地 Span 树（非 OTel ID）
```

开关：`REASONER_ARTIFACTS_ENABLED`（默认 true）、`REASONER_ARTIFACTS_DIR`（默认 `logs/reasoner`）。  
设计与 OTel/LangSmith 关系：[`docs/reasoner-observability.md`](../docs/reasoner-observability.md)。

### 智能问数（`/api/askdata`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/askdata/sessions` | 会话列表 / 新建 |
| GET | `/api/askdata/history/{id}` | 历史 |
| DELETE | `/api/askdata/session/{id}` | 删会话 |
| POST | `/api/askdata/chat/stream` | SSE：指标优先 / NL2SQL + sql_guard |

DDL：`sql/003_biz_askdata.sql` + `seed_askdata.sql`（订单中心精简：约 9 张业务表 + 日汇总 VIEW；无仓配/促销/组织维）。口径/ER：`docs/askdata-metrics.md`、`docs/askdata-er.md`。评测：`test_code/askdata_eval.py`。前端问数 Tab 含业务表关系图。

### 用户管理（`/api/admin`，仅 admin）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/admin/capabilities` | `{ is_admin }` |
| GET | `/api/admin/users` `/roles` `/units` | 列表 |
| PATCH | `/api/admin/users/{id}` | 改 role / unit / is_active |

旧 stub `/api/chat`、`/api/agent` 仍 501；新业务走 **`/api/rag`** / **`/api/reasoner`** / **`/api/askdata`**。

## 联调注意

1. **请求必带** `knowledge_base_id`；可选 `vector_weight` / `bm25_weight`。
2. **换 KB = 换会话**；新建会话提示当前知识库名称。
3. 正式问答只用 **`retrieve_full`**；LCEL 编排，LangSmith 看分步耗时。
4. QA 缓存按 **`unit_id` + kb + 权重 + 规范化问句**（非 user、非语义近邻首版）。
5. `chat_sessions.use_history`：**业务忽略**。
6. 问答会话窗口与 QA 缓存依赖 **Redis**；Celery Worker 需跑着才能异步抽长期记忆。
7. Milvus Lite：同库文件勿多开进程；连接已加锁 + `ensure_ready`。生产用 standalone。

## 基础设施约定

- DB：连接池 `DB_POOL_MAXSIZE=8`，线程池 `DB_THREAD_WORKERS=12`；Celery 独立池。
- Milvus 文档 Collection：`kb_document_vectors`；记忆：`user_memory_vectors`。
- SQL 一律 `%s` + params。
- LangSmith：`.env` 中 `LANGCHAIN_TRACING_V2` + `LANGCHAIN_API_KEY`。

## 数据库

空库：`server/sql/001_schema_v2.sql`（含 `user_memories`）→ `seed_demo.sql`。  
智能问数：再执行 `003_biz_askdata.sql` → `seed_askdata.sql`。  
已有旧 v2 库只缺记忆表：再执行 `002_add_user_memories.sql`。
