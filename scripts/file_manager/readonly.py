"""只读保护: 程序级只读 + 挂载点目录级只读 (向下继承)。"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from exceptions import ReadOnlyError, UserChoiceRequired
from scripts.file_manager.path_utils import join_logical_path
from scripts.logger import log
from scripts.path_resolver import GALLERY

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager


class ReadonlyGuard:
    """维护 {完整逻辑路径: 是否只读} 缓存并提供写前检查。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager
        self.cache: dict[str, bool] = {GALLERY: False}

    def build(self) -> None:
        """遍历路径表预计算 {完整逻辑路径: 是否只读}。"""
        mgr = self._mgr
        cache: dict[str, bool] = {GALLERY: False}
        with mgr.db_lock:
            root = mgr.db.get_root_path()
            if root is None:
                self.cache = cache
                return
            stack: list[tuple[str, str]] = [(root["uuid"], GALLERY)]
            while stack:
                parent_uuid, parent_lp = stack.pop()
                for child in mgr.db.list_paths(parent_uuid):
                    lp = parent_lp + "/" + child["name"]
                    rel_key = lp[len(GALLERY):] or "/"
                    mc = mgr.resolver.mount_config(rel_key)
                    cache[lp] = (bool(mc.readonly) if mc is not None
                                 else cache[parent_lp])
                    stack.append((child["uuid"], lp))
        self.cache = cache

    def check(self, segments: list[str],
              destination: Literal["mount", "local"] | None) -> None:
        """/gallery 写操作前的只读与挂载选择检查。"""
        mgr = self._mgr
        lp = join_logical_path("gallery", segments)
        if mgr.read_only:
            log.warning(f"Write rejected (program-level read-only): {lp}")
            raise ReadOnlyError(
                f"Program-level read-only mode is enabled, write rejected: {lp}")
        if self.cache.get(lp, False):
            log.warning(f"Write rejected (directory is read-only): {lp}")
            raise ReadOnlyError(f"Directory is read-only, write rejected: {lp}")
        mount_key = mgr.resolver.mount_key_of(segments)
        if mount_key is not None and destination is None:
            raise UserChoiceRequired(
                f"Write target is inside a read-write mount, "
                f"choose a destination: {lp}",
                options=["mount", "local"],
                logical_path=lp)
