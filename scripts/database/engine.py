"""database/engine.py —— 连接管理、连接工厂与任务派发。

职责:
- SQLite 连接工厂 (统一 PRAGMA 设置);
- 单写线程 (唯一写连接 + FIFO 队列 + Future) 与读线程池
  (每线程 threading.local 只读连接);
- 写任务统一套用显式事务 (BEGIN IMMEDIATE -> 业务函数 -> COMMIT/ROLLBACK);
- 写空闲达到配置时长后自动执行 SQLite WAL checkpoint
  (PRAGMA wal_checkpoint(TRUNCATE)), 把 WAL 内容写回主库;
- 模块级 init()/close() 生命周期与同步派发入口 submit_write/submit_read。

具体的表操作见 table/*.py, 搜索见 search.py, 对外门面见 service.py。
"""
from __future__ import annotations

import contextlib
import contextvars
import itertools
import os
import queue
import sqlite3
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from exceptions import (
    DatabaseClosedError,
    InputDataError,
    SchemaInitError,
)
from scripts.database.upgrade import (
    DB_VERSION,
    backup_database,
    bootstrap,
    migration_needed,
)
from scripts.logger import log

# 读工作线程数
_READ_WORKERS = 4

_CLOSED_MSG = "Database is not initialized or already closed"

# 任务优先级: Web 请求优先于后台任务 (入库/对账/缩略图解析等)
PRIORITY_INTERACTIVE = 0
PRIORITY_BACKGROUND = 1
_SHUTDOWN_PRIORITY = 99

_CURRENT_PRIORITY: contextvars.ContextVar[int] = contextvars.ContextVar(
    "genesis_db_priority", default=PRIORITY_BACKGROUND)


@contextlib.contextmanager
def interactive_priority():
    """在 Web 请求作用域内把数据库任务标记为交互优先。"""
    token = _CURRENT_PRIORITY.set(PRIORITY_INTERACTIVE)
    try:
        yield
    finally:
        _CURRENT_PRIORITY.reset(token)


def _current_priority() -> int:
    return _CURRENT_PRIORITY.get()

# 写空闲 WAL checkpoint: 默认 10 分钟; 0 表示关闭 (由 config.json 提供)
CHECKPOINT_IDLE_MINUTES = 10.0
_CHECKPOINT_POLL_MAX = 30.0
# 写队列中的 checkpoint 任务标记 (不经 BEGIN/COMMIT 事务)
_CHECKPOINT_TASK = object()


# ---------------------------------------------------------------- #
# 连接工厂 (连接管理唯一入口)
# ---------------------------------------------------------------- #

