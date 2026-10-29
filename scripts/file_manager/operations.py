"""文件 CRUD 操作与入库/元数据解析流程。"""
from __future__ import annotations

import io
import os
import shutil
import uuid as uuid_lib
from typing import TYPE_CHECKING, Any, Callable, Literal

from exceptions import (
    CrossRootError,
    DirectoryNotEmptyError,
    FileManagerError,
    InvalidPathError,
    NameConflictError,
    PathNotFoundError,
)
from exceptions import FileNotFoundError as FMFileNotFound
from scripts.file_manager.disk_utils import rmtree, touch, wait_file_ready
from scripts.file_manager.path_utils import (
    guess_mime,
    join_logical_path,
    should_ignore,
    split_logical_path,
)
from scripts.sha256 import EMPTY_SHA256
from scripts.file_manager.whitelist import OpType
from scripts.image_probe import probe_image_size
from scripts.logger import log
from scripts.path_resolver import validate_name

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager

INGEST_MAX_RETRY = 3


class FileOperations:
    """文件创建/重命名/删除/移动/读写与入库、元数据解析。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager

    # ------------------------------------------------------------------ #
    # 创建
    # ------------------------------------------------------------------ #

    def create_folder(self, parent_path: str, folder_name: str,
                      destination: Literal["mount", "local"] | None = None) -> str:
        """创建文件夹, 返回新文件夹的逻辑路径。"""
        return self._create_entry(parent_path, folder_name,
                                  destination, is_dir=True)

    def create_file(self, parent_path: str, filename: str,
                    destination: Literal["mount", "local"] | None = None) -> str:
        """创建空文件, 返回新文件的逻辑路径。"""
        return self._create_entry(parent_path, filename,
                                  destination, is_dir=False)

    def _create_entry(self, parent_path: str, name: str,
                      destination: Literal["mount", "local"] | None,
                      is_dir: bool) -> str:
        """创建文件夹/空文件的公共流程: 校验 -> 冲突检查 -> 落盘 -> (gallery) 入库。"""
        mgr = self._mgr
        validate_name(name)
        kind, segments = split_logical_path(parent_path)
        new_lp = join_logical_path(kind, segments + [name])
        label = "Folder" if is_dir else "File"
        parent_phys, parent_uuid, parent_lp = mgr.resolve_parent(
            kind, segments, destination, guard=True,
            must_exist=(kind == "cache"))
        target = os.path.join(parent_phys, name)
        if os.path.exists(target):
            raise NameConflictError(f"{label} already exists: {new_lp}")

        op_type = OpType.CREATE_DIR if is_dir else OpType.CREATE_FILE
        if kind == "cache":
            touch(target, is_dir)
            log.info(f"{label} created: {new_lp}")
            return new_lp

        # /gallery: 落盘后通知 DataBase
        mgr.whitelist.mark(target, op_type)
        # destination="local" 时本地影子目录可能尚不存在, 逐级补齐
        os.makedirs(parent_phys, exist_ok=True)
        touch(target, is_dir)
        with mgr.db_lock:
            if is_dir:
                mgr.db.add_path(name, path_type="inbuilt",
                                parent_uuid=parent_uuid,
                                path_uuid=str(uuid_lib.uuid4()))
            else:
                info_id = mgr.db_helper.get_or_create_info(EMPTY_SHA256)
                mgr.db.add_file(info_id, parent_uuid, name, guess_mime(name),
                                file_size=0)
        mgr.readonly.cache[new_lp] = mgr.readonly.cache.get(parent_lp, False)
        log.info(f"{label} created: {new_lp}")
        return new_lp

    # ------------------------------------------------------------------ #
    # 重命名
    # ------------------------------------------------------------------ #

    def rename_file(self, parent_path: str, old_name: str, new_name: str,
                    destination: Literal["mount", "local"] | None = None) -> None:
        """重命名文件。"""
        self._rename(parent_path, old_name, new_name, destination, is_dir=False)

    def rename_folder(self, parent_path: str, old_name: str, new_name: str,
                      destination: Literal["mount", "local"] | None = None) -> None:
        """重命名文件夹 (只更新路径表 name 字段, 子路径无需逐条更新)。"""
        self._rename(parent_path, old_name, new_name, destination, is_dir=True)

    def _rename(self, parent_path: str, old_name: str, new_name: str,
                destination: Literal["mount", "local"] | None,
                is_dir: bool) -> None:
        """重命名文件/文件夹的公共流程。"""
        mgr = self._mgr
        validate_name(new_name)
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        label = "Folder" if is_dir else "File"
        # 文件夹级只读检查针对自身 (segments + old_name), 文件级针对父目录
        parent_phys, parent_uuid, _ = mgr.resolve_parent(
            kind, segments, destination, guard=(kind == "gallery"),
            guard_segments=segments + [old_name] if is_dir else None)

        if kind == "cache":
            from scripts.file_manager.disk_utils import rename_on_disk
            rename_on_disk(parent_phys, old_name, new_name, lp)
            log.info(f"{label} renamed: {lp}/{old_name} -> {new_name}")
            return

        src = os.path.join(parent_phys, old_name)
        dst = os.path.join(parent_phys, new_name)
        entry_exists = os.path.isdir if is_dir else os.path.isfile
        if not entry_exists(src):
            raise FMFileNotFound(f"{label} not found: {lp}/{old_name}")
        if os.path.exists(dst):
            raise NameConflictError(f"{label} name already taken: {lp}/{new_name}")

        # 文件夹需先确认路径表记录存在, 再落盘重命名
        dir_row = None
        if is_dir:
            with mgr.db_lock:
                dir_row = mgr.db_helper.get_child(parent_uuid, old_name)
            if dir_row is None:
                raise PathNotFoundError(f"Logical path not found: {lp}/{old_name}")

        op_type = OpType.RENAME_DIR if is_dir else OpType.RENAME_FILE
        mgr.whitelist.mark(src, op_type)
        mgr.whitelist.mark(dst, op_type)
        os.replace(src, dst)
        with mgr.db_lock:
            if is_dir:
                mgr.db.update_path(dir_row["uuid"], name=new_name)
            else:
                mgr.db_helper.rename_file_record(parent_uuid, old_name, new_name)
        if is_dir:
            mgr.readonly.build()  # 子路径键前缀变化, 整体重建
        log.info(f"{label} renamed: {lp}/{old_name} -> {new_name}")

    # ------------------------------------------------------------------ #
    # 删除
    # ------------------------------------------------------------------ #

    def delete_file(self, parent_path: str, filename: str,
                    destination: Literal["mount", "local"] | None = None) -> None:
        """删除文件。"""
        self._delete_entry(parent_path, filename, confirm=False,
                           destination=destination, is_dir=False)

    def delete_folder(self, parent_path: str, folder_name: str,
                      confirm: bool = False,
                      destination: Literal["mount", "local"] | None = None) -> None:
        """删除文件夹; 非空删除必须显式 confirm=True。"""
        self._delete_entry(parent_path, folder_name, confirm=confirm,
                           destination=destination, is_dir=True)

    def _delete_entry(self, parent_path: str, name: str, confirm: bool,
                      destination: Literal["mount", "local"] | None,
                      is_dir: bool) -> None:
        """删除文件/文件夹的公共流程。"""
        mgr = self._mgr
        validate_name(name)  # 拒绝 ".." / 路径分隔符, 防止路径穿越
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        label = "Folder" if is_dir else "File"
        parent_phys, parent_uuid, _ = mgr.resolve_parent(
            kind, segments, destination, guard=(kind == "gallery"),
            guard_segments=segments + [name] if is_dir else None)

        target = os.path.join(parent_phys, name)
        entry_exists = os.path.isdir if is_dir else os.path.isfile
        if not entry_exists(target):
            raise FMFileNotFound(f"{label} not found: {lp}/{name}")
        if is_dir and os.listdir(target) and not confirm:
            raise DirectoryNotEmptyError(
                f"Folder is not empty, pass confirm=True to delete: {lp}/{name}")

        dir_row = None
        if kind == "cache":
            if is_dir:
                rmtree(target)
            else:
                os.remove(target)
            log.info(f"{label} deleted: {lp}/{name}")
            return

        if is_dir:
            with mgr.db_lock:
                dir_row = mgr.db_helper.get_child(parent_uuid, name)
            if dir_row is None:
                raise PathNotFoundError(f"Logical path not found: {lp}/{name}")
            # 递归删除前, 将整棵子树写入白名单 (子项事件同样需要去重)
            for dirpath, _dirnames, filenames in os.walk(target):
                mgr.whitelist.mark(dirpath, OpType.DELETE_DIR)
                for fn in filenames:
                    mgr.whitelist.mark(os.path.join(dirpath, fn),
                                       OpType.DELETE_FILE)
            rmtree(target)
        else:
            mgr.whitelist.mark(target, OpType.DELETE_FILE)
            os.remove(target)

        with mgr.db_lock:
            if is_dir:
                # 自底向上逐条通知 DataBase: 先文件记录, 后路径记录
                mgr.db_helper.remove_dir_records(dir_row["uuid"])
            else:
                mgr.db.delete_file(parent_uuid, name)
        if is_dir:
            mgr.readonly.build()
        log.info(f"{label} deleted: {lp}/{name}")

    # ------------------------------------------------------------------ #
    # 移动
    # ------------------------------------------------------------------ #

    def move_entry(self, source_parent: str, name: str, target_parent: str,
                   is_dir: bool = False,
                   destination: Literal["mount", "local"] | None = None) -> str:
        """跨目录移动文件/文件夹 (仅 /gallery), 返回新逻辑路径。"""
        mgr = self._mgr
        validate_name(name)
        src_kind, src_segments = split_logical_path(source_parent)
        dst_kind, dst_segments = split_logical_path(target_parent)
        if src_kind != "gallery" or dst_kind != "gallery":
            raise CrossRootError(
                f"Move is only supported within /gallery: "
                f"{source_parent} -> {target_parent}")
        if src_segments == dst_segments:
            raise InvalidPathError(
                f"Source and target directories are identical: {source_parent}")
        if is_dir and dst_segments[:len(src_segments) + 1] == src_segments + [name]:
            raise InvalidPathError(
                f"Cannot move a folder into its own subtree: {target_parent}")

        label = "Folder" if is_dir else "File"
        src_lp = join_logical_path("gallery", src_segments + [name])
        # 源操作始终作用于实际位置 (挂载内即挂载物理路径), 不触发写入目标选择
        src_parent_phys, src_parent_uuid, _ = mgr.resolve_parent(
            src_kind, src_segments, "mount", guard=True,
            guard_segments=src_segments + [name] if is_dir else None)
        dst_parent_phys, dst_parent_uuid, dst_lp = mgr.resolve_parent(
            dst_kind, dst_segments, destination, guard=True)

        src = os.path.join(src_parent_phys, name)
        dst = os.path.join(dst_parent_phys, name)
        entry_exists = os.path.isdir if is_dir else os.path.isfile
        if not entry_exists(src):
            raise FMFileNotFound(f"{label} not found: {src_lp}")
        if os.path.exists(dst):
            raise NameConflictError(
                f"{label} already exists: {dst_lp}/{name}")

        # 整棵子树写入白名单 (内部移动不触发外部变更同步)
        op_type = OpType.RENAME_DIR if is_dir else OpType.RENAME_FILE
        if is_dir:
            for dirpath, _dirnames, filenames in os.walk(src):
                mgr.whitelist.mark(dirpath, op_type)
                for fn in filenames:
                    mgr.whitelist.mark(os.path.join(dirpath, fn), op_type)
        else:
            mgr.whitelist.mark(src, op_type)
        mgr.whitelist.mark(dst, op_type)

        os.makedirs(dst_parent_phys, exist_ok=True)
        shutil.move(src, dst)  # 跨文件系统时自动复制后删除源

        with mgr.db_lock:
            new_segments = dst_segments + [name]
            if is_dir:
                mgr.db_helper.move_dir_records(
                    src_parent_uuid, src_segments + [name], new_segments,
                    dst_parent_uuid)
            else:
                mgr.db_helper.move_file_records(
                    src_parent_uuid, src_segments + [name],
                    new_segments, dst_parent_uuid)
        if is_dir:
            mgr.readonly.build()
        new_lp = join_logical_path("gallery", new_segments)
        log.info(f"{label} moved: {src_lp} -> {new_lp}")
        return new_lp

    # ------------------------------------------------------------------ #
    # 查询 / 读写
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 复制
    # ------------------------------------------------------------------ #

    def copy_entry(self, source_parent: str, name: str, target_parent: str,
                   is_dir: bool = False,
                   destination: Literal["mount", "local"] | None = None) -> str:
        """复制文件/文件夹到另一目录 (仅 /gallery), 返回新逻辑路径。"""
        mgr = self._mgr
        validate_name(name)
        src_kind, src_segments = split_logical_path(source_parent)
        dst_kind, dst_segments = split_logical_path(target_parent)
        if src_kind != "gallery" or dst_kind != "gallery":
            raise CrossRootError(
                f"Copy is only supported within /gallery: "
                f"{source_parent} -> {target_parent}")
        if is_dir and dst_segments[:len(src_segments) + 1] == src_segments + [name]:
            raise InvalidPathError(
                f"Cannot copy a folder into its own subtree: {target_parent}")

        label = "Folder" if is_dir else "File"
        src_lp = join_logical_path("gallery", src_segments + [name])
        src_parent_phys, src_parent_uuid, _ = mgr.resolve_parent(
            src_kind, src_segments, "mount")
        dst_parent_phys, dst_parent_uuid, dst_parent_lp = mgr.resolve_parent(
            dst_kind, dst_segments, destination, guard=True)

        src = os.path.join(src_parent_phys, name)
        dst = os.path.join(dst_parent_phys, name)
        entry_exists = os.path.isdir if is_dir else os.path.isfile
        if not entry_exists(src):
            raise FMFileNotFound(f"{label} not found: {src_lp}")
        if os.path.exists(dst):
            raise NameConflictError(
                f"{label} already exists: {dst_parent_lp}/{name}")

        new_segments = dst_segments + [name]
        new_lp = join_logical_path("gallery", new_segments)
        os.makedirs(dst_parent_phys, exist_ok=True)

        if is_dir:
            mgr.whitelist.mark(dst, OpType.CREATE_DIR)
            dst_uuid: str | None = None
            try:
                with mgr.db_lock:
                    src_row = mgr.db_helper.get_child(src_parent_uuid, name)
                if src_row is None:
                    raise PathNotFoundError(
                        f"Logical path not found: {src_lp}")
                os.makedirs(dst)
                with mgr.db_lock:
                    dst_uuid = mgr.db.add_path(
                        name, path_type="inbuilt", parent_uuid=dst_parent_uuid,
                        path_uuid=str(uuid_lib.uuid4()))
                self._copy_folder_children(
                    src_row["uuid"], src_segments + [name], dst_uuid,
                    new_segments, destination)
            except Exception:
                # 尽力回滚: 删除已创建的磁盘与数据库子树
                if dst_uuid is not None:
                    with mgr.db_lock:
                        try:
                            mgr.db_helper.remove_dir_records(dst_uuid)
                        except Exception:  # noqa: BLE001
                            pass
                try:
                    rmtree(dst)
                except OSError:
                    pass
                raise
            mgr.readonly.build()
        else:
            mgr.whitelist.mark(dst, OpType.CREATE_FILE)
            try:
                shutil.copy2(src, dst)
                self._copy_file_record(
                    src_parent_uuid, name, dst_parent_uuid, name,
                    dst_segments, destination)
            except Exception:
                try:
                    os.remove(dst)
                except OSError:
                    pass
                raise
            mgr.readonly.cache[new_lp] = mgr.readonly.cache.get(
                join_logical_path("gallery", dst_segments), False)
        log.info(f"{label} copied: {src_lp} -> {new_lp}")
        return new_lp

    def _copy_folder_children(self, src_uuid: str, src_segments: list[str],
                              dst_uuid: str, dst_segments: list[str],
                              destination: Literal["mount", "local"] | None) -> None:
        mgr = self._mgr
        with mgr.db_lock:
            files = mgr.db.list_files_by_path(src_uuid)
            dirs = mgr.db.list_paths(src_uuid)
        src_dir_phys = mgr.resolver.segments_to_physical(src_segments)
        dst_dir_phys = mgr.resolver.segments_to_physical(
            dst_segments, destination)

        for row in files:
            filename = row["filename"]
            src = os.path.join(src_dir_phys, filename)
            dst = os.path.join(dst_dir_phys, filename)
            mgr.whitelist.mark(dst, OpType.CREATE_FILE)
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                raise FileManagerError(
                    f"Failed to copy file: {src_segments}/{filename} ({e})") from e
            self._copy_file_record(
                src_uuid, filename, dst_uuid, filename,
                dst_segments, destination)

        for child in dirs:
            child_name = child["name"]
            child_src_segments = src_segments + [child_name]
            child_dst_segments = dst_segments + [child_name]
            child_phys = mgr.resolver.segments_to_physical(
                child_dst_segments, destination)
            mgr.whitelist.mark(child_phys, OpType.CREATE_DIR)
            os.makedirs(child_phys)
            with mgr.db_lock:
                child_uuid = mgr.db.add_path(
                    child_name, path_type="inbuilt", parent_uuid=dst_uuid,
                    path_uuid=str(uuid_lib.uuid4()))
            self._copy_folder_children(
                child["uuid"], child_src_segments, child_uuid,
                child_dst_segments, destination)

    def _copy_file_record(self, src_parent_uuid: str, src_name: str,
                          dst_parent_uuid: str, dst_name: str,
                          dst_segments: list[str],
                          destination: Literal["mount", "local"] | None) -> None:
        """复制文件记录与文件-模型关联; 无记录时按新增文件入库。"""
        mgr = self._mgr
        with mgr.db_lock:
            row = mgr.db_helper.get_file_row(src_parent_uuid, src_name)
            if row is not None:
                models = mgr.db.get_file_models(src_parent_uuid, src_name)
                mgr.db.add_file(
                    int(row["info_id"]), dst_parent_uuid, dst_name,
                    str(row.get("mime_type") or guess_mime(dst_name)),
                    width=int(row.get("width") or 0),
                    height=int(row.get("height") or 0),
                    file_size=int(row.get("file_size") or 0))
                try:
                    mgr.db.update_file_models(
                        dst_parent_uuid, dst_name,
                        checkpoints=models.get("checkpoint"),
                        loras=models.get("lora"),
                        embeddings=models.get("embedding"),
                        vaes=models.get("vae"),
                        upscales=models.get("upscale"))
                except Exception:
                    mgr.db.delete_file(dst_parent_uuid, dst_name)
                    raise
                return
        # 源文件尚无数据库记录: 物理复制完成后按新增文件异步入库
        physical = mgr.resolver.segments_to_physical(
            dst_segments + [dst_name], destination)
        mgr.sha.run(self.ingest_file_task, dst_parent_uuid, dst_name, physical)

    def entry_stat(self, parent_path: str,
                   name: str) -> dict[str, Any] | None:
        """返回条目物理信息 (size/created_time/edit_time/is_dir); 不存在返回 None。"""
        mgr = self._mgr
        validate_name(name)
        kind, segments = split_logical_path(parent_path)
        if kind == "cache":
            phys = os.path.join(mgr.resolve_parent(kind, segments)[0], name)
        else:
            phys = mgr.resolver.segments_to_physical(segments + [name])
        try:
            st = os.stat(phys)
        except OSError:
            return None
        return {"size": int(st.st_size), "created_time": int(st.st_ctime),
                "edit_time": int(st.st_mtime), "is_dir": os.path.isdir(phys)}

    def folder_stats(self, parent_path: str) -> dict[str, int]:
        """递归统计逻辑目录下的文件数与总字节数 (供缓存占用等展示使用)。"""
        kind, segments = split_logical_path(parent_path)
        base = self._mgr.resolve_parent(kind, segments)[0]
        count = 0
        total = 0
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                    count += 1
                except OSError:
                    continue
        return {"files": count, "bytes": total}

    def read_file(self, parent_path: str, filename: str) -> io.BufferedReader:
        """以二进制只读模式打开文件, 返回流对象。"""
        mgr = self._mgr
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        phys = os.path.join(mgr.resolve_parent(kind, segments)[0], filename)
        if not os.path.isfile(phys):
            raise FMFileNotFound(f"File not found: {lp}/{filename}")
        return open(phys, "rb")

    def file_exists(self, parent_path: str, filename: str) -> bool:
        """检查文件是否已存在 (/gallery 与 /cache 均可)。"""
        mgr = self._mgr
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        phys = os.path.join(mgr.resolve_parent(kind, segments)[0], filename)
        return os.path.isfile(phys)

    def write_file(self, parent_path: str, filename: str, data: bytes) -> None:
        """将二进制内容写入文件 (/gallery 与 /cache 均可)。"""
        mgr = self._mgr
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        parent_phys = mgr.resolve_parent(
            kind, segments, guard=(kind == "gallery"),
            must_exist=(kind == "cache"))[0]
        lp = join_logical_path(kind, segments)
        target = os.path.join(parent_phys, filename)
        tmp = os.path.join(parent_phys, f".{uuid_lib.uuid4()}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        log.info(f"File written: {lp}/{filename} ({len(data)} bytes)")

    def list_files(self, parent_path: str) -> list[dict[str, Any]]:
        """列出逻辑目录下的全部文件记录。"""
        mgr = self._mgr
        kind, segments = split_logical_path(parent_path)
        parent_phys, _, _ = mgr.resolve_parent(
            kind, segments, must_exist=(kind == "cache"))
        if kind == "cache":
            directory = join_logical_path("cache", segments)
            return [{"filename": name, "sha256": None, "directory": directory}
                    for name in sorted(os.listdir(parent_phys))
                    if os.path.isfile(os.path.join(parent_phys, name))]
        with mgr.db_lock:
            dir_uuid, _ = mgr.resolver.walk_segments(segments)
            rows = mgr.db.list_files_by_path(dir_uuid)
            # 经 info 关联补充 sha256 (walrus 避免重复查询)
            return [{**r,
                     "sha256": (info["sha256"]
                                if (info := mgr.db.get_info(int(r["info_id"])))
                                else None)}
                    for r in rows]

    # ------------------------------------------------------------------ #
    # SHA256 计算服务
    # ------------------------------------------------------------------ #

    def compute_sha256(
            self, parent_path: str, filename: str,
            callback: Callable[[str | None, str | None], None]) -> None:
        """异步计算文件 SHA256, 结果通过 callback(sha256_hex|None, error|None) 返回。"""
        mgr = self._mgr
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        try:
            parent_phys = mgr.resolve_parent(kind, segments)[0]
        except PathNotFoundError:
            callback(None, "file_not_found")
            return
        phys = os.path.join(parent_phys, filename)
        if not os.path.isfile(phys):
            callback(None, "file_not_found")
            return
        mgr.sha.submit(phys, callback)

    # ------------------------------------------------------------------ #
    # 元数据解析
    # ------------------------------------------------------------------ #

    def reparse_metadata(self, parent_path: str,
                         filename: str) -> dict[str, Any]:
        """重新读取文件元数据并写回数据库 (右键菜单)。"""
        mgr = self._mgr
        validate_name(filename)
        if mgr.parse_metadata is None:
            raise FileManagerError("Metadata parser is not available")
        kind, segments = split_logical_path(parent_path)
        if kind != "gallery":
            raise CrossRootError(
                f"Metadata can only be parsed under /gallery: {parent_path}")
        parent_phys, parent_uuid, _ = mgr.resolve_parent(kind, segments)
        physical = os.path.join(parent_phys, filename)
        if not os.path.isfile(physical):
            raise FMFileNotFound(f"File not found: {parent_path}/{filename}")
        with mgr.db_lock:
            row = mgr.db_helper.get_file_row(parent_uuid, filename)
        if row is None:
            raise FMFileNotFound(
                f"File record not found: {parent_path}/{filename}")
        meta = mgr.parse_metadata(physical)
        # 分辨率与文件大小和"能否解析生成参数"无关: 每次重新读取都独立探测
        width, height = probe_image_size(physical)
        try:
            file_size: int | None = os.path.getsize(physical)
        except OSError:
            file_size = None
        file_updates: dict[str, Any] = {}
        if width and height:
            file_updates["width"] = width
            file_updates["height"] = height
        if file_size is not None:
            file_updates["file_size"] = file_size
        if file_updates:
            with mgr.db_lock:
                mgr.db.update_file(parent_uuid, filename, **file_updates)
        if not meta:
            log.info(f"No metadata found (resolution refreshed): "
                     f"{parent_path}/{filename}")
            return {}
        mgr.db_helper.apply_metadata(parent_uuid, filename, int(row["info_id"]),
                                     meta, physical)
        log.info(f"Metadata reparsed: {parent_path}/{filename}")
        return meta

    def reparse_folder_metadata(self, folder_path: str,
                                recursive: bool = False) -> dict[str, int]:
        """重新读取文件夹内全部图片的元数据, 返回统计结果。"""
        mgr = self._mgr
        kind, segments = split_logical_path(folder_path)
        if kind != "gallery":
            raise CrossRootError(
                f"Metadata can only be parsed under /gallery: {folder_path}")
        if mgr.parse_metadata is None:
            raise FileManagerError("Metadata parser is not available")

        targets: list[tuple[str, str]] = []
        with mgr.db_lock:
            folder_uuid, _ = mgr.resolver.walk_segments(segments)

            def collect(dir_uuid: str, dir_segments: list[str]) -> None:
                for row in mgr.db.list_files_by_path(dir_uuid):
                    targets.append((join_logical_path("gallery", dir_segments),
                                    row["filename"]))
                if recursive:
                    for child in mgr.db.list_paths(dir_uuid):
                        collect(child["uuid"], dir_segments + [child["name"]])

            collect(folder_uuid, segments)

        updated = 0
        failed = 0
        for parent, filename in targets:
            try:
                self.reparse_metadata(parent, filename)
                updated += 1
            except Exception as e:  # noqa: BLE001 单个文件失败不影响其余文件
                failed += 1
                log.warning(f"Metadata reparse failed: {parent}/{filename} ({e})")
        log.info(f"Folder metadata reparsed: {folder_path} "
                 f"(recursive={recursive}, updated={updated}, failed={failed})")
        return {"files": len(targets), "updated": updated, "failed": failed}

    def reregister_subtree(self, physical_dir: str,
                           segments: list[str]) -> str:
        """物理目录子树重新入库: 目录逐级确保记录, 文件异步哈希入库。"""
        mgr = self._mgr
        base_uuid = mgr.db_helper.ensure_dir_records(segments)
        for dirpath, _dirnames, filenames in os.walk(physical_dir):
            rel = os.path.relpath(dirpath, physical_dir)
            sub_segments = segments if rel == "." else \
                segments + rel.replace(os.sep, "/").split("/")
            dir_uuid = mgr.db_helper.ensure_dir_records(sub_segments)
            for fn in filenames:
                if should_ignore(fn):
                    continue
                mgr.sha.run(self.ingest_file_task, dir_uuid, fn,
                            os.path.join(dirpath, fn))
        return base_uuid

    # ---- 新文件入库流程 ----

    def ingest_file_task(self, parent_uuid: str, name: str,
                         physical: str) -> None:
        """新文件入库: SHA256 → 空元数据 info → file 记录 → 元数据解析。"""
        mgr = self._mgr
        if not wait_file_ready(physical):
            log.warning(f"Failed to ingest new file (empty or unstable): "
                        f"{physical}")
            return
        digest: str | None = None
        file_size = 0
        for _attempt in range(INGEST_MAX_RETRY):
            try:
                size_before = os.path.getsize(physical)
            except OSError:
                log.warning(f"Failed to ingest new file (disappeared): {physical}")
                return
            digest, err = mgr.sha.compute(physical)
            if digest is None or digest == EMPTY_SHA256:
                log.warning(f"Failed to ingest new file (hash error): "
                            f"{physical} ({err})")
                return
            try:
                file_size = os.path.getsize(physical)
            except OSError:
                log.warning(f"Failed to ingest new file (disappeared): {physical}")
                return
            if file_size == size_before:
                break  # 哈希期间大小未变化, 视为稳定
            digest = None
        if digest is None:
            log.warning(f"Failed to ingest new file (file keeps changing): "
                        f"{physical}")
            return
        try:
            with mgr.db_lock:
                existing = mgr.db_helper.get_file_row(parent_uuid, name)
                if existing is not None:
                    if int(existing.get("file_size") or 0) == file_size:
                        return  # 已有记录且内容大小一致, 避免重复入库
                    # 大小已变化: 旧记录是过期内容, 重新入库
                    mgr.db.delete_file(parent_uuid, name)
                # 与元数据解析解耦: 无论能否解析生成参数, 分辨率都独立探测入库
                width, height = probe_image_size(physical)
                info_id = mgr.db_helper.get_or_create_info(digest)
                mgr.db.add_file(info_id, parent_uuid, name, guess_mime(name),
                                width=width, height=height,
                                file_size=file_size)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to ingest new file: {physical} ({e})")
            return
        # 解析器调用放在独立任务中, 不占用本任务剩余时间
        mgr.sha.run(self.parse_metadata_task, parent_uuid, name,
                    info_id, physical)

    def parse_metadata_task(self, path_uuid: str, filename: str,
                            info_id: int, physical: str) -> None:
        """后台解析任务: 解析成功则写回 info/file/模型表。"""
        mgr = self._mgr
        if mgr.parse_metadata is None:
            log.warning(f"Metadata parser unavailable, skipped: {physical}")
            return
        try:
            meta = mgr.parse_metadata(physical)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to parse metadata: {physical} ({e})")
            return
        if not meta:
            return
        mgr.db_helper.apply_metadata(path_uuid, filename, info_id, meta, physical)
