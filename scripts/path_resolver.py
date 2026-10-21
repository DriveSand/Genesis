"""path_resolver.py — 路径解析模块: 整个程序路径转换的唯一入口。

本模块只负责 /gallery 目录的转换 ("/gallery" 与 "/cache" 的根前缀分发
由文件管理模块处理, 本模块不感知 /cache 的存在) 。

职责:
- /gallery 逻辑路径 <-> 物理路径的双向转换 (逻辑目录结构来自 DataBase 路径表,
  物理落点由挂载配置决定: 命中最深挂载点用挂载物理路径, 否则落 GALLERY_ROOT)
- 路径 UUID -> 完整逻辑路径 / 物理路径
- SHA256 -> 该内容现有副本的逻辑路径+文件名列表
- 物理路径 -> 完整逻辑路径 / 挂载点 key (反向查询, 供文件管理模块使用)

逻辑路径约定:
- 本模块对外的"完整逻辑路径"**不带 /gallery 前缀**, 但恒以 "/" 开头:
  根目录为 "/", 其余形如 "/abc/123" (与 DataBase resolve_path 的返回一致)
- 查询方法的输入采用同一规范形式 (必须以 "/" 开头)
- 段列表形式的内部方法 (walk_segments / segments_to_physical / mount_key_of)
  供文件管理模块复用, 段中不允许出现 "." / ".."

线程模型: walk_segments / sha_to_files / uuid_to_logical 内部自取共享 db_lock
 (由构造方传入与文件管理模块相同的 RLock, 保证多步操作原子性; None 时自建仅供
独立测试) 。挂载配置由本类唯一持有, update_mount_configs 原子整表替换 (发布后的
字典不再就地修改, 读侧无需加锁即拿到一致快照) 。
本模块除只读访问 DataBase 外不做任何磁盘 IO、不执行任何 SQL。
"""
import os
import threading
from dataclasses import replace
from typing import Any, Iterator, Literal

from exceptions import InvalidPathError, PathNotFoundError
from scripts.logger import log
from scripts.sha256 import normalize_sha256

GALLERY = "/gallery"


# ---------------------------------------------------------------------- #
# 模块级纯函数
# ---------------------------------------------------------------------- #

def norm_physical(path: str) -> str:
    """物理路径规范化: 统一分隔符, Windows 下忽略大小写。"""
    return os.path.normcase(os.path.normpath(path))


def validate_name(name: Any) -> None:
    """校验单段文件/文件夹名称, 非法时抛 InvalidPathError。"""
    if (not isinstance(name, str) or not name or name in (".", "..")
            or "/" in name or "\\" in name or ":" in name):
        raise InvalidPathError(f"Invalid file/folder name: {name!r}")  # 非法名称


