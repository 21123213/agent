"""
Milvus 向量服务：文档 Collection + 用户长期记忆 Collection。

文档（kb_document_vectors）
--------------------------
相对旧版（仅 user_id）：新增 unit_id / knowledge_base_id / owner_user_id /
embedding_model / embedding_version，权限过滤在应用层算 expr。
删除文件：delete_by_file；删除知识库：delete_by_kb。

长期记忆（user_memory_vectors）
--------------------------------
与文档分离；标量含 user_id / unit_id / scenario / updated_at / memory_id。
冲突策略：同用户相近记忆由应用层按 updated_at 取最近；检索 top-m 后注入 prompt。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, List, Optional

from app.settings import get_settings

logger = logging.getLogger(__name__)

_pymilvus = None


def _get_pymilvus():
    global _pymilvus
    if _pymilvus is None:
        from pymilvus import (
            Collection,
            CollectionSchema,
            DataType,
            FieldSchema,
            connections,
            utility,
        )

        _pymilvus = {
            "connections": connections,
            "Collection": Collection,
            "CollectionSchema": CollectionSchema,
            "FieldSchema": FieldSchema,
            "DataType": DataType,
            "utility": utility,
        }
    return _pymilvus


def _is_lock_contention(exc: BaseException) -> bool:
    """Lite 多线程同时 connect 时常见的文件锁冲突（非真正不可用）。"""
    name = type(exc).__name__
    msg = str(exc).lower()
    return (
        "DataDirLockedError" in name
        or "datadirlocked" in msg
        or "holds the lock" in msg
        or ("permission denied" in msg and "milvus" in msg)
        or "open local milvus failed" in msg
    )


class MilvusService:
    def __init__(self) -> None:
        s = get_settings()
        self.enabled = bool(s.milvus_enabled)
        self.mode = (s.milvus_mode or "lite").lower()
        self.lite_path = s.milvus_lite_path
        self.host = s.milvus_host
        self.port = s.milvus_port
        self.user = s.milvus_user
        self.password = s.milvus_password
        self.collection_name = s.milvus_collection_name
        self.memory_collection_name = s.milvus_memory_collection_name
        self.dim = int(s.milvus_embedding_dim)
        self.embedding_version = s.embedding_version
        self.collection = None
        self.memory_collection = None
        self._connected = False
        self._loaded = False
        self._memory_loaded = False
        # Lite 冷启动时多线程并行 connect 会抢同一 db 文件锁
        self._connect_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._memory_load_lock = threading.Lock()
        if not self.enabled:
            logger.warning("Milvus disabled (MILVUS_ENABLED=false)")

    def connect(self) -> bool:
        if not self.enabled:
            return False
        if self._connected:
            return True
        with self._connect_lock:
            if not self.enabled:
                return False
            if self._connected:
                return True
            pm = _get_pymilvus()
            try:
                if self.mode == "lite":
                    lite_dir = os.path.dirname(os.path.abspath(self.lite_path))
                    if lite_dir:
                        os.makedirs(lite_dir, exist_ok=True)
                    pm["connections"].connect(alias="default", uri=self.lite_path)
                else:
                    params: dict[str, Any] = {
                        "alias": "default",
                        "host": self.host,
                        "port": self.port,
                    }
                    if self.user:
                        params["user"] = self.user
                        params["password"] = self.password
                    pm["connections"].connect(**params)
                self._connected = True
                self._loaded = False
                self._memory_loaded = False
                self.ensure_collection()
                self.ensure_memory_collection()
                logger.info("Milvus connected mode=%s", self.mode)
                return True
            except Exception as exc:
                # 锁冲突：可能其它线程/进程已连上；若本实例已成功则复用，勿禁用整库
                if self._connected:
                    return True
                if _is_lock_contention(exc):
                    logger.warning(
                        "Milvus connect lock contention (mode=%s): %s",
                        self.mode,
                        exc,
                    )
                    return False
                logger.error("Milvus connect failed: %s", exc)
                self.enabled = False
                return False

    def ensure_ready(self) -> bool:
        """
        检索前串行预热：connect + load。
        full 流水线多 query 并行前调用，避免 Lite 冷启动自撞。
        """
        if not self.enabled:
            return False
        if not self.connect():
            return False
        return self._ensure_loaded()

    def _ensure_loaded(self) -> bool:
        if not self.collection:
            return False
        if self._loaded:
            return True
        with self._load_lock:
            if self._loaded:
                return True
            try:
                self.collection.load()
                self._loaded = True
                return True
            except Exception as exc:
                logger.error("Collection.load failed: %s", exc)
                return False

    def create_schema(self):
        pm = _get_pymilvus()
        fs = pm["FieldSchema"]
        dt = pm["DataType"]
        fields = [
            fs(name="id", dtype=dt.VARCHAR, max_length=128, is_primary=True),
            fs(name="unit_id", dtype=dt.INT64),
            fs(name="knowledge_base_id", dtype=dt.INT64),
            fs(name="owner_user_id", dtype=dt.INT64),
            fs(name="file_id", dtype=dt.INT64),
            fs(name="chunk_index", dtype=dt.INT64),
            fs(name="embedding_version", dtype=dt.VARCHAR, max_length=32),
            fs(name="embedding_model", dtype=dt.VARCHAR, max_length=64),
            # 入库 Unix 秒；检索后可按「更新近的优先」排序，不替代 chunk 正文
            fs(name="indexed_at", dtype=dt.INT64),
            fs(name="embedding", dtype=dt.FLOAT_VECTOR, dim=self.dim),
            fs(name="chunk_text", dtype=dt.VARCHAR, max_length=65535),
            fs(name="metadata", dtype=dt.JSON),
        ]
        return pm["CollectionSchema"](
            fields=fields,
            description="Agents KB document vectors",
            enable_dynamic_field=False,
        )

    def _existing_dim(self) -> Optional[int]:
        if not self.collection:
            return None
        for field in self.collection.schema.fields:
            if field.name == "embedding":
                if hasattr(field, "params") and field.params:
                    return field.params.get("dim")
                return getattr(field, "dim", None)
        return None

    def ensure_collection(self) -> bool:
        if not self.enabled:
            return False
        if not self._connected and not self.connect():
            return False
        pm = _get_pymilvus()
        if pm["utility"].has_collection(self.collection_name):
            self.collection = pm["Collection"](self.collection_name)
            existing = self._existing_dim()
            # 开发便利：维度不对、或缺关键标量时直接 drop 重建。
            # 生产不能这么干（会丢向量），应做迁移/双写；Lite 本地库可接受重建。
            names = {f.name for f in self.collection.schema.fields}
            need = {"unit_id", "knowledge_base_id", "indexed_at"}
            if existing != self.dim or not need.issubset(names):
                logger.warning(
                    "Recreating collection %s (dim/schema mismatch; DEV only)",
                    self.collection_name,
                )
                pm["utility"].drop_collection(self.collection_name)
                self.collection = None
                self._loaded = False
            else:
                return True

        schema = self.create_schema()
        self.collection = pm["Collection"](name=self.collection_name, schema=schema)
        self.collection.create_index(
            field_name="embedding",
            index_params={
                "index_type": "HNSW",
                "metric_type": "IP",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
        logger.info("Created collection %s dim=%s", self.collection_name, self.dim)
        return True

    @staticmethod
    def make_vector_id(
        unit_id: int, kb_id: int, file_id: int, chunk_index: int
    ) -> str:
        return f"u{unit_id}_kb{kb_id}_f{file_id}_c{chunk_index}"

    def insert_vectors(
        self,
        *,
        unit_id: int,
        knowledge_base_id: int,
        owner_user_id: int,
        file_id: int,
        chunks: List[dict[str, Any]],
        embedding_model: str,
        embedding_version: str | None = None,
    ) -> int:
        if not self.enabled:
            return 0
        if not self._connected and not self.connect():
            return 0
        if not chunks:
            return 0
        ver = embedding_version or self.embedding_version
        now_ts = int(time.time())
        data = []
        for chunk in chunks:
            emb = chunk["embedding"]
            if len(emb) != self.dim:
                raise ValueError(
                    f"dim mismatch chunk={chunk['chunk_index']}: "
                    f"{len(emb)} != {self.dim}"
                )
            idx = int(chunk["chunk_index"])
            # 允许调用方覆盖（例如按文件 upload_time）；默认入库时刻
            indexed_at = int(chunk.get("indexed_at") or now_ts)
            data.append(
                {
                    "id": self.make_vector_id(
                        unit_id, knowledge_base_id, file_id, idx
                    ),
                    "unit_id": int(unit_id),
                    "knowledge_base_id": int(knowledge_base_id),
                    "owner_user_id": int(owner_user_id),
                    "file_id": int(file_id),
                    "chunk_index": idx,
                    "embedding_version": ver,
                    "embedding_model": embedding_model[:64],
                    "indexed_at": indexed_at,
                    "embedding": emb,
                    "chunk_text": (chunk.get("text") or "")[:65535],
                    "metadata": chunk.get("metadata") or {},
                }
            )
        self.collection.insert(data)
        self.collection.flush()
        logger.info(
            "Inserted %s vectors file_id=%s kb=%s",
            len(data),
            file_id,
            knowledge_base_id,
        )
        return len(data)

    def search(
        self,
        *,
        unit_id: int,
        knowledge_base_ids: List[int],
        query_embedding: List[float],
        top_k: int = 10,
        file_ids: Optional[List[int]] = None,
        embedding_version: str | None = None,
        _reloaded: bool = False,
    ) -> List[dict[str, Any]]:
        """
        向量检索。过滤：unit_id + knowledge_base_id(+file_id) + embedding_version。
        """
        if not self.enabled:
            return []
        if not self._connected and not self.connect():
            return []
        if not knowledge_base_ids:
            return []
        if len(query_embedding) != self.dim:
            raise ValueError(
                f"query embedding dim {len(query_embedding)} != {self.dim}"
            )
        ver = embedding_version or self.embedding_version
        kb_list = ",".join(str(int(x)) for x in knowledge_base_ids)
        expr = (
            f"unit_id == {int(unit_id)} and knowledge_base_id in [{kb_list}] "
            f'and embedding_version == "{ver}"'
        )
        if file_ids:
            fids = ",".join(str(int(x)) for x in file_ids)
            expr += f" and file_id in [{fids}]"
        try:
            if not self._ensure_loaded():
                return []
            results = self.collection.search(
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "IP", "params": {"ef": 64}},
                limit=max(1, int(top_k)),
                expr=expr,
                output_fields=[
                    "chunk_text",
                    "file_id",
                    "chunk_index",
                    "knowledge_base_id",
                    "metadata",
                    "unit_id",
                    "owner_user_id",
                ],
            )
            formatted: List[dict[str, Any]] = []
            for hits in results:
                for hit in hits:
                    formatted.append(
                        {
                            "id": hit.id,
                            "distance": hit.distance,
                            "score": hit.distance,
                            "chunk_text": hit.entity.get("chunk_text"),
                            "file_id": hit.entity.get("file_id"),
                            "chunk_index": hit.entity.get("chunk_index"),
                            "knowledge_base_id": hit.entity.get("knowledge_base_id"),
                            "unit_id": hit.entity.get("unit_id"),
                            "owner_user_id": hit.entity.get("owner_user_id"),
                            "metadata": hit.entity.get("metadata") or {},
                        }
                    )
            return formatted
        except Exception as exc:
            err = str(exc).lower()
            if (not _reloaded) and (
                "not loaded" in err or "goaway" in err or "unavailable" in err
            ):
                self._loaded = False
                logger.warning("Milvus search retry after load: %s", exc)
                return self.search(
                    unit_id=unit_id,
                    knowledge_base_ids=knowledge_base_ids,
                    query_embedding=query_embedding,
                    top_k=top_k,
                    file_ids=file_ids,
                    embedding_version=embedding_version,
                    _reloaded=True,
                )
            logger.error("Milvus search failed: %s", exc)
            return []

    def delete_by_file(self, unit_id: int, file_id: int) -> int:
        return self._delete(f"unit_id == {int(unit_id)} and file_id == {int(file_id)}")

    def delete_by_kb(self, unit_id: int, knowledge_base_id: int) -> int:
        return self._delete(
            f"unit_id == {int(unit_id)} and knowledge_base_id == {int(knowledge_base_id)}"
        )

    def _delete(self, expr: str) -> int:
        if not self.enabled:
            return 0
        if not self._connected and not self.connect():
            return 0
        try:
            if not self._ensure_loaded():
                return 0
            rows = self.collection.query(expr=expr, output_fields=["id"])
            count = len(rows)
            self.collection.delete(expr)
            self.collection.flush()
            logger.info("Deleted %s vectors expr=%s", count, expr)
            return count
        except Exception as exc:
            logger.error("Milvus delete failed: %s", exc)
            return 0

    # =========================================================================
    # 用户长期记忆 Collection：user_memory_vectors
    # =========================================================================

    def create_memory_schema(self):
        pm = _get_pymilvus()
        fs = pm["FieldSchema"]
        dt = pm["DataType"]
        fields = [
            fs(name="id", dtype=dt.VARCHAR, max_length=128, is_primary=True),
            fs(name="memory_id", dtype=dt.INT64),  # MySQL user_memories.id
            fs(name="user_id", dtype=dt.INT64),
            fs(name="unit_id", dtype=dt.INT64),
            fs(name="scenario", dtype=dt.VARCHAR, max_length=64),
            fs(name="category", dtype=dt.VARCHAR, max_length=32),
            fs(name="updated_at", dtype=dt.INT64),  # Unix 秒；冲突取最近
            fs(name="embedding", dtype=dt.FLOAT_VECTOR, dim=self.dim),
            fs(name="content", dtype=dt.VARCHAR, max_length=4096),
        ]
        return pm["CollectionSchema"](
            fields=fields,
            description="Agents user long-term memory vectors",
            enable_dynamic_field=False,
        )

    def ensure_memory_collection(self) -> bool:
        """确保记忆 Collection 存在；schema 不匹配时 Lite 可重建（DEV）。"""
        if not self.enabled:
            return False
        if not self._connected and not self.connect():
            return False
        pm = _get_pymilvus()
        name = self.memory_collection_name
        if pm["utility"].has_collection(name):
            self.memory_collection = pm["Collection"](name)
            names = {f.name for f in self.memory_collection.schema.fields}
            need = {"memory_id", "user_id", "unit_id", "scenario", "updated_at"}
            existing_dim = None
            for field in self.memory_collection.schema.fields:
                if field.name == "embedding":
                    if hasattr(field, "params") and field.params:
                        existing_dim = field.params.get("dim")
                    else:
                        existing_dim = getattr(field, "dim", None)
            if existing_dim != self.dim or not need.issubset(names):
                logger.warning(
                    "Recreating memory collection %s (schema mismatch; DEV only)",
                    name,
                )
                pm["utility"].drop_collection(name)
                self.memory_collection = None
                self._memory_loaded = False
            else:
                return True

        schema = self.create_memory_schema()
        self.memory_collection = pm["Collection"](name=name, schema=schema)
        self.memory_collection.create_index(
            field_name="embedding",
            index_params={
                "index_type": "HNSW",
                "metric_type": "IP",
                "params": {"M": 16, "efConstruction": 200},
            },
        )
        logger.info("Created memory collection %s dim=%s", name, self.dim)
        return True

    def _ensure_memory_loaded(self) -> bool:
        if not self.memory_collection:
            if not self.ensure_memory_collection():
                return False
        if self._memory_loaded:
            return True
        with self._memory_load_lock:
            if self._memory_loaded:
                return True
            try:
                self.memory_collection.load()
                self._memory_loaded = True
                return True
            except Exception as exc:
                logger.error("memory Collection.load failed: %s", exc)
                return False

    @staticmethod
    def make_memory_vector_id(user_id: int, memory_id: int) -> str:
        return f"um_u{int(user_id)}_m{int(memory_id)}"

    def insert_user_memory_vector(
        self,
        *,
        memory_id: int,
        user_id: int,
        unit_id: int,
        content: str,
        embedding: List[float],
        scenario: str = "rag_qa",
        category: str = "preference",
        updated_at: int | None = None,
    ) -> str:
        """写入一条记忆向量；返回 vector id。先删同 memory_id 旧向量再插（幂等）。"""
        if not self.enabled:
            return ""
        if not self._connected and not self.connect():
            return ""
        if not self.ensure_memory_collection():
            return ""
        if len(embedding) != self.dim:
            raise ValueError(f"memory embedding dim {len(embedding)} != {self.dim}")
        vid = self.make_memory_vector_id(user_id, memory_id)
        # 幂等：同 memory_id 先删
        self.delete_user_memory_vector(user_id=user_id, memory_id=memory_id)
        if not self._ensure_memory_loaded():
            return ""
        ts = int(updated_at or time.time())
        row = {
            "id": vid,
            "memory_id": int(memory_id),
            "user_id": int(user_id),
            "unit_id": int(unit_id),
            "scenario": (scenario or "rag_qa")[:64],
            "category": (category or "preference")[:32],
            "updated_at": ts,
            "embedding": embedding,
            "content": (content or "")[:4096],
        }
        self.memory_collection.insert([row])
        self.memory_collection.flush()
        logger.info(
            "Inserted memory vector id=%s user=%s memory_id=%s",
            vid,
            user_id,
            memory_id,
        )
        return vid

    def search_user_memories(
        self,
        *,
        user_id: int,
        unit_id: int,
        query_embedding: List[float],
        top_k: int = 5,
        scenario: str | None = "rag_qa",
    ) -> List[dict[str, Any]]:
        """
        按 user_id(+unit) 过滤后的语义召回。
        返回按相似度排序；同相似度下调用方可再按 updated_at 取最近。
        """
        if not self.enabled:
            return []
        if not self._connected and not self.connect():
            return []
        if not self.ensure_memory_collection():
            return []
        if len(query_embedding) != self.dim:
            raise ValueError(
                f"query embedding dim {len(query_embedding)} != {self.dim}"
            )
        expr = f"user_id == {int(user_id)} and unit_id == {int(unit_id)}"
        if scenario:
            # 标量字符串比较
            esc = scenario.replace('"', "")
            expr += f' and scenario == "{esc}"'
        try:
            if not self._ensure_memory_loaded():
                return []
            results = self.memory_collection.search(
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "IP", "params": {"ef": 64}},
                limit=max(1, int(top_k)),
                expr=expr,
                output_fields=[
                    "memory_id",
                    "user_id",
                    "unit_id",
                    "scenario",
                    "category",
                    "updated_at",
                    "content",
                ],
            )
            formatted: List[dict[str, Any]] = []
            for hits in results:
                for hit in hits:
                    formatted.append(
                        {
                            "id": hit.id,
                            "score": float(hit.distance),
                            "memory_id": hit.entity.get("memory_id"),
                            "user_id": hit.entity.get("user_id"),
                            "unit_id": hit.entity.get("unit_id"),
                            "scenario": hit.entity.get("scenario"),
                            "category": hit.entity.get("category"),
                            "updated_at": hit.entity.get("updated_at"),
                            "content": hit.entity.get("content"),
                        }
                    )
            # 冲突：同 content 近似时 updated_at 大者优先（稳定排序）
            formatted.sort(
                key=lambda x: (float(x.get("score") or 0), int(x.get("updated_at") or 0)),
                reverse=True,
            )
            return formatted
        except Exception as exc:
            logger.error("memory search failed: %s", exc)
            return []

    def delete_user_memory_vector(
        self, *, user_id: int, memory_id: int
    ) -> int:
        if not self.enabled or not self.memory_collection:
            if not self.ensure_memory_collection():
                return 0
        expr = (
            f"user_id == {int(user_id)} and memory_id == {int(memory_id)}"
        )
        try:
            if not self._ensure_memory_loaded():
                return 0
            rows = self.memory_collection.query(expr=expr, output_fields=["id"])
            count = len(rows)
            if count:
                self.memory_collection.delete(expr)
                self.memory_collection.flush()
            return count
        except Exception as exc:
            logger.error("memory delete failed: %s", exc)
            return 0


milvus_service = MilvusService()
