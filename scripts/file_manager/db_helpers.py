"""数据库交互辅助。

集中实现文件管理模块对 DataBase 的组合操作 (路径节点维护、文件记录迁移、
元数据写回等), 使文件操作/监控模块不直接散落数据库逻辑。
"""
from __future__ import annotations

import uuid as uuid_lib
from typing import TYPE_CHECKING, Any

from exceptions import PathNotFoundError
from scripts.logger import log
from scripts.path_resolver import GALLERY

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager

# 元数据解析器可更新的 info 字段 (尺寸/大小属于 file 表)
META_UPDATABLE_FIELDS = {
    "positive_prompt", "negative_prompt", "seed", "steps", "cfg",
    "sampler", "scheduler", "tool", "rating", "note",
}
# 元数据解析器可更新的 file 字段 (单个文件的尺寸/大小)
META_FILE_FIELDS = {"width", "height", "file_size"}


class DbHelper:
    """以 FileManager 的数据库/锁/路径解析器为上下文的组合查询与写入。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager

    # ---- 基础查询 ----
    def get_child(self, parent_uuid: str,
                  name: str) -> dict[str, Any] | None:
        """按名称取直接子路径节点 (调用方需持有/可重入 db_lock)。"""
        return next(
            (c for c in self._mgr.db.list_paths(parent_uuid)
             if c["name"] == name),
            None)

    def get_file_row(self, parent_uuid: str,
                     filename: str) -> dict[str, Any] | None:
        """按文件名取该目录下的文件记录 (调用方需持有/可重入 db_lock)。"""
        return next(
            (r for r in self._mgr.db.list_files_by_path(parent_uuid)
             if r["filename"] == filename),
            None)

    # ---- info / file 记录 ----
    def get_or_create_info(self, sha256: str) -> int:
        """按 SHA256 复用 info 记录; 不存在则以空元数据创建 (rating=1)。"""
        db = self._mgr.db
        existing = db.get_info_by_sha256(sha256)
        if existing is not None:
            return int(existing["id"])
        return int(db.create_info(
            sha256, seed=0, steps=0, cfg=0.0, sampler="", scheduler="",
            tool="", rating=1, positive_prompt="", negative_prompt="",
            note=""))

    def rename_file_record(self, parent_uuid: str, old_name: str,
                           new_name: str) -> None:
        """file 表重命名 (保留 file.id 与文件-模型关联)。"""
        row = self.get_file_row(parent_uuid, old_name)
        if row is None:
            log.warning(f"No database record found for file, "
                        f"rename skipped: {old_name}")
            return
        if not self._mgr.db.rename_file(parent_uuid, old_name, new_name):
            log.warning(f"File record rename failed: {old_name} -> {new_name}")

    # ---- 目录记录 ----
    def remove_dir_records(self, dir_uuid: str) -> None:
        """递归删除路径节点子树记录: 先文件记录, 后路径记录 (自底向上)。"""
        db = self._mgr.db
        for f in db.list_files_by_path(dir_uuid):
            db.delete_file(dir_uuid, f["filename"])
        for child in db.list_paths(dir_uuid):
            self.remove_dir_records(child["uuid"])
        db.delete_path(dir_uuid)

    def ensure_dir_records(self, segments: list[str]) -> str:
        """确保 /gallery 各级目录节点存在 (缺失时以 inbuilt 逐级创建)。"""
        mgr = self._mgr
        with mgr.db_lock:
            root = mgr.db.get_root_path()
            if root is None:
                raise PathNotFoundError("Root path record is missing")
            current = root["uuid"]
            lp = GALLERY
            for seg in segments:
                child = self.get_child(current, seg)
                if child is None:
                    new_uuid = str(uuid_lib.uuid4())
                    mgr.db.add_path(seg, path_type="inbuilt",
                                    parent_uuid=current, path_uuid=new_uuid)
                    new_lp = lp + "/" + seg
                    mgr.readonly.cache[new_lp] = \
                        mgr.readonly.cache.get(lp, False)
                    current = new_uuid
                else:
                    current = child["uuid"]
                lp += "/" + seg
            return current

    # ---- 移动事件的记录迁移 ----
    def move_dir_records(self, src_parent_uuid: str | None,
                         src_segments: list[str],
                         dst_segments: list[str],
                         dst_parent_uuid: str) -> None:
        """目录移动的记录迁移: 更新节点 parent_uuid/name, 保留子树与文件 id。"""
        mgr = self._mgr
        name_old, name_new = src_segments[-1], dst_segments[-1]
        row = (self.get_child(src_parent_uuid, name_old)
               if src_parent_uuid is not None else None)
        if row is None:
            # 旧记录不存在: 按新增处理整棵子树
            self.ensure_dir_records(dst_segments)
            mgr.operations.reregister_subtree(
                mgr.resolver.segments_to_physical(dst_segments, None),
                dst_segments)
        elif src_segments[:-1] == dst_segments[:-1]:
            # 同级重命名: 只需更新 name 字段
            mgr.db.update_path(row["uuid"], name=name_new)
        else:
            # 跨目录移动: 只迁移目录节点, 子树记录与文件-模型关联保持不变
            mgr.db.move_path(row["uuid"], dst_parent_uuid, name_new)

    def move_file_records(self, src_parent_uuid: str | None,
                          src_segments: list[str],
                          dst_segments: list[str],
                          dst_parent_uuid: str) -> None:
        """文件移动的记录迁移: 同级改名、跨目录重挂 info, 或按新增入库。"""
        mgr = self._mgr
        name_old, name_new = src_segments[-1], dst_segments[-1]
        row_file = (self.get_file_row(src_parent_uuid, name_old)
                    if src_parent_uuid is not None else None)
        if src_segments[:-1] == dst_segments[:-1]:
            if row_file is not None:
                self.rename_file_record(src_parent_uuid, name_old, name_new)
            else:
                # 磁盘上存在但无记录: 按新增入库
                physical = mgr.resolver.segments_to_physical(dst_segments, None)
                mgr.sha.run(mgr.operations.ingest_file_task,
                            dst_parent_uuid, name_new, physical)
        elif row_file is not None:
            # 内容未变化: 原地迁移文件记录, 保留 file.id 与模型关联
            if not mgr.db.relocate_file(src_parent_uuid, name_old,
                                        dst_parent_uuid, name_new):
                log.warning(f"File record move failed: "
                            f"{name_old} -> {name_new}")
        else:
            physical = mgr.resolver.segments_to_physical(dst_segments, None)
            mgr.sha.run(mgr.operations.ingest_file_task,
                        dst_parent_uuid, name_new, physical)

    # ---- 元数据写回 ----
    def apply_metadata(self, path_uuid: str, filename: str, info_id: int,
                       meta: dict[str, Any], physical: str) -> None:
        """把解析结果写入数据库: info 字段 + 文件尺寸 + 模型绑定。"""
        mgr = self._mgr
        # models 不在白名单内, 过滤天然将其挡在 update_info 之外
        models = meta.get("models")
        updates = {k: v for k, v in meta.items() if k in META_UPDATABLE_FIELDS}
        file_updates = {k: v for k, v in meta.items() if k in META_FILE_FIELDS}
        try:
            with mgr.db_lock:
                if updates:
                    mgr.db.update_info(info_id, **updates)
                if file_updates:
                    mgr.db.update_file(path_uuid, filename, **file_updates)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to write metadata: {physical} ({e})")
        if isinstance(models, dict):
            try:
                with mgr.db_lock:
                    mgr.db.update_file_models(
                        path_uuid, filename,
                        checkpoints=models.get("checkpoints"),
                        loras=models.get("loras"),
                        embeddings=models.get("embeddings"),
                        vaes=models.get("vaes"),
                        upscales=models.get("upscales"),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(f"Failed to write model bindings: {physical} ({e})")


__all__ = ["DbHelper", "META_FILE_FIELDS", "META_UPDATABLE_FIELDS"]
