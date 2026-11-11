"""
统一数据访问层（MySQL / TDSQL-C）。

逻辑链路
--------
1. 进程内维护 **连接池**（默认 maxsize=8），同步 pymysql。
2. FastAPI 协程 **禁止**直接跑 SQL：经 ``run_in_thread`` / ``asyncio.to_thread``
   丢进线程池（默认 max_workers=12），在线程里借连接 → 参数化执行 → 归还。
3. Celery worker **另建池**（``get_pool(purpose=\"celery\")``），勿与 Web 共用同一池实例，
   否则多进程叠加连接数易打满云库。

云库连接上限：在控制台或 ``SHOW VARIABLES LIKE 'max_connections'`` 查看；
本进程占用约 = 池 maxsize；总占用 ≈ Web进程×8 + Celery进程×各自池大小，须留余量。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Empty, Full, Queue
from typing import Any, Callable, Iterator, Optional, Sequence, TypeVar

import pymysql
from pymysql.cursors import DictCursor

from app.settings import get_settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# 线程池：包装同步 SQL / 其它阻塞 IO
# ---------------------------------------------------------------------------

_executor_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None


def get_executor() -> ThreadPoolExecutor:
    """进程内单例线程池（Web 用）。Celery 任务本身已在 worker 进程，不必再套一层。"""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                s = get_settings()
                _executor = ThreadPoolExecutor(
                    max_workers=s.db_thread_workers,
                    thread_name_prefix="db-worker",
                )
                logger.info("DB thread pool started: max_workers=%s", s.db_thread_workers)
    return _executor


async def run_in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """在线程池执行同步函数，供 async 路由调用。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        get_executor(),
        lambda: fn(*args, **kwargs),
    )


# ---------------------------------------------------------------------------
# 连接池
# ---------------------------------------------------------------------------


class _PooledConnection:
    """归还时把连接放回队列，而不是真正 close。"""

    def __init__(self, raw: pymysql.Connection, pool: "ConnectionPool"):
        self._raw = raw
        self._pool = pool
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pool.release(self._raw)


class ConnectionPool:
    """简易线程安全连接池。"""

    def __init__(self, *, maxsize: int, purpose: str = "web"):
        self.purpose = purpose
        self.maxsize = maxsize
        self._queue: Queue[pymysql.Connection] = Queue(maxsize=maxsize)
        self._created = 0
        self._lock = threading.Lock()
        self._settings = get_settings()

    def _connect(self) -> pymysql.Connection:
        s = self._settings
        kwargs: dict[str, Any] = {
            "host": s.tdsql_host,
            "port": s.tdsql_port,
            "user": s.tdsql_user,
            "password": s.tdsql_password,
            "database": s.tdsql_db,
            "charset": s.tdsql_charset,
            "autocommit": True,
            "connect_timeout": s.tdsql_connect_timeout,
            "read_timeout": int(getattr(s, "tdsql_read_timeout", 30) or 30),
            "write_timeout": int(getattr(s, "tdsql_write_timeout", 30) or 30),
            "cursorclass": DictCursor,
        }
        if s.tdsql_ssl_ca:
            kwargs["ssl"] = {"ca": s.tdsql_ssl_ca}
        return pymysql.connect(**kwargs)

    def acquire(self, *, timeout: float = 30.0) -> _PooledConnection:
        try:
            conn = self._queue.get_nowait()
        except Empty:
            with self._lock:
                if self._created < self.maxsize:
                    conn = self._connect()
                    self._created += 1
                    logger.debug(
                        "pool[%s] new connection %s/%s",
                        self.purpose,
                        self._created,
                        self.maxsize,
                    )
                    return _PooledConnection(conn, self)
            # 已达上限：阻塞等待归还
            try:
                conn = self._queue.get(timeout=timeout)
            except Empty as exc:
                raise TimeoutError(
                    f"DB pool[{self.purpose}] exhausted (maxsize={self.maxsize})"
                ) from exc

        # 借出前做一次轻量探活
        try:
            conn.ping(reconnect=True)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            conn = self._connect()
        return _PooledConnection(conn, self)

    def release(self, conn: pymysql.Connection) -> None:
        try:
            self._queue.put_nowait(conn)
        except Full:
            try:
                conn.close()
            except Exception:
                pass
            with self._lock:
                self._created = max(0, self._created - 1)


_pools: dict[str, ConnectionPool] = {}
_pools_lock = threading.Lock()


def get_pool(*, purpose: str = "web") -> ConnectionPool:
    """
    purpose:
      - \"web\": FastAPI 进程
      - \"celery\": Celery worker 进程（必须独立实例）
    """
    with _pools_lock:
        pool = _pools.get(purpose)
        if pool is None:
            s = get_settings()
            maxsize = (
                s.celery_db_pool_maxsize if purpose == "celery" else s.db_pool_maxsize
            )
            pool = ConnectionPool(maxsize=maxsize, purpose=purpose)
            _pools[purpose] = pool
            logger.info("DB pool[%s] created: maxsize=%s", purpose, maxsize)
        return pool


def set_pool_purpose(purpose: str) -> None:
    """Celery worker 启动时调用，使默认 get_conn 走 celery 池。"""
    threading.current_thread().__dict__["_db_pool_purpose"] = purpose


def _current_purpose() -> str:
    return threading.current_thread().__dict__.get("_db_pool_purpose", "web")


@contextmanager
def get_conn(*, dict_cursor: bool = True) -> Iterator[pymysql.Connection]:
    """从当前进程对应池借连接；用完必须归还（contextmanager 保证）。"""
    pool = get_pool(purpose=_current_purpose())
    conn = pool.acquire()
    try:
        yield conn  # type: ignore[misc]
    finally:
        conn.close()  # 实际是 release


def fetch_one(sql: str, params: Optional[Sequence[Any]] = None) -> Optional[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            row = cur.fetchone()
            return dict(row) if row else None


def fetch_all(sql: str, params: Optional[Sequence[Any]] = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]


def execute(sql: str, params: Optional[Sequence[Any]] = None) -> int:
    """写操作：优先返回 lastrowid，否则 rowcount。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return int(cur.lastrowid or cur.rowcount or 0)


def execute_many(sql: str, params_seq: Sequence[Sequence[Any]]) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, list(params_seq))
            return int(cur.rowcount or 0)
