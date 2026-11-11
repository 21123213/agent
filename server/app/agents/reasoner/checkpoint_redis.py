"""
Reasoner 用 Redis Checkpointer（普通 Redis 键，不依赖 RedisJSON/RediSearch）。

为什么不用官方 ``langgraph-checkpoint-redis``
--------------------------------------------
官方实现要求 RedisJSON + RediSearch（Redis 8 / Redis Stack）。本地常见的
纯 Redis 没有这些模块。本实现用 HASH + pickle（经 LangGraph serde 的
dumps_typed/loads_typed）存 checkpoint，**本地与上线同一套**，禁止 MemorySaver。

人工等待
--------
LangGraph ``interrupt`` 把图状态写入本 checkpointer；前端 ``Command(resume=...)``
用同一 ``thread_id``（= session_id）续跑。**不再使用 PendingStore**。

键空间（切勿与 session context / QA cache 混清）
----------------------------------------------
- ``reasoner:lg:ckpt:{tid}:{ns}``     HASH  field=checkpoint_id → payload
- ``reasoner:lg:blob:{tid}:{ns}``     HASH  field=channel|version → blob
- ``reasoner:lg:writes:{tid}:{ns}:{cid}`` HASH field=task|idx → write
- TTL：可配；默认跟会话窗口类似量级或更长（interrupt 可能隔几分钟才 resume）
"""
from __future__ import annotations

import logging
import pickle
import threading
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)

from app.infra import redis_client as redis_mgr
from app.settings import get_settings

logger = logging.getLogger(__name__)

# 进程内按 thread 轻量锁，避免同一 session 并发写互相覆盖
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _thread_lock(thread_id: str) -> threading.Lock:
    with _locks_guard:
        if thread_id not in _locks:
            _locks[thread_id] = threading.Lock()
        return _locks[thread_id]


def _ttl_sec() -> int:
    """checkpoint TTL；Settings 可配，默认 24h（长于短记忆，方便隔夜 resume 调试）。"""
    s = get_settings()
    return int(getattr(s, "reasoner_checkpoint_ttl", 86400) or 86400)


def _k_ckpt(tid: str, ns: str) -> str:
    return f"reasoner:lg:ckpt:{tid}:{ns}"


def _k_blob(tid: str, ns: str) -> str:
    return f"reasoner:lg:blob:{tid}:{ns}"


def _k_writes(tid: str, ns: str, cid: str) -> str:
    return f"reasoner:lg:writes:{tid}:{ns}:{cid}"


