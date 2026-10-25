"""上传流程: 临时文件、分块合并、移动入库与失败清理。"""
from __future__ import annotations

import io
import os
import threading
import uuid as uuid_lib
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Callable, Literal

from exceptions import (
    CrossRootError,
    FileManagerError,
    InputDataError,
    InvalidPathError,
    SHA256Error,
)
from scripts.file_manager.disk_utils import copy_verified, unique_name
from scripts.file_manager.path_utils import guess_mime, split_logical_path
from scripts.sha256 import normalize_sha256
from scripts.file_manager.whitelist import OpType
from scripts.image_probe import probe_image_size
from scripts.logger import log

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager


class UploadService:
    """上传临时文件与最终落库流程 (独立上传线程池 x2)。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="upload")
        self._map: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def discard_upload(self, source_filename: str) -> None:
        """丢弃一个上传临时文件 (分块合并后清理或失败时清理半成品)。"""
        if (not source_filename or "/" in source_filename
                or "\\" in source_filename or ".." in source_filename):
            raise InvalidPathError(
                f"Invalid upload temp file name: {source_filename!r}")
        with self._lock:
            self._map.pop(source_filename, None)
        try:
            os.remove(os.path.join(self._mgr.upload_folder, source_filename))
        except OSError:
            pass

    def open_upload_file(self, original_filename: str,
                         target_parent: str) -> tuple[str, io.BufferedWriter]:
        """开始一次上传: 生成临时文件并返回可写流。"""
        if not original_filename or "/" in original_filename \
                or "\\" in original_filename:
            raise InvalidPathError(
                f"Invalid upload filename: {original_filename!r}")
        kind, _ = split_logical_path(target_parent)
        if kind != "gallery":
            log.error("Cross-root operation rejected: upload target must be "
                      f"under /gallery: {target_parent}")
            raise CrossRootError(
                f"Upload target must be under /gallery: {target_parent}")

        os.makedirs(self._mgr.upload_folder, exist_ok=True)
        temp_name = str(uuid_lib.uuid4())
        with self._lock:
            self._map[temp_name] = {
                "original_name": original_filename,
                "target_parent": target_parent,
            }
        stream = open(os.path.join(self._mgr.upload_folder, temp_name), "wb")
        return temp_name, stream

    def upload_move(self, source_filename: str, target_parent: str,
                    sha256: str,
                    destination: Literal["mount", "local"] | None = None,
                    callback: Callable[[str | None, str | None], None] | None = None
                    ) -> None:
        """将上传临时文件移动到目标位置并入库。"""
        mgr = self._mgr
        expected = normalize_sha256(sha256)
        if expected is None:
            raise InputDataError(f"Invalid SHA256 format: {sha256!r}")
        kind, segments = split_logical_path(target_parent)
        if kind != "gallery":
            log.error("Cross-root operation rejected: upload target must be "
                      f"under /gallery: {target_parent}")
            raise CrossRootError(
                f"Upload target must be under /gallery: {target_parent}")
        mgr.readonly.check(segments, destination)  # 可能抛 ReadOnlyError/UserChoiceRequired

        # 进入移动阶段前, 在调用线程校验上传内容的哈希
        temp_path = os.path.join(mgr.upload_folder, source_filename)
        digest, hash_err = mgr.sha.compute(temp_path)
        if digest is None:
            raise SHA256Error(
                f"Failed to compute SHA256 of uploaded file: {hash_err}")
        if digest != expected:
            raise SHA256Error(
                f"Uploaded file SHA256 mismatch: expected {expected}, got {digest}")

        self._executor.submit(
            self._upload_task, source_filename, target_parent, expected,
            destination, callback)

    def _upload_task(self, source_filename: str, target_parent: str,
                     expected_sha: str,
                     destination: Literal["mount", "local"] | None,
                     callback: Callable[[str | None, str | None], None] | None) -> None:
        mgr = self._mgr
        try:
            with self._lock:
                entry = self._map.get(source_filename)
            if entry is None:
                raise FileManagerError(f"Upload record not found: {source_filename}")

            _, segments = split_logical_path(target_parent)
            phys_dir, parent_uuid, parent_lp = mgr.resolve_parent(
                "gallery", segments, destination)
            os.makedirs(phys_dir, exist_ok=True)

            final_name = unique_name(phys_dir, entry["original_name"])
            src = os.path.join(mgr.upload_folder, source_filename)
            dst = os.path.join(phys_dir, final_name)

            mgr.whitelist.mark(dst, OpType.CREATE_FILE)
            try:
                os.replace(src, dst)  # 同文件系统: 原子移动
                digest = expected_sha
            except OSError:
                # 跨文件系统: 校验复制 (复制后二次比对哈希)
                digest = copy_verified(src, dst, expected_sha, mgr.sha)
                os.remove(src)

            width, height = probe_image_size(dst)
            with mgr.db_lock:
                info_id = mgr.db_helper.get_or_create_info(digest)
                mgr.db.add_file(info_id, parent_uuid, final_name,
                                guess_mime(final_name),
                                width=width, height=height,
                                file_size=os.path.getsize(dst))

            # 上传走白名单路径, 不产生可入库的监控事件;
            # 与 ingest_file_task 一致, 入库后异步触发元数据解析
            mgr.sha.run(mgr.operations.parse_metadata_task, parent_uuid,
                        final_name, info_id, dst)

            with self._lock:
                self._map.pop(source_filename, None)
            new_lp = parent_lp + "/" + final_name
            log.info(f"Upload completed: {source_filename} -> {new_lp}")
            if callback is not None:
                callback(new_lp, None)
        except Exception as e:  # noqa: BLE001 失败时保留临时文件与映射表
            log.warning(f"Upload failed: {source_filename} ({e})")
            if callback is not None:
                callback(None, str(e))

    def shutdown(self) -> None:
        """关闭上传线程池并清理 /cache 内的上传临时文件与内存映射表。"""
        self._executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            temp_names = list(self._map)
            self._map.clear()
        for name in temp_names:
            try:
                os.remove(os.path.join(self._mgr.upload_folder, name))
            except OSError:
                pass


__all__ = ["UploadService"]
