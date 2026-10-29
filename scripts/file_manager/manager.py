"""FileManager 门面: 组装子模块, 管理启动/关闭并提供对外服务。"""
from __future__ import annotations

import os
import threading
from typing import Any, Callable, Literal

from exceptions import PathNotFoundError
from scripts.file_manager.db_helpers import DbHelper
from scripts.file_manager.mount_manager import MountManager
from scripts.file_manager.operations import FileOperations
from scripts.file_manager.path_utils import join_logical_path
from scripts.file_manager.readonly import ReadonlyGuard
from scripts.sha256 import Sha256Service
from scripts.file_manager.startup import StartupReconciler
from scripts.file_manager.upload import UploadService
from scripts.file_manager.watchdog import WatchService
from scripts.file_manager.whitelist import DedupeWhitelist
from scripts.logger import log
from scripts.path_resolver import PathResolver


class FileManager:
    """文件系统操作唯一入口: 组装并协调各子模块。"""

    def __init__(
        self,
        db,                        # DataBase 实例 (查询与通知)
        gallery_root: str,         # GALLERY_ROOT 物理路径
        cache_root: str,           # CACHE_FOLDER 物理路径
        upload_folder: str,        # UPLOAD_FOLDER (名称或完整路径)
        mount_configs: dict,       # {挂载逻辑路径: MountConfig}
        read_only: bool = False,   # 程序级只读标志
    ) -> None:
        self.db = db
        self.gallery_root = os.path.abspath(gallery_root)
        self.cache_root = os.path.abspath(cache_root)
        # upload_folder 传名称时挂到 /cache 下, 传绝对路径则直接使用
        self.upload_folder = os.path.join(self.cache_root, upload_folder)
        self.read_only = bool(read_only)
        self.shutting_down = False

        # 线程安全
        self.db_lock = threading.RLock()       # 串行化所有 DataBase 调用

        # 共享基础设施
        self.whitelist = DedupeWhitelist()
        self.resolver = PathResolver(
            db, mount_configs, self.gallery_root, self.db_lock)
        self.sha = Sha256Service(4)
        self.readonly = ReadonlyGuard(self)
        self.readonly.build()

        # 元数据解析器 (预留接口, 尚未编写时容错跳过)
        try:
            from scripts.parser_scheduler import parse_metadata  # type: ignore
            self.parse_metadata = parse_metadata
        except ImportError:
            self.parse_metadata = None

        # 子模块 (顺序: 底层辅助 -> 业务 -> 监控)
        self.db_helper = DbHelper(self)
        self.operations = FileOperations(self)
        self.upload = UploadService(self)
        self.watch = WatchService(self)
        self.mounts = MountManager(self)
        self.startup = StartupReconciler(self)

        # 监控: 内置 gallery 根 + 各挂载根, 然后启动 observer
        self.watch.register_gallery()
        for mount_key, mc in self.resolver.iter_mounts():
            self.mounts.init_mount_watch(mount_key, mc)
        self.watch.start()

        # 程序关闭期间的文件增删改无法由 watchdog 感知, 启动后后台对账一次
        self.sha.run(self.startup.run)

        log.info(f"FileManager initialized (gallery={self.gallery_root}, "
                 f"cache={self.cache_root}, read_only={self.read_only})")

    # ------------------------------------------------------------------ #
    # 关闭 / 挂载配置
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """停止监控、取消定时任务、关闭线程池并清理临时数据。"""
        self.shutting_down = True
        self.watch.shutdown()
        self.mounts.shutdown()
        self.sha.shutdown(wait=True, cancel_futures=True)
        self.upload.shutdown()
        self.whitelist.clear()
        # 读取流 (read_file) 由调用者负责关闭, 本模块不做跟踪回收
        log.info("FileManager shut down")

    def update_mount_configs(self, mount_configs: dict) -> None:
        """挂载配置变更时更新内存配置、只读缓存并补注册新挂载点监控。"""
        self.mounts.update_mount_configs(mount_configs)

    # ------------------------------------------------------------------ #
    # 路径解析 (供子模块复用的公共骨架)
    # ------------------------------------------------------------------ #

    def resolve_parent(
            self, kind: str, segments: list[str],
            destination: Literal["mount", "local"] | None = None,
            guard: bool = False, must_exist: bool = False,
            guard_segments: list[str] | None = None,
            ) -> tuple[str, str | None, str | None]:
        """逻辑父目录解析 (创建/重命名/删除/读写共用的公共骨架)。"""
        if kind == "cache":
            parent_phys = os.path.join(self.cache_root, *segments)
            if must_exist and not os.path.isdir(parent_phys):
                raise PathNotFoundError(
                    f"Logical path not found: {join_logical_path(kind, segments)}")
            return parent_phys, None, None
        if guard:
            self.readonly.check(
                guard_segments if guard_segments else segments, destination)
        parent_uuid, parent_lp = self.resolver.walk_segments(segments)
        parent_phys = self.resolver.segments_to_physical(segments, destination)
        return parent_phys, parent_uuid, parent_lp

    # ------------------------------------------------------------------ #
    # 对外文件操作
    # ------------------------------------------------------------------ #

    def create_folder(self, parent_path: str, folder_name: str,
                      destination: Literal["mount", "local"] | None = None) -> str:
        return self.operations.create_folder(parent_path, folder_name,
                                             destination)

    def create_file(self, parent_path: str, filename: str,
                    destination: Literal["mount", "local"] | None = None) -> str:
        return self.operations.create_file(parent_path, filename, destination)

    def rename_file(self, parent_path: str, old_name: str, new_name: str,
                    destination: Literal["mount", "local"] | None = None) -> None:
        self.operations.rename_file(parent_path, old_name, new_name, destination)

    def rename_folder(self, parent_path: str, old_name: str, new_name: str,
                      destination: Literal["mount", "local"] | None = None) -> None:
        self.operations.rename_folder(parent_path, old_name, new_name,
                                      destination)

    def delete_file(self, parent_path: str, filename: str,
                    destination: Literal["mount", "local"] | None = None) -> None:
        self.operations.delete_file(parent_path, filename, destination)

    def delete_folder(self, parent_path: str, folder_name: str,
                      confirm: bool = False,
                      destination: Literal["mount", "local"] | None = None) -> None:
        self.operations.delete_folder(parent_path, folder_name, confirm,
                                      destination)

    def move_entry(self, source_parent: str, name: str, target_parent: str,
                   is_dir: bool = False,
                   destination: Literal["mount", "local"] | None = None) -> str:
        return self.operations.move_entry(source_parent, name, target_parent,
                                          is_dir, destination)

    def copy_entry(self, source_parent: str, name: str, target_parent: str,
                   is_dir: bool = False,
                   destination: Literal["mount", "local"] | None = None) -> str:
        return self.operations.copy_entry(source_parent, name, target_parent,
                                          is_dir, destination)

    def entry_stat(self, parent_path: str,
                   name: str) -> dict[str, Any] | None:
        return self.operations.entry_stat(parent_path, name)

    def folder_stats(self, parent_path: str) -> dict[str, int]:
        return self.operations.folder_stats(parent_path)

    def read_file(self, parent_path: str, filename: str):
        return self.operations.read_file(parent_path, filename)

    def file_exists(self, parent_path: str, filename: str) -> bool:
        return self.operations.file_exists(parent_path, filename)

    def write_file(self, parent_path: str, filename: str, data: bytes) -> None:
        self.operations.write_file(parent_path, filename, data)

    def list_files(self, parent_path: str) -> list[dict[str, Any]]:
        return self.operations.list_files(parent_path)

    def compute_sha256(
            self, parent_path: str, filename: str,
            callback: Callable[[str | None, str | None], None]) -> None:
        self.operations.compute_sha256(parent_path, filename, callback)

    def reparse_metadata(self, parent_path: str,
                         filename: str) -> dict[str, Any]:
        return self.operations.reparse_metadata(parent_path, filename)

    def reparse_folder_metadata(self, folder_path: str,
                                recursive: bool = False) -> dict[str, int]:
        return self.operations.reparse_folder_metadata(folder_path, recursive)

    # ------------------------------------------------------------------ #
    # 上传流程
    # ------------------------------------------------------------------ #

    def discard_upload(self, source_filename: str) -> None:
        self.upload.discard_upload(source_filename)

    def open_upload_file(self, original_filename: str,
                         target_parent: str):
        return self.upload.open_upload_file(original_filename, target_parent)

    def upload_move(self, source_filename: str, target_parent: str,
                    sha256: str,
                    destination: Literal["mount", "local"] | None = None,
                    callback: Callable[[str | None, str | None], None] | None = None
                    ) -> None:
        self.upload.upload_move(source_filename, target_parent, sha256,
                                destination, callback)
