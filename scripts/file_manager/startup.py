"""启动对账: 程序关闭期间的文件系统改动。"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from exceptions import PathNotFoundError
from scripts.file_manager.path_utils import is_accessible, should_ignore
from scripts.logger import log
from scripts.path_resolver import GALLERY

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager


class StartupReconciler:
    """比对磁盘与数据库, 补齐关闭期间新增/变化的文件并清理失效记录。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager

    def run(self) -> None:
        """后台对账 /gallery 与全部在线挂载点。"""
        mgr = self._mgr
        roots = [(mgr.gallery_root, GALLERY)]
        for mount_key, mc in mgr.resolver.iter_mounts():
            roots.append((mc.physical_path, GALLERY + mount_key))
        for physical, logical in roots:
            if not is_accessible(physical):
                log.warning(f"Startup sync skipped (inaccessible): "
                            f"{logical} -> {physical}")
                continue
            segments = [s for s in logical[len(GALLERY):].split("/") if s]
            try:
                self.sync_dir(physical, segments)
            except Exception as e:  # noqa: BLE001 单个根失败不影响其他根
                log.error(f"Startup sync failed: {logical} ({e})")
        log.info("Startup database sync completed")

    def is_mount_point(self, segments: list[str]) -> bool:
        """逻辑段列表是否恰好是一个已配置的挂载点。"""
        key = self._mgr.resolver.mount_key_of(segments)
        return key is not None and key == "/" + "/".join(segments)

    def sync_dir(self, physical_dir: str, segments: list[str]) -> None:
        """递归对账单个目录: 磁盘条目补齐入库, 数据库失效记录移除。

        挂载点（及其阴影目录）由挂载根自身负责对账, 此处跳过,
        避免本地空目录把外部挂载中的文件记录误删。
        """
        mgr = self._mgr
        try:
            with mgr.db_lock:
                dir_uuid, _ = mgr.resolver.walk_segments(segments)
        except PathNotFoundError as e:
            log.warning(f"Startup sync skipped, no path record: "
                        f"{'/'.join(segments) or '/'} ({e})")
            return
        try:
            entries = list(os.scandir(physical_dir))
        except OSError as e:
            log.warning(f"Startup sync cannot read directory: "
                        f"{physical_dir} ({e})")
            return

        dir_names: set[str] = set()
        file_names: set[str] = set()
        for entry in entries:
            name = entry.name
            if should_ignore(name):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                dir_names.add(name)
                child_segments = segments + [name]
                if self.is_mount_point(child_segments):
                    # 挂载点的本地阴影目录不参与对账 (含目录不存在的情况)
                    continue
                try:
                    with mgr.db_lock:
                        mgr.db_helper.ensure_dir_records(child_segments)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Startup sync failed to register directory: "
                                f"{entry.path} ({e})")
                    continue
                self.sync_dir(entry.path, child_segments)
            else:
                file_names.add(name)
                self.sync_file(dir_uuid, name, entry.path)

        # 移除磁盘上已不存在的记录; 删除前再确认一次, 避免与并发新增竞态
        with mgr.db_lock:
            for child in mgr.db.list_paths(dir_uuid):
                if child["name"] in dir_names:
                    continue
                if self.is_mount_point(segments + [child["name"]]):
                    continue  # 挂载节点不依赖本地阴影目录是否存在
                if os.path.isdir(os.path.join(physical_dir, child["name"])):
                    continue
                log.info(f"Startup sync removed directory record: "
                         f"{'/'.join(segments + [child['name']])}")
                mgr.db_helper.remove_dir_records(child["uuid"])
            for row in mgr.db.list_files_by_path(dir_uuid):
                if row["filename"] in file_names:
                    continue
                if os.path.exists(os.path.join(physical_dir, row["filename"])):
                    continue
                log.info(f"Startup sync removed file record: "
                         f"{'/'.join(segments + [row['filename']])}")
                mgr.db.delete_file(dir_uuid, row["filename"])

    def sync_file(self, parent_uuid: str, name: str, physical: str) -> None:
        """单个文件对账: 缺失则入库; 大小变化视为内容替换, 重新入库。"""
        mgr = self._mgr
        try:
            size = os.path.getsize(physical)
        except OSError:
            return
        with mgr.db_lock:
            row = mgr.db_helper.get_file_row(parent_uuid, name)
        if row is not None and int(row.get("file_size") or 0) == size:
            return
        if row is not None:
            log.info(f"Startup sync re-ingesting changed file: {physical}")
            with mgr.db_lock:
                mgr.db.delete_file(parent_uuid, name)
        if size > 0:
            mgr.sha.run(mgr.operations.ingest_file_task,
                        parent_uuid, name, physical)


__all__ = ["StartupReconciler"]