def open_connection(db_path) -> sqlite3.Connection:
    """创建新连接。每个连接创建时独立执行三条 PRAGMA。

    isolation_level=None 关闭 sqlite3 的隐式事务管理,
    写事务由 _run_in_transaction 显式控制 BEGIN/COMMIT/ROLLBACK。
    禁止 check_same_thread=False: 连接在哪个线程创建就只能在哪个线程使用
    (写连接仅在写线程内创建; 读连接在各读线程内经 threading.local 创建) 。
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    log.debug(f"Database connection opened: {db_path}")
    return conn


# ---------------------------------------------------------------- #
# 写事务模板 (由写线程对每个任务套用, 业务函数自身不管事务)
# ---------------------------------------------------------------- #

def _run_in_transaction(conn: sqlite3.Connection, fn, args: tuple) -> Any:
    """显式事务执行一个业务写操作: BEGIN IMMEDIATE -> fn -> COMMIT / ROLLBACK。

    BEGIN IMMEDIATE 在事务开始时即取写锁, 避免锁升级死锁;
    IntegrityError 统一转换为 InputDataError (保留原因链) 。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn, *args)
    except sqlite3.IntegrityError as e:
        _safe_rollback(conn)
        log.error(f"Database integrity error: {e}", exc_info=True)
        raise InputDataError(
            f"Database constraint violated or conflict occurred: {e}"
        ) from e
    except BaseException:
        _safe_rollback(conn)
        raise
    conn.execute("COMMIT")
    return result


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """回滚当前事务; 事务已被 SQLite 自动终止时忽略错误。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- #
# 引擎: 唯一写线程 + 读连接池 (连接管理集中于此)
# ---------------------------------------------------------------- #

class _Engine:
    """连接与线程基础设施。

    写侧: 单后台线程持有唯一写连接 (线程内创建) , 任务经优先级队列提交,
    结果经 concurrent.futures.Future 返回; Web 请求优先于后台任务。
    读侧: 固定 4 个工作线程 (各自 threading.local 只读连接) + 优先级队列,
    同样优先处理 Web 请求的查询。
    """

    def __init__(self, db_path: Path,
                 idle_checkpoint_minutes: float = CHECKPOINT_IDLE_MINUTES):
        self.db_path = db_path
        self.path_key = ""
        self._closed = False
        self._close_lock = threading.Lock()
        self._write_seq = itertools.count()
        self._write_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._writer = threading.Thread(
            target=self._write_loop, name="db-writer", daemon=True)
        self._read_seq = itertools.count()
        self._read_queue: queue.PriorityQueue = queue.PriorityQueue()
        self._read_workers = [
            threading.Thread(target=self._read_loop, name=f"db-read-{index}",
                             daemon=True)
            for index in range(_READ_WORKERS)
        ]
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_conns_lock = threading.Lock()
        # 写空闲计时与 WAL checkpoint 定时器 (0 表示关闭该功能)
        self._idle_checkpoint_seconds = max(
            0.0, float(idle_checkpoint_minutes)) * 60.0
        self._last_write_at = time.monotonic()
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        self._writer.start()
        for worker in self._read_workers:
            worker.start()
        if self._idle_checkpoint_seconds > 0:
            self._checkpoint_thread = threading.Thread(
                target=self._checkpoint_loop, name="db-checkpoint", daemon=True)
            self._checkpoint_thread.start()

    # ---- 写侧 ----
    def submit_write(self, fn, *args) -> Future:
        """提交写任务到 FIFO 队列, 返回 Future。

        入队在 close 锁内完成: 与 close() 的关闭哨兵之间有确定先后顺序,
        保证 close() 返回后不会仍有任务处于未完成状态。
        """
        with self._close_lock:
            if self._closed:
                raise DatabaseClosedError(_CLOSED_MSG)
            future = Future()
            self._write_queue.put(
                (_current_priority(), next(self._write_seq), (fn, args, future)))
            self._last_write_at = time.monotonic()
            return future

    # ---- WAL checkpoint ----
    def submit_checkpoint(self) -> Future | None:
        """排队执行一次 WAL checkpoint; 引擎已关闭时返回 None。

        checkpoint 在写线程中直接执行 (不在 BEGIN/COMMIT 事务内),
        避免长事务阻止 WAL 合并。
        """
        with self._close_lock:
            if self._closed:
                return None
            future = Future()
            self._write_queue.put(
                (PRIORITY_BACKGROUND, next(self._write_seq),
                 (_CHECKPOINT_TASK, (), future)))
            self._last_write_at = time.monotonic()
            return future

    def _checkpoint_loop(self) -> None:
        """写空闲达到阈值后执行 WAL checkpoint (链式轮询)。"""
        interval = self._idle_checkpoint_seconds
        poll = min(_CHECKPOINT_POLL_MAX, max(1.0, interval / 4.0))
        while not self._checkpoint_stop.wait(poll):
            if time.monotonic() - self._last_write_at < interval:
                continue
            try:
                future = self.submit_checkpoint()
                if future is not None:
                    future.result()
            except Exception as e:  # noqa: BLE001 checkpoint 失败不影响服务
                log.warning(f"WAL checkpoint failed: {e}")

    def _run_checkpoint(self, conn: sqlite3.Connection) -> tuple:
        """在写连接上执行 TRUNCATE checkpoint, 返回 (busy, wal, moved)。"""
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result = tuple(int(v) for v in (row or (0, 0, 0)))
        busy, wal_frames, moved = (result + (0, 0, 0))[:3]
        if busy:
            log.warning("WAL checkpoint skipped: database is busy")
        else:
            # TRUNCATE 模式下 SQLite 可能在返回前已截断 WAL, 计数常为 0
            log.debug(f"WAL checkpoint completed "
                      f"(wal frames={wal_frames}, moved={moved})")
        return result

    def _write_loop(self) -> None:
        """写线程主体: 创建并持有唯一写连接, 逐个执行队列中的业务写操作。"""
        try:
            conn = open_connection(self.db_path)
        except Exception:
            log.error("Failed to open the write connection", exc_info=True)
            self._drain_with_error_on_startup()
            return
        try:
            while True:
                _priority, _seq, task = self._write_queue.get()
                if task is None:  # 关闭哨兵: 先于哨兵入队的任务均已处理完毕
                    break
                fn, args, future = task
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            if fn is _CHECKPOINT_TASK:
                                future.set_result(self._run_checkpoint(conn))
                            else:
                                future.set_result(
                                    _run_in_transaction(conn, fn, args))
                        except InputDataError as e:
                            # 业务校验类拒绝: 属调用方输入问题, 降为 DEBUG
                            log.debug(f"Write operation rejected: {e}")
                            future.set_exception(e)
                        except Exception as e:
                            log.error(
                                "Write operation failed: "
                                f"{getattr(fn, '__name__', fn)}",
                                exc_info=True,
                            )
                            future.set_exception(e)
                except Exception:
                    # Future 状态异常等极端情形: 不允许写线程死亡,
                    # 否则所有等待者将永久阻塞
                    log.error("Write worker internal error", exc_info=True)
        finally:
            conn.close()
            log.debug("Write connection closed")

    def _drain_with_error_on_startup(self) -> None:
        """写连接建立失败: 持续排空队列, 等待者全部以该异常收尾, 避免永久阻塞。"""
        while True:
            _priority, _seq, task = self._write_queue.get()
            if task is None:
                return
            _, _, future = task
            if future.set_running_or_notify_cancel():
                future.set_exception(
                    SchemaInitError("Database write connection failed to open"))

    # ---- 读侧 ----
    def submit_read(self, fn, *args) -> Future:
        """提交读任务到优先级队列 (4 个工作线程并发执行)。"""
        with self._close_lock:
            if self._closed:
                raise DatabaseClosedError(_CLOSED_MSG)
            future = Future()
            self._read_queue.put(
                (_current_priority(), next(self._read_seq), fn, args, future))
            return future

    def _read_loop(self) -> None:
        while True:
            item = self._read_queue.get()
            _priority, _seq, fn, args, future = item
            if fn is None:  # 关闭哨兵
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(self._read_job(fn, args))
            except BaseException as e:  # noqa: BLE001 异常交给等待方
                future.set_exception(e)

    def _read_job(self, fn, args) -> Any:
        try:
            return fn(self._get_read_conn(), *args)
        except InputDataError as e:
            log.debug(f"Read operation rejected: {e}")
            raise
        except Exception:
            log.error(
                f"Read operation failed: {getattr(fn, '__name__', fn)}",
                exc_info=True,
            )
            raise

    def _get_read_conn(self) -> sqlite3.Connection:
        """取当前读线程的独享连接; 不存在则在当前线程内创建。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_connection(self.db_path)
            # 读连接严禁执行任何写语句: 数据库层面强制只读
            conn.execute("PRAGMA query_only = ON")
            self._local.conn = conn
            with self._read_conns_lock:
                self._read_conns.append(conn)
        return conn

    # ---- 生命周期 ----
    def close(self) -> None:
        """等待写队列清空、进行中任务全部完成后关闭所有连接 (幂等) 。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # 先停止 checkpoint 定时器, 再投递关闭哨兵 (此时已无新任务可入队)
        self._checkpoint_stop.set()
        if self._checkpoint_thread is not None:
            self._checkpoint_thread.join(timeout=5)
        # 写任务全部处理完毕后再退出 (优先级 99 保证排在所有写任务之后)
        self._write_queue.put(
            (_SHUTDOWN_PRIORITY, next(self._write_seq), None))
        self._writer.join()
        # 读任务无副作用: 丢弃排队中的读请求, 仅等待执行中的读完成
        while True:
            try:
                _p, _s, fn, args, future = self._read_queue.get_nowait()
            except queue.Empty:
                break
            if fn is not None and future is not None:
                future.cancel()
        for _worker in self._read_workers:
            self._read_queue.put(
                (_SHUTDOWN_PRIORITY, next(self._read_seq), None, (), None))
        for worker in self._read_workers:
            worker.join()
        # 读线程此时均已退出, 集中兜底关闭其连接 (无线程仍在使用,
        # 不构成运行期跨线程共享; 线程本地对象本身亦随线程退出释放)
        with self._read_conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()
        log.debug("Database engine closed")


# ---------------------------------------------------------------- #
# 模块级生命周期与任务派发
# ---------------------------------------------------------------- #

_ENGINE: _Engine | None = None
_ENGINE_LOCK = threading.Lock()


def _norm_db_path(db_path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(db_path)))


def _require_engine() -> _Engine:
    engine = _ENGINE
    if engine is None:
        raise DatabaseClosedError(_CLOSED_MSG)
    return engine


def init(db_path: Any, expected_version: int = DB_VERSION,
         idle_checkpoint_minutes: float = CHECKPOINT_IDLE_MINUTES) -> None:
    """初始化数据库引擎 (写线程 + 读连接池) , 并完成建表与根目录记录。

    :param db_path: SQLite 数据库文件路径。
    :param expected_version: 程序记录的数据库版本; 低于该值的库会被升级,
        高于该值抛 DatabaseVersionError (拒绝启动) 。
    :param idle_checkpoint_minutes: 写空闲多少分钟后自动合并 WAL;
        0 表示关闭。该值在初始化时读取, 修改后需重启程序生效。
    同路径重复调用为幂等操作; 不同路径重复调用会先关闭旧引擎。
    """
    global _ENGINE
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    path_key = _norm_db_path(db_path)
    # 迁移前准备: 先回写 WAL, 再备份主库 (必须在任何业务服务启动前完成)
    need_migration, current_version = migration_needed(
        db_file, int(expected_version))
    with _ENGINE_LOCK:
        current = _ENGINE
        if current is not None:
            if current.path_key == path_key:
                return  # 幂等: 同一路径重复初始化直接复用
            log.warning("Database re-initializing with a different path")
            _ENGINE = None
        engine = _Engine(db_file, idle_checkpoint_minutes)
        engine.path_key = path_key
        _ENGINE = engine
    try:
        if need_migration:
            future = engine.submit_checkpoint()
            if future is not None:
                result = future.result()
                if result and result[0]:
                    log.warning("WAL checkpoint before migration reported busy")
            backup_database(db_file, current_version)
        engine.submit_write(bootstrap, int(expected_version)).result()
    except BaseException:
        engine.close()
        with _ENGINE_LOCK:
            if _ENGINE is engine:
                _ENGINE = None
        raise


def close() -> None:
    """关闭数据库: 等待写队列清空、所有进行中任务完成后关闭全部连接。

    程序退出时必须调用; 重复调用为幂等操作。
    """
    global _ENGINE
    with _ENGINE_LOCK:
        engine = _ENGINE
        _ENGINE = None
    if engine is not None:
        engine.close()


def submit_write(fn, *args) -> Any:
    """提交写任务并阻塞等待 (保持既有同步调用风格) 。"""
    return _require_engine().submit_write(fn, *args).result()


def submit_read(fn, *args) -> Any:
    """提交读任务并阻塞等待 (保持既有同步调用风格) 。"""
    return _require_engine().submit_read(fn, *args).result()