class RedisCheckpointSaver(BaseCheckpointSaver):
    """
    基于普通 Redis 的 Checkpointer。

    序列化：先用 LangGraph ``serde.dumps_typed`` 得到 (type, bytes)，再 ``pickle``
    整包写入 Redis（value 为二进制，故 Redis 客户端需 ``decode_responses=False``
    的连接；见 ``_raw_redis``）。
    """

    def __init__(self) -> None:
        super().__init__()

    def _raw_redis(self):
        """
        checkpoint 存二进制，必须用 decode_responses=False 的客户端。
        与业务用的 decode_responses=True 客户端分开，避免字符串解码破坏 pickle。
        """
        s = get_settings()
        import redis

        return redis.Redis(
            host=s.redis_host,
            port=s.redis_port,
            db=s.redis_db,
            password=s.redis_password or None,
            decode_responses=False,
            socket_connect_timeout=2,
            socket_timeout=5,
        )

    def _dump(self, obj: Any) -> bytes:
        return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)

    def _load(self, raw: Optional[bytes]) -> Any:
        if not raw:
            return None
        return pickle.loads(raw)

    def _touch_ttl(self, r, *keys: str) -> None:
        ttl = _ttl_sec()
        for k in keys:
            try:
                r.expire(k, ttl)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # BaseCheckpointSaver API
    # ------------------------------------------------------------------

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "") or ""
        with _thread_lock(thread_id):
            r = self._raw_redis()
            ckpt_key = _k_ckpt(thread_id, checkpoint_ns)
            want_id = get_checkpoint_id(config)
            if want_id:
                raw = r.hget(ckpt_key, want_id)
                checkpoint_id = want_id
            else:
                # 取字典序最大的 id（LangGraph checkpoint id 可比较）
                all_ids = r.hkeys(ckpt_key)
                if not all_ids:
                    return None
                decoded = [
                    i.decode("utf-8") if isinstance(i, (bytes, bytearray)) else str(i)
                    for i in all_ids
                ]
                checkpoint_id = max(decoded)
                raw = r.hget(ckpt_key, checkpoint_id)
            if not raw:
                return None
            checkpoint_b, metadata_b, parent_checkpoint_id = self._load(raw)
            checkpoint_: Checkpoint = self.serde.loads_typed(checkpoint_b)
            channel_values = self._load_blobs(
                r, thread_id, checkpoint_ns, checkpoint_.get("channel_versions", {})
            )
            writes = self._load_writes(r, thread_id, checkpoint_ns, checkpoint_id)
            return CheckpointTuple(
                config={
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": checkpoint_id,
                    }
                },
                checkpoint={**checkpoint_, "channel_values": channel_values},
                metadata=self.serde.loads_typed(metadata_b),
                pending_writes=[
                    (tid, ch, self.serde.loads_typed(v)) for tid, ch, v, _ in writes
                ],
                parent_config=(
                    {
                        "configurable": {
                            "thread_id": thread_id,
                            "checkpoint_ns": checkpoint_ns,
                            "checkpoint_id": parent_checkpoint_id,
                        }
                    }
                    if parent_checkpoint_id
                    else None
                ),
            )

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if not config:
            return iter(())
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
        # 简化：只列该 thread+ns 下全部，按 id 倒序
        r = self._raw_redis()
        ckpt_key = _k_ckpt(thread_id, checkpoint_ns)
        all_ids = r.hkeys(ckpt_key)
        decoded = sorted(
            (
                i.decode("utf-8") if isinstance(i, (bytes, bytearray)) else str(i)
                for i in all_ids
            ),
            reverse=True,
        )
        before_id = get_checkpoint_id(before) if before else None
        config_id = get_checkpoint_id(config)
        n = 0
        for cid in decoded:
            if config_id and cid != config_id:
                continue
            if before_id and cid >= before_id:
                continue
            tup = self.get_tuple(
                {
                    "configurable": {
                        "thread_id": thread_id,
                        "checkpoint_ns": checkpoint_ns,
                        "checkpoint_id": cid,
                    }
                }
            )
            if tup is None:
                continue
            if filter and not all(
                filter[k] == (tup.metadata or {}).get(k) for k in filter
            ):
                continue
            yield tup
            n += 1
            if limit is not None and n >= limit:
                break

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
        with _thread_lock(thread_id):
            r = self._raw_redis()
            c = checkpoint.copy()
            values: dict[str, Any] = c.pop("channel_values")  # type: ignore[misc]
            blob_key = _k_blob(thread_id, checkpoint_ns)
            for k, v in new_versions.items():
                field = f"{k}|{v}"
                payload = (
                    self.serde.dumps_typed(values[k])
                    if k in values
                    else ("empty", b"")
                )
                r.hset(blob_key, field, self._dump(payload))
            ckpt_key = _k_ckpt(thread_id, checkpoint_ns)
            parent = config["configurable"].get("checkpoint_id")
            r.hset(
                ckpt_key,
                checkpoint["id"],
                self._dump(
                    (
                        self.serde.dumps_typed(c),
                        self.serde.dumps_typed(
                            get_checkpoint_metadata(config, metadata)
                        ),
                        parent,
                    )
                ),
            )
            self._touch_ttl(r, ckpt_key, blob_key)
            logger.debug(
                "redis_checkpoint put thread=%s id=%s", thread_id, checkpoint["id"]
            )
            return {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint["id"],
                }
            }

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "") or ""
        checkpoint_id = config["configurable"]["checkpoint_id"]
        with _thread_lock(thread_id):
            r = self._raw_redis()
            wkey = _k_writes(thread_id, checkpoint_ns, checkpoint_id)
            for idx, (ch, val) in enumerate(writes):
                mapped = WRITES_IDX_MAP.get(ch, idx)
                field = f"{task_id}|{mapped}"
                if mapped >= 0 and r.hexists(wkey, field):
                    continue
                r.hset(
                    wkey,
                    field,
                    self._dump(
                        (task_id, ch, self.serde.dumps_typed(val), task_path)
                    ),
                )
            self._touch_ttl(r, wkey)

    def delete_thread(self, thread_id: str) -> None:
        """清会话时调用：删掉该 thread 下所有 checkpoint / blob / writes。"""
        with _thread_lock(thread_id):
            r = self._raw_redis()
            # 扫描本库 reasoner:lg:*:{thread_id}:* —— 用已知前缀删除
            # 简化：keys 匹配（生产量不大；若 QPS 高可改 SCAN）
            patterns = [
                f"reasoner:lg:ckpt:{thread_id}:*",
                f"reasoner:lg:blob:{thread_id}:*",
                f"reasoner:lg:writes:{thread_id}:*",
            ]
            deleted = 0
            for pat in patterns:
                for key in r.scan_iter(match=pat, count=100):
                    r.delete(key)
                    deleted += 1
            logger.info(
                "redis_checkpoint delete_thread thread=%s keys=%s", thread_id, deleted
            )

    # async 包装：LangGraph 异步路径会调这些
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        return self.get_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ):
        for item in self.list(config, filter=filter, before=before, limit=limit):
            yield item

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        return self.put(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        self.put_writes(config, writes, task_id, task_path)

    async def adelete_thread(self, thread_id: str) -> None:
        self.delete_thread(thread_id)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _load_blobs(
        self,
        r,
        thread_id: str,
        checkpoint_ns: str,
        versions: ChannelVersions,
    ) -> dict[str, Any]:
        blob_key = _k_blob(thread_id, checkpoint_ns)
        out: dict[str, Any] = {}
        for ch, ver in versions.items():
            field = f"{ch}|{ver}"
            raw = r.hget(blob_key, field)
            if not raw:
                continue
            typed = self._load(raw)
            if not typed or typed[0] == "empty":
                continue
            out[ch] = self.serde.loads_typed(typed)
        return out

    def _load_writes(
        self, r, thread_id: str, checkpoint_ns: str, checkpoint_id: str
    ) -> list[tuple[str, str, Any, str]]:
        wkey = _k_writes(thread_id, checkpoint_ns, checkpoint_id)
        rows = []
        for _field, raw in (r.hgetall(wkey) or {}).items():
            rows.append(self._load(raw))
        # 稳定排序：按 task_id + channel
        rows.sort(key=lambda x: (x[0], x[1]))
        return rows


_saver: RedisCheckpointSaver | None = None
_saver_lock = threading.Lock()


def get_checkpointer() -> RedisCheckpointSaver:
    """
    进程内单例 Checkpointer。

    注意：必须连得上 Redis；连不上应在启动/首次请求时失败，
    **不要**静默退回 MemorySaver（多 worker 会丢 interrupt 状态）。
    """
    global _saver
    with _saver_lock:
        if _saver is None:
            # 探活：与业务 redis 同一 host；失败直接抛
            if not redis_mgr.redis_available():
                raise RuntimeError(
                    "Reasoner 需要 Redis（checkpointer / skill 粘滞）；"
                    "请启动 Redis 并检查 REDIS_* 配置"
                )
            _saver = RedisCheckpointSaver()
            logger.info("Reasoner RedisCheckpointSaver ready")
        return _saver
