"""sha256.py — SHA256 计算模块。

职责: 
- compute_file_sha256: 分块计算文件 SHA256 的纯函数 (mtime 变化时丢弃重算, 
  最多重试 SHA_MAX_RETRY 次; ≤ SMALL_FILE_LIMIT 一次性读入内存) 
- Sha256Service: 4 线程池计算服务。异步哈希回调 (compute_sha256 对外接口) 
  与文件管理模块的入库/元数据解析后台任务共用同一池, 保持线程预算与
  shutdown 等待语义不变

本模块不依赖 path_resolver / file_manager, 不做任何数据库访问。
"""
import hashlib
import os
import re
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from scripts.logger import log

CHUNK_SIZE = 8 * 1024 * 1024          # SHA256 / 复制分块: 8MB
SMALL_FILE_LIMIT = 8 * 1024 * 1024    # ≤8MB 一次性读入内存
SHA_MAX_RETRY = 3                     # mtime 变化时最多重新计算次数

# 空文件 (空内容) 的 SHA256
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def normalize_sha256(value: Any) -> str | None:
    """规范化 SHA256 输入 (strip + lower) ; 格式非法 (非 64 位十六进制) 返回 None。"""
    key = str(value or "").strip().lower()
    return key if _SHA256_RE.fullmatch(key) else None


def compute_file_sha256(physical: str) -> tuple[str | None, str | None]:
    """计算文件 SHA256; mtime 变化时丢弃重算, 最多重试 3 次。

    ≤8MB 一次性读入内存; >8MB 按 8MB 分块, 每块读完检查 mtime。
    返回 (hexdigest, None) 或 (None, 错误描述)。纯函数, 无线程与锁。
    """
    retries = 0
    while True:
        try:
            stat0 = os.stat(physical)
        except OSError:
            return None, "file_deleted"
        mtime0 = stat0.st_mtime
        hasher = hashlib.sha256()
        changed = False
        try:
            with open(physical, "rb") as f:
                if stat0.st_size <= SMALL_FILE_LIMIT:
                    hasher.update(f.read())
                    changed = os.stat(physical).st_mtime != mtime0
                else:
                    while True:
                        chunk = f.read(CHUNK_SIZE)
                        if not chunk:
                            break
                        hasher.update(chunk)
                        if os.stat(physical).st_mtime != mtime0:
                            changed = True
                            break
        except OSError:
            return None, "file_deleted"

        if not changed:
            return hasher.hexdigest(), None
        retries += 1
        if retries > SHA_MAX_RETRY:
            return None, "file_modified_unstable"


class Sha256Service:
    """SHA256 计算服务: 固定大小线程池。"""

    def __init__(self, max_workers: int = 4):
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="sha256")

    def compute(self, physical: str) -> tuple[str | None, str | None]:
        """在调用线程同步计算 (供入库流程等后台任务内复用) 。"""
        return compute_file_sha256(physical)

    def submit(self, physical: str,
               callback: Callable[[str | None, str | None], None]) -> Future:
        """异步计算文件 SHA256, 完成后在工作线程回调 callback(digest, error)。"""
        return self._executor.submit(self._task, physical, callback)

    def run(self, fn: Callable, *args: Any) -> Future:
        """把任意后台任务提交到本池 (供文件管理模块的入库/解析任务复用) 。"""
        return self._executor.submit(fn, *args)

    def _task(self, physical: str,
              callback: Callable[[str | None, str | None], None]) -> None:
        digest, error = compute_file_sha256(physical)
        if digest is not None:
            log.debug(f"SHA256 计算完成: {physical}")
        else:
            log.warning(f"SHA256 计算失败: {physical} ({error})")
        try:
            callback(digest, error)
        except Exception as e:  # noqa: BLE001 回调异常不应影响线程池
            log.error(f"SHA256 回调执行异常: {e}")

    def shutdown(self, wait: bool = True, cancel_futures: bool = True) -> None:
        """关闭线程池 (程序退出时调用) 。"""
        self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
