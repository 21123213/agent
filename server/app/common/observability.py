"""启动时把 Settings 同步到 LangSmith / OTel / rag_trace 相关环境变量。"""
from __future__ import annotations

import os

from app.common.tracing import setup_otel
from app.settings import get_settings


def bootstrap_observability() -> None:
    s = get_settings()
    # 熔断阈值：Settings / CIRCUIT_* env → 全局 registry
    try:
        from app.common.circuit_breaker import configure_circuit_registry_from_settings

        configure_circuit_registry_from_settings()
    except Exception:
        pass
    if s.langchain_tracing_v2:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
    if s.langchain_api_key and not os.getenv("LANGCHAIN_API_KEY"):
        os.environ["LANGCHAIN_API_KEY"] = s.langchain_api_key
    if s.langchain_project:
        os.environ.setdefault("LANGCHAIN_PROJECT", s.langchain_project)
    if s.otel_exporter_otlp_endpoint:
        os.environ.setdefault(
            "OTEL_EXPORTER_OTLP_ENDPOINT", s.otel_exporter_otlp_endpoint
        )
    if s.otel_service_name:
        os.environ.setdefault("OTEL_SERVICE_NAME", s.otel_service_name)
    os.environ["RAG_TRACE_ENABLED"] = "true" if s.rag_trace_enabled else "false"
    os.environ["RAG_TRACE_FULL"] = "true" if s.rag_trace_full else "false"
    if s.rag_trace_dir:
        os.environ.setdefault("RAG_TRACE_DIR", s.rag_trace_dir)
    setup_otel()
