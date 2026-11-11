"""全局配置（pydantic-settings），读取仓库根 .env。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(_REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Agents"
    app_version: str = "0.1.0"
    app_debug: bool = True
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    cors_origins: str = "http://localhost:5173,http://localhost:3000"

    jwt_secret_key: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # 注册默认归属：viewer=4（最低权限）；admin_role_id=1 可管 KB / 用户 / 跨 unit 问数
    default_unit_id: int = 1
    default_role_id: int = 4
    admin_role_id: int = 1

    # ---------- AskData（智能问数）----------
    askdata_max_rows: int = 200
    askdata_query_timeout_sec: float = 15.0

    # ---------- DB 池 / 线程池（架构定稿）----------
    db_pool_maxsize: int = 8
    db_thread_workers: int = 14
    celery_db_pool_maxsize: int = 6

    tdsql_host: str = "127.0.0.1"
    tdsql_port: int = 3306
    tdsql_user: str = "root"
    tdsql_password: str = ""
    tdsql_db: str = "agent"
    tdsql_charset: str = "utf8mb4"
    tdsql_connect_timeout: int = 10
    # 读写超时：避免远端库半开连接把请求卡到数分钟（系统 TCP 超时）
    tdsql_read_timeout: int = 30
    tdsql_write_timeout: int = 30
    tdsql_ssl_ca: str | None = None

    # ---------- Redis / Celery ----------
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_broker_url: str = ""
    celery_result_backend: str = ""

    # ---------- Milvus ----------
    milvus_enabled: bool = True
    milvus_mode: str = "lite"
    milvus_lite_path: str = "./db/milvus_lite.db"
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 19530
    milvus_user: str = ""
    milvus_password: str = ""
    # 新 schema 使用独立 collection，避免与旧版 user_id-only 冲突
    milvus_collection_name: str = "kb_document_vectors"
    milvus_embedding_dim: int = 1024
    embedding_version: str = "v1"

    # ---------- Embedding / Enhance / LLM 路由 ----------
    dashscope_api_key: str = ""
    dashscope_embedding_api_key: str = ""
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_chat_model: str = "qwen-vl-max"
    dashscope_answer_model: str = ""
    # FAST：深度 ReAct 中间步（3.7-max，开思考）
    dashscope_fast_model: str = "qwen3.7-max"
    dashscope_strong_model: str = "qwen3.8-max"
    # QUICK：前端「快速回答」（qwen3.6-flash，关思考）
    # DEEP：深度收口 / Plan 主模型（3.8-max，开思考）
    dashscope_quick_model: str = "qwen3.6-flash"
    dashscope_deep_model: str = "qwen3.8-max"
    dashscope_judge_model: str = ""
    # AskData SQL 校验（默认与 QUICK 同模型，但开思考；可用 DASHSCOPE_VALIDATE_MODEL 覆盖）
    dashscope_validate_model: str = "qwen3.6-flash"
    dashscope_compatible_base: str = (
        "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    dashscope_timeout_sec: float = 90.0
    # 可选第二厂商（降级）
    zhipu_api_key: str = ""
    zhipu_compatible_base: str = "https://open.bigmodel.cn/api/paas/v4"
    zhipu_fast_model: str = "glm-4-flash"
    zhipu_strong_model: str = "glm-4"
    zhipu_enhance_model: str = "glm-4v"
    # 兼容旧配置名；AskData 校验已改走 DashScope validate 模型
    zhipu_validate_model: str = "glm-5.3"
    llm_enhance_mode: str = "off"  # off | mixed | all
    llm_enhance_max_images: int = 3

    # ---------- 检索 / RAG ----------
    retrieve_top_k_per_path: int = 20
    answer_top_k: int = 10
    query_expand_n: int = 3
    bm25_candidate_limit: int = 2000
    bm25_use_jieba: bool = True
    rrf_k: int = 60
    rrf_per_query: int = 15
    rrf_final: int = 15
    # full 混合检索默认：向量 0.4 / BM25 0.6（接口可覆盖）
    rrf_vector_weight: float = 0.4
    rrf_bm25_weight: float = 0.6
    rerank_enabled: bool = False
    rerank_model: str = "gte-rerank-v2"
    rerank_max_doc_chars: int = 6000
    # baseline（快速回答）：向量 15 + BM25 15 → rerank 取 10
    baseline_vector_top_k: int = 15
    baseline_bm25_top_k: int = 15
    baseline_rerank_top_n: int = 10

    # ---------- 会话短记忆（Redis 窗口 + MySQL 全量）----------
    # 最近 N 轮 user+assistant 全文保留；更早压成 [此前对话摘要]
    max_context_turns: int = 3
    max_context_tokens: int = 20000
    context_cache_ttl: int = 1800  # session:{sid}:context TTL 秒

    # ---------- QA 答案缓存（unit 级，与 session 命名空间分离）----------
    rag_qa_cache_enabled: bool = True
    rag_qa_cache_ttl: int = 3600  # 秒

    # ---------- 用户长期记忆 ----------
    user_memory_enabled: bool = True  # 默认开；可用 env 关
    user_memory_extract_every_n: int = 3  # 每 N 轮投递一次抽取任务
    user_memory_max_per_user: int = 50  # 条数上限（淘汰旧的）
    user_memory_top_m: int = 5  # 问答前语义召回条数
    milvus_memory_collection_name: str = "user_memory_vectors"

    # ---------- 推理 Agent（reasoner）----------
    # 默认模式：react | plan_execute
    reasoner_default_mode: str = "react"
    reasoner_max_steps: int = 10
    reasoner_max_replans: int = 2
    # 深度思考默认 8 万；快速回答用 reasoner_token_budget_quick（4 万）
    reasoner_token_budget: int = 80000
    reasoner_token_budget_quick: int = 40000
    reasoner_token_soft_ratio: float = 0.70
    reasoner_token_hard_ratio: float = 0.90
    reasoner_web_search_max: int = 8
    # 知识库 rag_search 单轮上限（产品默认 2）
    reasoner_rag_search_max: int = 2
    # 同批 / 同层工具最大并行数（ReAct 与 PE 同层共用）
    reasoner_max_parallel_tools: int = 5
    # Redis checkpointer / skill 粘滞 TTL（秒）
    reasoner_checkpoint_ttl: int = 86400
    reasoner_skills_ttl: int = 86400
    # 熔断：连续「逻辑调用」失败次数（与 retry 内部 attempt 解耦；env: CIRCUIT_*）
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout_sec: float = 30.0

    # ---------- 可观测 ----------
    langchain_tracing_v2: bool = False
    langchain_api_key: str = ""
    langchain_project: str = "agents"
    # 仅控制台摘要等调试；rag_trace.jsonl 已停写，默认关
    rag_trace_enabled: bool = False
    rag_trace_full: bool = False
    rag_trace_dir: str = "logs"
    otel_exporter_otlp_endpoint: str = ""
    otel_service_name: str = "agents-server"

    # ---------- Upload / parse / chunk ----------
    upload_directory: str = "./uploads"
    upload_max_file_size: int = 52428800
    upload_allowed_extensions: str = ".pdf,.txt,.md,.docx"
    unstructured_pdf_strategy: str = "hi_res"
    unstructured_ocr_languages: str = "chi_sim,eng"
    chunk_max_characters: int = 3000
    chunk_new_after_n_chars: int = 2400
    chunk_combine_text_under_n_chars: int = 500
    chunk_overlap: int = 200
    chunk_overlap_all: bool = False
    hf_endpoint: str = ""

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_extensions(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.upload_allowed_extensions.split(",")
            if e.strip()
        }

    @property
    def effective_celery_broker(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def effective_celery_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def effective_embedding_api_key(self) -> str:
        return self.dashscope_embedding_api_key or self.dashscope_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
