"""Web 后端共享状态: 核心模块引用 + 上传任务注册表。"""
import threading
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AppState:
    """进程级依赖容器 (由 main.py 组装后注入 FastAPI) 。"""

    config: Any                  # ConfigManager
    db: Any                      # DataBase
    fm: Any                      # FileManager
    thumb: Any                   # ThumbnailGenerator
    read_only: bool = False
    program_version: str = "0.0.0"
    db_version: int = 0
    started_at: int = field(default_factory=lambda: int(time.time()))
    # 上传任务注册表: task_id -> {"target": str, "created": int,
    #   "files": {token: {"name","mime","sha256","size","chunks","finalizing"}}}
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)
    upload_lock: threading.Lock = field(default_factory=threading.Lock)

    # 上传任务过期时间 (秒): 超过该时长未完成的任务视为废弃
    UPLOAD_TTL = 24 * 3600

    def new_upload_task(self, target: str) -> dict[str, Any]:
        task = {"target": target, "created": int(time.time()), "files": {}}
        return task

    def purge_stale_uploads(self) -> None:
        """惰性清理过期上传任务 (分块临时文件同样丢弃) 。"""
        deadline = int(time.time()) - self.UPLOAD_TTL
        with self.upload_lock:
            stale = [tid for tid, t in self.uploads.items()
                     if t["created"] < deadline]
            dropped = [self.uploads.pop(tid) for tid in stale]
        for task in dropped:
            for entry in task["files"].values():
                for tmp in entry["chunks"].values():
                    try:
                        self.fm.discard_upload(tmp)
                    except Exception:  # noqa: BLE001 清理尽力而为
                        pass
