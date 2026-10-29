"""去重白名单: 过滤文件管理模块自身操作触发的 watchdog 事件。"""
from __future__ import annotations

import threading
import time
from enum import Enum

from scripts.file_manager.path_utils import norm_physical

WHITELIST_MATCH_WINDOW = 10.0  # 白名单事件匹配窗口 (秒)
WHITELIST_EXPIRE = 30.0        # 白名单条目过期时间 (秒)


class OpType(str, Enum):
    """去重白名单操作类型。"""
    CREATE_FILE = "CREATE_FILE"
    CREATE_DIR = "CREATE_DIR"
    RENAME_FILE = "RENAME_FILE"
    RENAME_DIR = "RENAME_DIR"
    DELETE_FILE = "DELETE_FILE"
    DELETE_DIR = "DELETE_DIR"


class DedupeWhitelist:
    """主动操作的物理路径白名单, 用于过滤自身触发的 watchdog 事件。

    结构: dict[规范化物理路径, (操作类型, 时间戳)], 线程安全。
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[OpType, float]] = {}
        self._lock = threading.Lock()

    def mark(self, physical_path: str, op: OpType) -> None:
        key = norm_physical(physical_path)
        now = time.time()
        with self._lock:
            # 惰性清理: 写入新条目时顺带清除超过 30 秒的过期条目
            stale = [k for k, (_, ts) in self._entries.items()
                     if now - ts > WHITELIST_EXPIRE]
            for k in stale:
                del self._entries[k]
            self._entries[key] = (op, now)

    def check(self, physical_path: str, op: OpType) -> bool:
        """路径在白名单中、操作类型匹配且时间差 < 10 秒时返回 True。"""
        key = norm_physical(physical_path)
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return False
            return (item[0] == op
                    and (time.time() - item[1]) < WHITELIST_MATCH_WINDOW)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