def match_root(physical: str,
               roots: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    """规范化物理根的最长前缀匹配。

    :param roots: {规范化物理根: (逻辑根, 原始物理根)}
    :return: (逻辑根, 原始物理根) 或 None。
    前缀边界按 os.sep 判定, 防止 D:\\a 误匹配 D:\\ab。
    """
    norm = norm_physical(physical)
    best: tuple[int, str, str] | None = None
    for root_norm, info in roots.items():
        if norm == root_norm or norm.startswith(root_norm + os.sep):
            if best is None or len(root_norm) > best[0]:
                best = (len(root_norm), info[0], info[1])
    if best is None:
        return None
    return best[1], best[2]


def _split_segments(path: str) -> list[str]:
    """按 "/" 切分为段列表并丢弃空段 ("/" -> [], "a//b/" -> ["a", "b"]) 。"""
    return [s for s in path.split("/") if s]


def _parse_logical(logical_path: str) -> list[str]:
    """解析 /gallery 内部逻辑路径 -> 段列表。

    必须以 "/" 开头 (根为 "/") ; 拒绝 "." / ".." 段 (路径穿越防护) 。
    """
    if not isinstance(logical_path, str) or not logical_path.startswith("/"):
        raise InvalidPathError(  # 非法的逻辑路径: 必须以 '/' 开头
            f"Invalid logical path (must start with '/'): {logical_path!r}")
    segments = _split_segments(logical_path)
    if any(seg in (".", "..") for seg in segments):
        raise InvalidPathError(  # 非法的逻辑路径: 不得包含相对路径段
            f"Invalid logical path (must not contain '.' or '..'): "
            f"{logical_path!r}")
    return segments


# ---------------------------------------------------------------------- #
# 路径解析器
# ---------------------------------------------------------------------- #

class PathResolver:
    """/gallery 路径解析器: 逻辑 <-> 物理 双向转换的唯一入口。"""

    def __init__(self, db, mount_configs: dict, gallery_root: str,
                 db_lock: "threading.RLock | None" = None):
        """
        :param db: DataBase 实例 (只读查询)
        :param mount_configs: {挂载逻辑路径(不含 /gallery 前缀, 如 "/abc"): MountConfig}
        :param gallery_root: GALLERY_ROOT 物理路径
        :param db_lock: 与文件管理模块共享的 RLock; 生产环境必须传入同一把锁,
            否则多步查询与写操作之间可能出现撕裂读。None 时自建仅供独立测试。
        """
        self._db = db
        self._db_lock = db_lock if db_lock is not None else threading.RLock()
        self._gallery_root = os.path.abspath(gallery_root)
        # 挂载配置唯一持有者: {规范化挂载key: MountConfig(物理路径已 abspath)}
        self._mount_configs: dict[str, Any] = {}
        # 反向查询索引: {规范化物理根: (逻辑根, 原始物理根)}, 含 gallery 根与全部挂载
        self._config_roots: dict[str, tuple[str, str]] = {}
        self.update_mount_configs(mount_configs)

    # ---------------------------------------------------------------- #
    # 挂载配置
    # ---------------------------------------------------------------- #

    def update_mount_configs(self, mount_configs: dict) -> None:
        """整体替换挂载配置并重建反向索引 (引用级原子替换, 读侧无锁安全) 。"""
        mounts: dict[str, Any] = {}
        roots: dict[str, tuple[str, str]] = {
            norm_physical(self._gallery_root): (GALLERY, self._gallery_root),
        }
        for key, mc in (mount_configs or {}).items():
            if not isinstance(key, str):
                continue
            # 挂载 key 统一为规范形式 (无空段/尾随斜杠), 保证 key.count("/")
            # 即挂载深度, 且与 mount_key_of 生成的 key 完全一致
            norm_key = "/" + "/".join(_split_segments(key))
            if norm_key == "/":
                continue
            physical = os.path.abspath(mc.physical_path)
            mc = replace(mc, physical_path=physical)
            mounts[norm_key] = mc
            roots[norm_physical(physical)] = (GALLERY + norm_key, physical)
        self._mount_configs = mounts
        self._config_roots = roots

    def iter_mounts(self) -> Iterator[tuple[str, Any]]:
        """按配置序遍历 (挂载key, MountConfig), 物理路径已规范化为绝对路径。

        配置更新为整表引用替换, 已发布的字典不会被就地修改,
        因此 items 视图即稳定快照, 可在遍历期间安全替换配置。
        """
        return iter(self._mount_configs.items())

    def mount_config(self, mount_key: str) -> Any | None:
        """按挂载 key 取配置 (key 为不含 /gallery 前缀的挂载逻辑路径) 。"""
        return self._mount_configs.get(mount_key)

    # ---------------------------------------------------------------- #
    # 对外查询 API
    # ---------------------------------------------------------------- #

    def sha_to_files(self, sha256: str) -> dict[str, list[dict[str, str]]]:
        """SHA256 -> 该内容现有副本的逻辑路径+文件名列表。

        :return: {"content": [{"logical_path": "/abc/123", "file_name": "a.png"}, ...]}
        logical_path 恒以 "/" 开头 (根级文件为 "/") 。
        内容不存在、SHA256 格式非法或无任何文件记录时返回 {"content": []}。
        """
        key = normalize_sha256(sha256)  # SHA256 校验统一复用 sha256 模块
        if key is None:
            log.warning(f"Invalid SHA256 format, return empty result: {sha256!r}")
            return {"content": []}
        with self._db_lock:
            info = self._db.get_info_by_sha256(key)
            if info is None:
                log.debug(f"No info record for SHA256: {key}")
                return {"content": []}
            rows = self._db.get_files_by_info(int(info["id"]))
        # 孤儿记录 (路径节点已被删除, 无法定位) 只告警并跳过
        for r in rows:
            if r.get("directory") is None:
                log.warning(
                    "File record references a missing path node, skipped: "
                    f"info_id={r.get('info_id')} filename={r.get('filename')!r}")
        return {"content": [
            {"logical_path": r["directory"], "file_name": r["filename"]}
            for r in rows if r.get("directory") is not None
        ]}

    def uuid_to_logical(self, path_uuid: str) -> str:
        """路径 UUID -> 完整逻辑路径 (恒以 "/" 开头; 根目录返回 "/") 。"""
        with self._db_lock:
            rel = self._db.resolve_path(path_uuid)
        if rel is None:
            raise PathNotFoundError(f"Path node not found: {path_uuid}")  # 路径节点不存在
        return rel

    def uuid_to_physical(self, path_uuid: str) -> str:
        """路径 UUID -> 物理绝对路径 (挂载感知; 仅目录节点) 。"""
        return self.segments_to_physical(
            _split_segments(self.uuid_to_logical(path_uuid)))

    def logical_to_physical(self, logical_path: str) -> str:
        """完整逻辑路径 -> 物理绝对路径。

        输入采用本模块规范形式 (以 "/" 开头, "/" 为根, 不带 /gallery 前缀) ;
        路径表中任一级不存在时抛 PathNotFoundError。
        """
        segments = _parse_logical(logical_path)
        self.walk_segments(segments)  # 校验存在性
        return self.segments_to_physical(segments)

    def logical_file_to_physical(self, logical_path: str, filename: str) -> str:
        """完整逻辑路径 + 文件名 -> 物理路径 + 文件名。"""
        validate_name(filename)
        return os.path.join(self.logical_to_physical(logical_path), filename)

    def physical_to_logical(self, physical: str) -> str | None:
        """物理路径 -> 完整逻辑路径 (恒以 "/" 开头; gallery 根返回 "/") 。

        基于配置全集 (gallery 根 + 全部挂载, 含离线挂载) 做最长前缀匹配;
        不属于任何根时返回 None。
        """
        hit = match_root(physical, self._config_roots)
        if hit is None:
            return None
        logical_root, original_root = hit
        rel = os.path.relpath(physical, original_root)
        parts = [] if rel == "." else _split_segments(rel.replace(os.sep, "/"))
        return "/" + "/".join(
            _split_segments(logical_root[len(GALLERY):]) + parts)

    def physical_to_mount_key(self, physical: str) -> str | None:
        """物理路径 -> 所属挂载点 key (不含 /gallery 前缀, 最长前缀优先) 。

        与 physical_to_logical 共用 match_root 最长前缀匹配 (基于挂载配置全集,
        含离线挂载) ; 不属于任何挂载点时返回 None。
        """
        hit = match_root(physical, self._config_roots)
        if hit is None:
            return None
        # 命中 gallery 根本身说明不属于任何挂载点; 命中挂载则去掉 /gallery 前缀
        return hit[0][len(GALLERY):] or None

    # ---------------------------------------------------------------- #
    # 供文件管理模块复用的内部方法
    # ---------------------------------------------------------------- #

    def walk_segments(self, segments: list[str]) -> tuple[str, str]:
        """沿路径表逐级解析 /gallery 段列表 -> (节点 uuid, 逻辑路径)。

        返回的逻辑路径带 /gallery 前缀 (供文件管理模块内部使用) 。
        任一级不存在时抛 PathNotFoundError。
        """
        with self._db_lock:
            root = self._db.get_root_path()
            if root is None:
                raise PathNotFoundError("Path table is missing the root node")  # 路径表缺少根节点
            current = root["uuid"]
            lp = GALLERY
            for seg in segments:
                child = next(
                    (c for c in self._db.list_paths(current) if c["name"] == seg),
                    None)
                if child is None:
                    raise PathNotFoundError(  # 逻辑路径不存在
                        f"Logical path not found: {lp + '/' + seg}")
                lp += "/" + seg
                current = child["uuid"]
            return current, lp

    def mount_key_of(self, segments: list[str]) -> str | None:
        """返回段列表命中的最深挂载点 key (不含 /gallery 前缀) , 无则 None。"""
        for i in range(len(segments), 0, -1):
            key = "/" + "/".join(segments[:i])
            if key in self._mount_configs:
                return key
        return None

    def segments_to_physical(
            self, segments: list[str],
            destination: Literal["mount", "local"] | None = None) -> str:
        """/gallery 段列表 -> 物理绝对路径。

        destination="local" 时忽略挂载映射, 落到 GALLERY_ROOT 本地影子目录
         (仅供文件管理模块的写入目标选择使用) 。
        """
        mount_key = None if destination == "local" else self.mount_key_of(segments)
        if mount_key is None:
            return os.path.join(self._gallery_root, *segments)
        # 挂载 key 已是规范形式 (见 update_mount_configs), count("/") 即挂载深度
        return os.path.join(self._mount_configs[mount_key].physical_path,
                            *segments[mount_key.count("/"):])
