"""文件系统监控服务: watchdog 注册、事件分发与数据库同步。"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from exceptions import PathNotFoundError
from scripts.file_manager.path_utils import (
    gallery_segments,
    norm_physical,
    should_ignore,
)
from scripts.file_manager.whitelist import OpType
from scripts.logger import log
from scripts.path_resolver import GALLERY, match_root

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager


class _WatchHandler(FileSystemEventHandler):
    def __init__(self, service: "WatchService") -> None:
        super().__init__()
        self._service = service

    def on_created(self, event) -> None:
        self._service.dispatch_event("created", event)

    def on_deleted(self, event) -> None:
        self._service.dispatch_event("deleted", event)

    def on_moved(self, event) -> None:
        self._service.dispatch_event("moved", event)


class WatchService:
    """递归监控 /gallery 与挂载根, 把磁盘变化同步到 DataBase。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager
        self._handler = _WatchHandler(self)
        self._observer = Observer()
        self._observer.daemon = True
        # 规范化物理根 -> (逻辑根, 原始物理根)
        self._roots: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ #
    # 注册 / 关闭
    # ------------------------------------------------------------------ #

    def register_gallery(self) -> None:
        self.start_watch(self._mgr.gallery_root, GALLERY)

    def start(self) -> None:
        self._observer.start()

    def shutdown(self) -> None:
        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception as e:  # noqa: BLE001 关闭阶段尽力而为
            log.error(f"Failed to stop watchdog observer: {e}")

    def start_watch(self, physical_root: str, logical_root: str):
        """注册监控, 返回 watch 句柄 (供后续 unschedule 移除); 失败返回 None。"""
        try:
            watch = self._observer.schedule(
                self._handler, physical_root, recursive=True)
        except Exception as e:  # noqa: BLE001 目录不可注册时按离线处理
            log.error(f"Failed to register watch for {physical_root}: {e}")
            return None
        self._roots[norm_physical(physical_root)] = (logical_root, physical_root)
        log.info(f"Watch registered: {logical_root} -> {physical_root}")
        return watch

    def unschedule(self, watch) -> None:
        self._observer.unschedule(watch)

    def remove_root(self, physical: str) -> None:
        self._roots.pop(norm_physical(physical), None)

    # ------------------------------------------------------------------ #
    # 事件分发
    # ------------------------------------------------------------------ #

    def locate_root(self, physical: str) -> tuple[str, str] | None:
        """物理路径归属到已注册的监控根 -> (逻辑根, 相对逻辑路径段串)。"""
        hit = match_root(physical, self._roots)
        if hit is None:
            return None
        logical_root, original_root = hit
        rel = os.path.relpath(physical, original_root)
        if rel == ".":
            return logical_root, ""
        parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
        return logical_root, "/".join(parts)

    def dispatch_event(self, kind: str, event: Any) -> None:
        try:
            self.handle_event(kind, event)
        except (OSError, PermissionError):
            # watchdog 回调中的 OSError/PermissionError 触发离线确认
            mount_key = self._mgr.resolver.physical_to_mount_key(event.src_path)
            if mount_key is not None:
                self._mgr.mounts.begin_offline_confirmation(mount_key)
            else:
                log.error(f"Filesystem event error: {event.src_path}")
        except Exception as e:  # noqa: BLE001 单个事件失败不能拖垮监控线程
            log.error(f"Filesystem event failed: {event.src_path} ({e})")

    def handle_event(self, kind: str, event: Any) -> None:
        mgr = self._mgr
        if mgr.shutting_down:
            return
        src = event.src_path
        dst = getattr(event, "dest_path", "") or ""
        if kind == "moved":
            # 原子写入 (临时文件 -> 目标重命名) 表现为 src 可忽略而 dst 有效的
            # moved 事件, 此时不能丢弃, 交由 on_moved 按 dst 新增处理
            if should_ignore(src) and should_ignore(dst):
                return
        elif should_ignore(src):
            return

        if kind in ("created", "deleted"):
            is_dir = event.is_directory
            if kind == "created":
                op_type = OpType.CREATE_DIR if is_dir else OpType.CREATE_FILE
                handler = self.on_created
            else:
                op_type = OpType.DELETE_DIR if is_dir else OpType.DELETE_FILE
                handler = self.on_deleted
            if mgr.whitelist.check(src, op_type):
                log.debug(f"Whitelist hit (event skipped): {src}")
                return
            located = self.locate_root(src)
            if located is None:
                return
            logical_root, rel = located
            if not rel:
                return
            handler(is_dir, logical_root, rel)
        elif kind == "moved":
            op_type = (OpType.RENAME_DIR if event.is_directory
                       else OpType.RENAME_FILE)
            if mgr.whitelist.check(dst, op_type) \
                    or mgr.whitelist.check(src, op_type):
                log.debug(f"Whitelist hit (event skipped): {src} -> {dst}")
                return
            self.on_moved(event.is_directory, src, dst)

    # ---- 事件处理 ---- #

    def on_created(self, is_dir: bool, logical_root: str, rel: str) -> None:
        mgr = self._mgr
        segments = gallery_segments(logical_root, rel)
        if segments is None:
            return
        if is_dir:
            mgr.db_helper.ensure_dir_records(segments)
        else:
            parent_uuid = mgr.db_helper.ensure_dir_records(segments[:-1])
            name = segments[-1]
            physical = mgr.resolver.segments_to_physical(segments, None)
            mgr.sha.run(mgr.operations.ingest_file_task,
                        parent_uuid, name, physical)

    def on_deleted(self, is_dir: bool, logical_root: str, rel: str) -> None:
        mgr = self._mgr
        segments = gallery_segments(logical_root, rel)
        if segments is None:
            return
        with mgr.db_lock:
            try:
                parent_uuid, _ = mgr.resolver.walk_segments(segments[:-1])
            except PathNotFoundError:
                return
            name = segments[-1]
            if is_dir:
                row = mgr.db_helper.get_child(parent_uuid, name)
                if row is not None:
                    mgr.db_helper.remove_dir_records(row["uuid"])
            else:
                mgr.db.delete_file(parent_uuid, name)
        if is_dir:
            mgr.readonly.build()

    def on_moved(self, is_dir: bool, src: str, dst: str) -> None:
        mgr = self._mgr
        src_located = self.locate_root(src)
        dst_located = self.locate_root(dst)

        if src_located is None and dst_located is None:
            return
        if src_located is None:
            # 从监控范围外移入: 按新增处理
            logical_root, rel = dst_located  # type: ignore[misc]
            if rel:
                self.on_created(is_dir, logical_root, rel)
            return
        if dst_located is None:
            # 移出监控范围: 按删除处理
            logical_root, rel = src_located
            if rel:
                self.on_deleted(is_dir, logical_root, rel)
            return

        src_root, src_rel = src_located
        dst_root, dst_rel = dst_located
        if not src_rel and not dst_rel:
            return
        src_segments = gallery_segments(src_root, src_rel)
        dst_segments = gallery_segments(dst_root, dst_rel)
        if src_segments is None or dst_segments is None:
            return

        with mgr.db_lock:
            try:
                src_parent_uuid, _ = mgr.resolver.walk_segments(src_segments[:-1])
            except PathNotFoundError:
                src_parent_uuid = None
            dst_parent_uuid = mgr.db_helper.ensure_dir_records(dst_segments[:-1])
            if is_dir:
                mgr.db_helper.move_dir_records(src_parent_uuid, src_segments,
                                               dst_segments, dst_parent_uuid)
            else:
                mgr.db_helper.move_file_records(src_parent_uuid, src_segments,
                                                dst_segments, dst_parent_uuid)
        if is_dir:
            mgr.readonly.build()


__all__ = ["WatchService"]
