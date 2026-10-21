"""file_manager.py — 程序文件操作的唯一入口。

职责 (严格限定, 不做其他事) :
1. 文件系统监控 (watchdog) : 递归监控 /gallery 下内置目录与挂载目录,
   文件/文件夹的新增、重命名、删除同步到 DataBase; /cache 不监控。
2. 对外文件操作服务: 创建、重命名、删除、读取。操作 /gallery 通知
   DataBase; 操作 /cache 不通知。
3. 只读保护: 程序级 (构造参数 read_only) + 目录级 (挂载配置 readonly,
   向下继承) 。只读拒绝一切写操作; 仅影响 /gallery, /cache 不受限制。
4. SHA256 计算服务: 线程池异步计算, 回调返回结果 (回调在工作线程执行) ;
   计算逻辑委托 scripts/sha256.py。

路径体系:
- 对外接口路径参数均为逻辑路径, 必须以 "/gallery" 或 "/cache" 开头。
  根前缀分发在本模块: 收到 /cache 请求直接访问缓存目录 (两个根都拒绝
  "." / ".." 段与非法文件名, 防止路径穿越) ; 收到 /gallery 请求则去除
  前缀后提交 scripts/path_resolver.py (整个程序路径转换的唯一入口) 。
- /gallery 的物理路径 = 挂载点物理路径 / GALLERY_ROOT + 相对段,
  目录结构通过 DataBase 路径表查询解析; /cache 直接映射 CACHE_FOLDER。

线程模型: 同步 API + sha256 服务线程池 x4 (哈希回调与入库/元数据解析
后台任务共用) + 上传线程 x2 + watchdog Observer 线程 + threading.Timer。
所有 DataBase 调用经内部锁串行化, 本模块不执行任何 SQL。
"""

import io
import mimetypes
import os
import shutil
import threading
import time
import uuid as uuid_lib
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Callable, Literal

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from exceptions import (
    CrossRootError,
    DirectoryNotEmptyError,
    FileManagerError,
    InputDataError,
    InvalidPathError,
    NameConflictError,
    PathNotFoundError,
    ReadOnlyError,
    SHA256Error,
    UserChoiceRequired,
)
from exceptions import FileNotFoundError as FMFileNotFound
from scripts.image_probe import probe_image_size
from scripts.logger import log
from scripts.path_resolver import (
    GALLERY,
    PathResolver,
    match_root,
    norm_physical as _norm_physical,
    validate_name,
)
from scripts.sha256 import (
    CHUNK_SIZE,
    EMPTY_SHA256,
    Sha256Service,
    normalize_sha256,
)

# ---------------------------------------------------------------------- #
# 常量
# ---------------------------------------------------------------------- #

_CACHE = "/cache"

_UPLOAD_MAX_TRIALS = 100               # (n) 后缀最多尝试次数
_COPY_MAX_RETRY = 3                    # 跨文件系统复制后哈希校验失败的最大重复制次数

_WHITELIST_MATCH_WINDOW = 10.0         # 白名单事件匹配窗口 (秒)
_WHITELIST_EXPIRE = 30.0               # 白名单条目过期时间 (秒)
_OFFLINE_CONFIRM_DELAY = 5.0           # 离线确认期 (秒)
_HEALTH_CHECK_INTERVAL = 30.0          # 挂载点健康检查间隔 (秒)
_RECOVERY_POLL_INTERVAL = 30.0         # 离线恢复轮询间隔 (秒)

# watchdog 忽略的临时/中间文件
_IGNORED_EXTS = {".tmp", ".temp", ".bak", ".swp", ".swo",
                 ".partial", ".crdownload", ".download"}
_IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}

# 元数据解析器可更新的 info 字段 (尺寸/大小属于 file 表)
_META_UPDATABLE_FIELDS = {
    "positive_prompt", "negative_prompt", "seed", "steps", "cfg",
    "sampler", "scheduler", "tool", "rating", "note",
}

# 元数据解析器可更新的 file 字段 (单个文件的尺寸/大小)
_META_FILE_FIELDS = {"width", "height", "file_size"}

# 入库前等待文件写入完成: 轮询间隔 / 最长等待 / 空文件判定 / 哈希复核次数
_READY_POLL_INTERVAL = 0.1
_READY_TIMEOUT = 10.0
_READY_EMPTY_TIMEOUT = 2.0
_INGEST_MAX_RETRY = 3


class _OpType(str, Enum):
    """去重白名单操作类型。"""
    CREATE_FILE = "CREATE_FILE"
    CREATE_DIR = "CREATE_DIR"
    RENAME_FILE = "RENAME_FILE"
    RENAME_DIR = "RENAME_DIR"
    DELETE_FILE = "DELETE_FILE"
    DELETE_DIR = "DELETE_DIR"


def _norm_physical(path: str) -> str:
    """物理路径规范化: 统一分隔符, Windows 下忽略大小写。"""
    return os.path.normcase(os.path.normpath(path))


def _guess_mime(filename: str) -> str:
    """用标准库 mimetypes 推断 MIME; 未知类型回退为二进制流。"""
    mime, _encoding = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _should_ignore(path: str) -> bool:
    """watchdog 忽略文件判定。"""
    base = os.path.basename(path)
    if base.startswith("~$"):
        return True
    if base in _IGNORED_NAMES:
        return True
    return os.path.splitext(base)[1].lower() in _IGNORED_EXTS


def _split_name_ext(filename: str) -> tuple[str, str]:
    """拆分文件名与完整扩展名: photo.tar.gz -> ("photo", ".tar.gz")。"""
    dot = filename.find(".", 1)  # 跳过开头的点 (隐藏文件无扩展名)
    if dot == -1:
        return filename, ""
    return filename[:dot], filename[dot:]


def _is_accessible(physical: str) -> bool:
    """检测目录是否可访问 (挂载点健康检查)。"""
    try:
        if not os.path.isdir(physical):
            return False
        os.listdir(physical)
        return True
    except (OSError, PermissionError):
        return False


# ---------------------------------------------------------------------- #
# 逻辑路径根分发（/gallery 与 /cache 的前缀处理归本模块；
# /gallery 前缀去除后的转换提交给 path_resolver）
# ---------------------------------------------------------------------- #

def split_logical_path(logical_path: str) -> tuple[str, list[str]]:
    """拆分逻辑路径 -> ("gallery"|"cache", 段列表)。非法时抛 InvalidPathError。

    两个根都拒绝 "." / ".." 段, 防止路径穿越 (如 /cache/../x) 。
    """
    if not isinstance(logical_path, str) or not logical_path.startswith("/"):
        raise InvalidPathError(
            f"Invalid logical path (must start with /gallery or /cache): "
            f"{logical_path!r}")  # 非法路径: 必须以 /gallery 或 /cache 开头
    lp = logical_path.rstrip("/") or "/"
    for prefix, kind in ((GALLERY, "gallery"), (_CACHE, "cache")):
        if lp == prefix or lp.startswith(prefix + "/"):
            rest = lp[len(prefix):]
            segments = [s for s in rest.split("/") if s]
            if any(s in (".", "..") for s in segments):
                raise InvalidPathError(
                    f"Invalid logical path (must not contain '.' or '..'): "
                    f"{logical_path!r}")  # 不得包含相对路径段
            return kind, segments
    raise InvalidPathError(
        f"Invalid logical path (must start with /gallery or /cache): "
        f"{logical_path!r}")  # 前缀不匹配任何受支持根


def join_logical_path(kind: str, segments: list[str]) -> str:
    """段列表拼回完整逻辑路径 (含 /gallery 或 /cache 前缀)。"""
    return f"/{kind}" + ("/" + "/".join(segments) if segments else "")


# ---------------------------------------------------------------------- #
# 去重白名单
# ---------------------------------------------------------------------- #

class _DedupeWhitelist:
    """本模块主动操作的物理路径白名单, 用于过滤自身触发的 watchdog 事件。

    结构: dict[规范化物理路径, (操作类型, 时间戳)], 线程安全。
    """

    def __init__(self) -> None:
        self._entries: dict[str, tuple[_OpType, float]] = {}
        self._lock = threading.Lock()

    def mark(self, physical_path: str, op: _OpType) -> None:
        key = _norm_physical(physical_path)
        now = time.time()
        with self._lock:
            # 惰性清理: 写入新条目时顺带清除超过 30 秒的过期条目
            stale = [k for k, (_, ts) in self._entries.items()
                     if now - ts > _WHITELIST_EXPIRE]
            for k in stale:
                del self._entries[k]
            self._entries[key] = (op, now)

    def check(self, physical_path: str, op: _OpType) -> bool:
        """路径在白名单中、操作类型匹配且时间差 < 10 秒时返回 True。"""
        key = _norm_physical(physical_path)
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                return False
            return item[0] == op and (time.time() - item[1]) < _WHITELIST_MATCH_WINDOW

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# ---------------------------------------------------------------------- #
# watchdog 事件处理器
# ---------------------------------------------------------------------- #

class _WatchHandler(FileSystemEventHandler):
    def __init__(self, manager: "FileManager") -> None:
        super().__init__()
        self._mgr = manager

    def on_created(self, event) -> None:
        self._mgr._dispatch_event("created", event)

    def on_deleted(self, event) -> None:
        self._mgr._dispatch_event("deleted", event)

    def on_moved(self, event) -> None:
        self._mgr._dispatch_event("moved", event)


# ---------------------------------------------------------------------- #
# FileManager
# ---------------------------------------------------------------------- #

class FileManager:
    """文件系统操作唯一入口。详见模块 docstring。"""

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
        self._gallery_root = os.path.abspath(gallery_root)
        self._cache_root = os.path.abspath(cache_root)
        # upload_folder 传名称时挂到 /cache 下, 传绝对路径则直接使用
        self._upload_folder = os.path.join(self._cache_root, upload_folder)
        self._read_only = bool(read_only)
        self._shutting_down = False

        # 线程池: 上传 x2; 哈希回调与入库/解析后台任务共用 sha256 服务池
        self._upload_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="upload")

        # 去重白名单与上传映射表
        self._whitelist = _DedupeWhitelist()
        self._upload_map: dict[str, dict[str, str]] = {}
        self._upload_lock = threading.Lock()

        # 线程安全
        self._db_lock = threading.RLock()      # 串行化所有 DataBase 调用
        self._state_lock = threading.Lock()    # 挂载点状态
        self._timers: set[threading.Timer] = set()
        self._timers_lock = threading.Lock()

        # 路径解析 (挂载配置由 resolver 唯一持有) 与 SHA256 计算服务
        self._resolver = PathResolver(
            db, mount_configs, self._gallery_root, self._db_lock)
        self._sha = Sha256Service(4)

        # 只读映射缓存 (启动时预计算, 运行时 O(1) 查表)
        self._readonly_cache: dict[str, bool] = {}
        self._build_readonly_cache()

        # 元数据解析器 (预留接口, 尚未编写时容错跳过)
        try:
            from scripts.parser_scheduler import parse_metadata  # type: ignore
            self._parse_metadata = parse_metadata
        except ImportError:
            self._parse_metadata = None

        # watchdog: 每个物理根目录注册一个 schedule
        self._watch_handler = _WatchHandler(self)
        self._observer = Observer()
        self._observer.daemon = True
        # 规范化物理根 -> (逻辑根, 原始物理根)
        self._watch_roots: dict[str, tuple[str, str]] = {}
        self._mount_state: dict[str, dict[str, Any]] = {}

        self._start_watch(self._gallery_root, GALLERY)
        for mount_key, mc in self._resolver.iter_mounts():
            self._init_mount_watch(mount_key, mc)
        self._observer.start()

        # 程序关闭期间的文件增删改无法由 watchdog 感知, 启动后后台对账一次;
        # 不阻塞启动, 对账过程产生的数据库变化由前端 SSE 自动刷新
        self._sha.run(self._startup_reconcile)

        log.info(f"FileManager initialized (gallery={self._gallery_root}, "
                 f"cache={self._cache_root}, read_only={self._read_only})")

    # ------------------------------------------------------------------ #
    # 关闭
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        """停止监控、取消定时任务、关闭线程池并清理临时数据。"""
        self._shutting_down = True

        try:
            self._observer.stop()
            self._observer.join(timeout=5)
        except Exception as e:  # noqa: BLE001 关闭阶段尽力而为
            log.error(f"Failed to stop watchdog observer: {e}")

        with self._timers_lock:
            timers = list(self._timers)
        for t in timers:
            t.cancel()

        self._sha.shutdown(wait=True, cancel_futures=True)
        self._upload_executor.shutdown(wait=True, cancel_futures=True)

        self._whitelist.clear()

        # 清理 /cache 内的上传临时文件与内存映射表
        with self._upload_lock:
            temp_names = list(self._upload_map)
            self._upload_map.clear()
        for name in temp_names:
            try:
                os.remove(os.path.join(self._upload_folder, name))
            except OSError:
                pass

        # 读取流 (read_file) 由调用者负责关闭, 本模块不做跟踪回收
        log.info("FileManager shut down")

    # ------------------------------------------------------------------ #
    # 只读保护
    # ------------------------------------------------------------------ #

    def _build_readonly_cache(self) -> None:
        """遍历路径表预计算 {完整逻辑路径: 是否只读}。"""
        cache: dict[str, bool] = {GALLERY: False}
        with self._db_lock:
            root = self.db.get_root_path()
            if root is None:
                self._readonly_cache = cache
                return
            stack: list[tuple[str, str]] = [(root["uuid"], GALLERY)]
            while stack:
                parent_uuid, parent_lp = stack.pop()
                for child in self.db.list_paths(parent_uuid):
                    lp = parent_lp + "/" + child["name"]
                    rel_key = lp[len(GALLERY):] or "/"
                    mc = self._resolver.mount_config(rel_key)
                    cache[lp] = bool(mc.readonly) if mc is not None else cache[parent_lp]
                    stack.append((child["uuid"], lp))
        self._readonly_cache = cache

    def update_mount_configs(self, mount_configs: dict) -> None:
        """挂载配置变更时更新内存配置、只读缓存并补注册新挂载点监控。

        挂载配置由 path_resolver 唯一持有, 本方法只做转发与联动。
        """
        self._resolver.update_mount_configs(mount_configs)
        self._build_readonly_cache()
        for mount_key, mc in self._resolver.iter_mounts():
            if mount_key not in self._mount_state:
                self._init_mount_watch(mount_key, mc)

    @property
    def resolver(self) -> PathResolver:
        """路径解析器 (只读复用; 缩略图模块等需要 sha -> 路径查询时使用)。"""
        return self._resolver

    def _write_guard(self, segments: list[str],
                     destination: Literal["mount", "local"] | None) -> None:
        """/gallery 写操作前的只读与挂载选择检查。"""
        lp = join_logical_path("gallery", segments)
        if self._read_only:
            log.warning(f"Write rejected (program-level read-only): {lp}")
            raise ReadOnlyError(
                f"Program-level read-only mode is enabled, write rejected: {lp}")
        if self._readonly_cache.get(lp, False):
            log.warning(f"Write rejected (directory is read-only): {lp}")
            raise ReadOnlyError(f"Directory is read-only, write rejected: {lp}")
        mount_key = self._resolver.mount_key_of(segments)
        if mount_key is not None and destination is None:
            raise UserChoiceRequired(
                f"Write target is inside a read-write mount, "
                f"choose a destination: {lp}",
                options=["mount", "local"],
                logical_path=lp)

    # ------------------------------------------------------------------ #
    # 路径解析与数据库查询辅助
    # ------------------------------------------------------------------ #

    def _resolve_parent(
            self, kind: str, segments: list[str],
            destination: Literal["mount", "local"] | None = None,
            guard: bool = False, must_exist: bool = False,
            guard_segments: list[str] | None = None,
            ) -> tuple[str, str | None, str | None]:
        """逻辑父目录解析 (创建/重命名/删除/读写共用的公共骨架)。

        cache: 直接映射缓存根; gallery: 按需做写前检查, 并经路径表校验存在性。

        :param guard: gallery 分支是否执行写前检查 (只读/挂载选择)
        :param must_exist: cache 分支是否要求父目录已存在
        :param guard_segments: 只读检查用的段列表 (缺省用 segments;
            文件夹级操作需把自身段拼在末尾)
        :return: (父目录物理路径, 目录节点 uuid [仅 gallery], 带前缀逻辑路径 [仅 gallery])
        """
        if kind == "cache":
            parent_phys = os.path.join(self._cache_root, *segments)
            if must_exist and not os.path.isdir(parent_phys):
                raise PathNotFoundError(
                    f"Logical path not found: {join_logical_path(kind, segments)}")
            return parent_phys, None, None
        if guard:
            self._write_guard(guard_segments if guard_segments else segments,
                              destination)
        parent_uuid, parent_lp = self._resolver.walk_segments(segments)
        parent_phys = self._resolver.segments_to_physical(segments, destination)
        return parent_phys, parent_uuid, parent_lp

    def _get_child(self, parent_uuid: str, name: str) -> dict[str, Any] | None:
        """按名称取直接子路径节点 (调用方需持有/可重入 _db_lock)。"""
        return next(
            (c for c in self.db.list_paths(parent_uuid) if c["name"] == name),
            None)

    def _get_file_row(self, parent_uuid: str,
                      filename: str) -> dict[str, Any] | None:
        """按文件名取该目录下的文件记录 (调用方需持有/可重入 _db_lock)。"""
        return next(
            (r for r in self.db.list_files_by_path(parent_uuid)
             if r["filename"] == filename), None)

    # ------------------------------------------------------------------ #
    # 对外服务接口: 创建 / 重命名 / 对外删除 / 读取
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
        validate_name(name)
        kind, segments = split_logical_path(parent_path)
        new_lp = join_logical_path(kind, segments + [name])
        label = "Folder" if is_dir else "File"
        parent_phys, parent_uuid, parent_lp = self._resolve_parent(
            kind, segments, destination, guard=True, must_exist=(kind == "cache"))
        target = os.path.join(parent_phys, name)
        if os.path.exists(target):
            raise NameConflictError(f"{label} already exists: {new_lp}")

        op_type = _OpType.CREATE_DIR if is_dir else _OpType.CREATE_FILE
        if kind == "cache":
            self._touch(target, is_dir)
            log.info(f"{label} created: {new_lp}")
            return new_lp

        # /gallery: 落盘后通知 DataBase
        self._whitelist.mark(target, op_type)
        # destination="local" 时本地影子目录可能尚不存在, 逐级补齐
        os.makedirs(parent_phys, exist_ok=True)
        self._touch(target, is_dir)
        with self._db_lock:
            if is_dir:
                self.db.add_path(name, path_type="inbuilt",
                                 parent_uuid=parent_uuid,
                                 path_uuid=str(uuid_lib.uuid4()))
            else:
                info_id = self._get_or_create_info(EMPTY_SHA256)
                self.db.add_file(info_id, parent_uuid, name, _guess_mime(name),
                                 file_size=0)
        self._readonly_cache[new_lp] = self._readonly_cache.get(parent_lp, False)
        log.info(f"{label} created: {new_lp}")
        return new_lp

    @staticmethod
    def _touch(physical: str, is_dir: bool = False) -> None:
        """在磁盘上创建空文件夹或空文件。"""
        if is_dir:
            os.makedirs(physical)
        else:
            with open(physical, "wb"):
                pass

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
        """重命名文件/文件夹的公共流程: 校验 -> 冲突检查 -> 落盘 -> (gallery) 入库。"""
        validate_name(new_name)
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        label = "Folder" if is_dir else "File"
        # 文件夹级只读检查针对自身 (segments + old_name), 文件级针对父目录
        parent_phys, parent_uuid, _ = self._resolve_parent(
            kind, segments, destination, guard=(kind == "gallery"),
            guard_segments=segments + [old_name] if is_dir else None)

        if kind == "cache":
            self._rename_on_disk(parent_phys, old_name, new_name, lp)
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
            with self._db_lock:
                dir_row = self._get_child(parent_uuid, old_name)
            if dir_row is None:
                raise PathNotFoundError(f"Logical path not found: {lp}/{old_name}")

        op_type = _OpType.RENAME_DIR if is_dir else _OpType.RENAME_FILE
        self._whitelist.mark(src, op_type)
        self._whitelist.mark(dst, op_type)
        os.replace(src, dst)
        with self._db_lock:
            if is_dir:
                self.db.update_path(dir_row["uuid"], name=new_name)
            else:
                self._db_rename_file(parent_uuid, old_name, new_name)
        if is_dir:
            self._build_readonly_cache()  # 子路径键前缀变化, 整体重建
        log.info(f"{label} renamed: {lp}/{old_name} -> {new_name}")

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

    def move_entry(self, source_parent: str, name: str, target_parent: str,
                   is_dir: bool = False,
                   destination: Literal["mount", "local"] | None = None) -> str:
        """跨目录移动文件/文件夹 (仅 /gallery), 返回新逻辑路径。

        目标目录已存在同名项时抛 NameConflictError; 文件夹不得移动进自身子树;
        源与目标目录相同时抛 InvalidPathError (同级改名请用 rename_*) 。
        """
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
        src_parent_phys, src_parent_uuid, _ = self._resolve_parent(
            src_kind, src_segments, "mount", guard=True,
            guard_segments=src_segments + [name] if is_dir else None)
        dst_parent_phys, dst_parent_uuid, dst_lp = self._resolve_parent(
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
        op_type = _OpType.RENAME_DIR if is_dir else _OpType.RENAME_FILE
        if is_dir:
            for dirpath, _dirnames, filenames in os.walk(src):
                self._whitelist.mark(dirpath, op_type)
                for fn in filenames:
                    self._whitelist.mark(os.path.join(dirpath, fn), op_type)
        else:
            self._whitelist.mark(src, op_type)
        self._whitelist.mark(dst, op_type)

        os.makedirs(dst_parent_phys, exist_ok=True)
        shutil.move(src, dst)  # 跨文件系统时自动复制后删除源

        with self._db_lock:
            new_segments = dst_segments + [name]
            if is_dir:
                self._move_dir_records(src_parent_uuid,
                                       src_segments + [name], new_segments,
                                       dst_parent_uuid)
            else:
                self._move_file_records(src_parent_uuid,
                                        src_segments + [name],
                                        new_segments, dst_parent_uuid)
        if is_dir:
            self._build_readonly_cache()
        new_lp = join_logical_path("gallery", new_segments)
        log.info(f"{label} moved: {src_lp} -> {new_lp}")
        return new_lp

    def entry_stat(self, parent_path: str, name: str) -> dict[str, Any] | None:
        """返回条目物理信息 (size/created_time/edit_time/is_dir); 不存在返回 None。"""
        validate_name(name)
        kind, segments = split_logical_path(parent_path)
        phys = os.path.join(self._resolve_parent(kind, segments)[0], name)
        try:
            st = os.stat(phys)
        except OSError:
            return None
        return {"size": int(st.st_size), "created_time": int(st.st_ctime),
                "edit_time": int(st.st_mtime), "is_dir": os.path.isdir(phys)}

    def folder_stats(self, parent_path: str) -> dict[str, int]:
        """递归统计逻辑目录下的文件数与总字节数 (供缓存占用等展示使用)。"""
        kind, segments = split_logical_path(parent_path)
        base = self._resolve_parent(kind, segments)[0]
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

    def discard_upload(self, source_filename: str) -> None:
        """丢弃一个上传临时文件 (分块合并后清理或失败时清理半成品)。"""
        if (not source_filename or "/" in source_filename
                or "\\" in source_filename or ".." in source_filename):
            raise InvalidPathError(
                f"Invalid upload temp file name: {source_filename!r}")
        with self._upload_lock:
            self._upload_map.pop(source_filename, None)
        try:
            os.remove(os.path.join(self._upload_folder, source_filename))
        except OSError:
            pass

    def _delete_entry(self, parent_path: str, name: str, confirm: bool,
                      destination: Literal["mount", "local"] | None,
                      is_dir: bool) -> None:
        """删除文件/文件夹的公共流程: 校验 -> 冲突检查 -> 落盘 -> (gallery) 入库。"""
        validate_name(name)  # 拒绝 ".." / 路径分隔符, 防止路径穿越
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        label = "Folder" if is_dir else "File"
        parent_phys, parent_uuid, _ = self._resolve_parent(
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
                self._rmtree(target)
            else:
                os.remove(target)
            log.info(f"{label} deleted: {lp}/{name}")
            return

        if is_dir:
            with self._db_lock:
                dir_row = self._get_child(parent_uuid, name)
            if dir_row is None:
                raise PathNotFoundError(f"Logical path not found: {lp}/{name}")
            # 递归删除前, 将整棵子树写入白名单 (子项事件同样需要去重)
            for dirpath, _dirnames, filenames in os.walk(target):
                self._whitelist.mark(dirpath, _OpType.DELETE_DIR)
                for fn in filenames:
                    self._whitelist.mark(
                        os.path.join(dirpath, fn), _OpType.DELETE_FILE)
            self._rmtree(target)
        else:
            self._whitelist.mark(target, _OpType.DELETE_FILE)
            os.remove(target)

        with self._db_lock:
            if is_dir:
                # 自底向上逐条通知 DataBase: 先文件记录, 后路径记录
                self._remove_dir_records(dir_row["uuid"])
            else:
                self.db.delete_file(parent_uuid, name)
        if is_dir:
            self._build_readonly_cache()
        log.info(f"{label} deleted: {lp}/{name}")

    def read_file(self, parent_path: str, filename: str) -> io.BufferedReader:
        """以二进制只读模式打开文件, 返回流对象。

        不检查只读; /gallery 与 /cache 均可读取。调用者负责关闭流,
        若传输过程中文件被删除/移动导致读取失败, 由调用者处理。
        """
        validate_name(filename)  # 拒绝 ".." / 路径分隔符, 防止路径穿越
        kind, segments = split_logical_path(parent_path)
        lp = join_logical_path(kind, segments)
        phys = os.path.join(self._resolve_parent(kind, segments)[0], filename)
        if not os.path.isfile(phys):
            raise FMFileNotFound(f"File not found: {lp}/{filename}")
        return open(phys, "rb")

    def file_exists(self, parent_path: str, filename: str) -> bool:
        """检查文件是否已存在 (/gallery 与 /cache 均可)。"""
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        phys = os.path.join(self._resolve_parent(kind, segments)[0], filename)
        return os.path.isfile(phys)

    def write_file(self, parent_path: str, filename: str, data: bytes) -> None:
        """将二进制内容写入文件 (/gallery 与 /cache 均可)。

        先写同目录唯一命名的临时文件再 os.replace 原子替换, 避免写入中断
        产生损坏文件。/cache 直接写入; /gallery 需通过只读与挂载选择检查,
        且不写入去重白名单 (新增/变更由监控线程照常入库)。
        """
        validate_name(filename)
        kind, segments = split_logical_path(parent_path)
        parent_phys = self._resolve_parent(
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
        """列出逻辑目录下的全部文件记录。

        /gallery: 记录来自 DataBase (info_id/path_uuid/filename/mime_type/
        added_at/directory), 并经 info 关联附加 sha256。
        /cache: 直接枚举磁盘目录, 无数据库记录 (sha256 为 None)。
        """
        kind, segments = split_logical_path(parent_path)
        parent_phys, _, _ = self._resolve_parent(
            kind, segments, must_exist=(kind == "cache"))
        if kind == "cache":
            directory = join_logical_path("cache", segments)
            return [{"filename": name, "sha256": None, "directory": directory}
                    for name in sorted(os.listdir(parent_phys))
                    if os.path.isfile(os.path.join(parent_phys, name))]
        with self._db_lock:
            dir_uuid, _ = self._resolver.walk_segments(segments)
            rows = self.db.list_files_by_path(dir_uuid)
            # 经 info 关联补充 sha256 (walrus 避免重复查询)
            return [{**r,
                     "sha256": (info["sha256"]
                                if (info := self.db.get_info(int(r["info_id"])))
                                else None)}
                    for r in rows]

    # ------------------------------------------------------------------ #
    # SHA256 计算服务 (计算逻辑委托 scripts/sha256.py)
    # ------------------------------------------------------------------ #

    def compute_sha256(self, parent_path: str, filename: str,
                       callback: Callable[[str | None, str | None], None]) -> None:
        """异步计算文件 SHA256, 结果通过 callback(sha256_hex|None, error|None) 返回。

        回调在工作线程中执行, 不在主线程。
        """
        validate_name(filename)  # 拒绝 ".." / 路径分隔符, 防止路径穿越
        kind, segments = split_logical_path(parent_path)
        try:
            parent_phys = self._resolve_parent(kind, segments)[0]
        except PathNotFoundError:
            callback(None, "file_not_found")
            return
        phys = os.path.join(parent_phys, filename)
        if not os.path.isfile(phys):
            callback(None, "file_not_found")
            return
        self._sha.submit(phys, callback)

    # ------------------------------------------------------------------ #
    # 上传流程
    # ------------------------------------------------------------------ #

    def open_upload_file(self, original_filename: str,
                         target_parent: str) -> tuple[str, io.BufferedWriter]:
        """开始一次上传: 生成临时文件 (uuid 命名, 防重名) 并返回可写流。

        :return: (临时文件名[即 upload_move 的 source_filename], 写入流)
        调用者负责写入并关闭流, 随后调用 upload_move 完成移动。
        """
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

        os.makedirs(self._upload_folder, exist_ok=True)
        temp_name = str(uuid_lib.uuid4())
        with self._upload_lock:
            self._upload_map[temp_name] = {
                "original_name": original_filename,
                "target_parent": target_parent,
            }
        stream = open(os.path.join(self._upload_folder, temp_name), "wb")
        return temp_name, stream

    def upload_move(self, source_filename: str, target_parent: str, sha256: str,
                    destination: Literal["mount", "local"] | None = None,
                    callback: Callable[[str | None, str | None], None] | None = None
                    ) -> None:
        """将 UPLOAD_FOLDER 中的临时文件移动到目标位置并入库。

        :param sha256: 上传内容的预期 SHA256 (64 位十六进制, 大小写不敏感)。

        SHA256 校验与只读/挂载选择检查在调用线程同步执行 (不一致时抛
        SHA256Error, 只读时抛 ReadOnlyError, 挂载点内写入时抛 UserChoiceRequired) ;
        校验通过前不进入移动阶段。实际移动在上传工作线程 (x2) 中排队执行,
        结果经 callback(新逻辑路径|None, 错误|None) 返回 (工作线程中执行)。
        移动失败时保留临时文件与映射表, 由调用者决定重试或清理。
        跨文件系统移动时复制后二次比对哈希, 不一致自动重新复制
        (最多 _COPY_MAX_RETRY 次) , 仍失败则清理半成品并经 callback 报错。
        """
        expected = normalize_sha256(sha256)
        if expected is None:
            raise InputDataError(f"Invalid SHA256 format: {sha256!r}")
        kind, segments = split_logical_path(target_parent)
        if kind != "gallery":
            log.error("Cross-root operation rejected: upload target must be "
                      f"under /gallery: {target_parent}")
            raise CrossRootError(
                f"Upload target must be under /gallery: {target_parent}")
        self._write_guard(segments, destination)  # 可能抛 ReadOnlyError/UserChoiceRequired

        # 进入移动阶段前, 在调用线程校验上传内容的哈希 (mtime 变化会自动重试)
        temp_path = os.path.join(self._upload_folder, source_filename)
        digest, hash_err = self._sha.compute(temp_path)
        if digest is None:
            raise SHA256Error(
                f"Failed to compute SHA256 of uploaded file: {hash_err}")
        if digest != expected:
            raise SHA256Error(
                f"Uploaded file SHA256 mismatch: expected {expected}, got {digest}")

        self._upload_executor.submit(
            self._upload_task, source_filename, target_parent, expected,
            destination, callback)

    def _upload_task(self, source_filename: str, target_parent: str,
                     expected_sha: str,
                     destination: Literal["mount", "local"] | None,
                     callback: Callable[[str | None, str | None], None] | None) -> None:
        try:
            with self._upload_lock:
                entry = self._upload_map.get(source_filename)
            if entry is None:
                raise FileManagerError(f"Upload record not found: {source_filename}")

            _, segments = split_logical_path(target_parent)
            phys_dir, parent_uuid, parent_lp = self._resolve_parent(
                "gallery", segments, destination)
            os.makedirs(phys_dir, exist_ok=True)

            final_name = self._unique_name(phys_dir, entry["original_name"])
            src = os.path.join(self._upload_folder, source_filename)
            dst = os.path.join(phys_dir, final_name)

            self._whitelist.mark(dst, _OpType.CREATE_FILE)
            try:
                os.replace(src, dst)  # 同文件系统: 原子移动 (临时文件已校验, 不复验)
                digest = expected_sha
            except OSError:
                # 跨文件系统: 校验复制 (复制后二次比对哈希, 不一致重新复制)
                digest = self._copy_verified(src, dst, expected_sha)
                os.remove(src)

            width, height = probe_image_size(dst)
            with self._db_lock:
                info_id = self._get_or_create_info(digest)
                self.db.add_file(info_id, parent_uuid, final_name,
                                 _guess_mime(final_name),
                                 width=width, height=height,
                                 file_size=os.path.getsize(dst))

            # 上传走白名单路径, 不产生可入库的监控事件;
            # 与 _ingest_file_task 一致, 入库后异步触发元数据解析
            self._sha.run(self._parse_metadata_task, parent_uuid, final_name,
                          info_id, dst)

            with self._upload_lock:
                self._upload_map.pop(source_filename, None)
            new_lp = parent_lp + "/" + final_name
            log.info(f"Upload completed: {source_filename} -> {new_lp}")
            if callback is not None:
                callback(new_lp, None)
        except Exception as e:  # noqa: BLE001 失败时保留临时文件与映射表
            log.warning(f"Upload failed: {source_filename} ({e})")
            if callback is not None:
                callback(None, str(e))

    @staticmethod
    def _unique_name(target_dir: str, filename: str) -> str:
        """重名时自动添加 (n) 后缀: file.png -> file(1).png; 保留完整扩展名。

        使用 os.path.exists() 检查, 不依赖数据库; 最多尝试 100 次。
        """
        stem, ext = _split_name_ext(filename)
        for n in range(_UPLOAD_MAX_TRIALS + 1):
            candidate = filename if n == 0 else f"{stem}({n}){ext}"
            if not os.path.exists(os.path.join(target_dir, candidate)):
                if n > 0:
                    log.info(f"Duplicate name, auto-appended (n) suffix: "
                             f"{filename} -> {candidate}")
                return candidate
        raise NameConflictError(
            f"Failed to find an available filename after "
            f"{_UPLOAD_MAX_TRIALS} attempts: {filename}")

    @staticmethod
    def _copy_file(src: str, dst: str) -> None:
        """分块复制文件; 失败时清理半成品。"""
        try:
            with open(src, "rb") as fs, open(dst, "wb") as fd:
                while True:
                    chunk = fs.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fd.write(chunk)
        except OSError:
            try:
                os.remove(dst)
            except OSError:
                pass
            raise

    def _copy_verified(self, src: str, dst: str, expected_sha: str) -> str:
        """跨文件系统校验复制: 复制后二次比对哈希, 不一致重新复制。

        最多尝试 _COPY_MAX_RETRY 次, 通过校验后返回 SHA256;
        仍失败则清理目标半成品并抛 SHA256Error。
        """
        for attempt in range(1, _COPY_MAX_RETRY + 1):
            self._copy_file(src, dst)
            digest, hash_err = self._sha.compute(dst)
            if digest is not None and digest == expected_sha:
                if attempt > 1:
                    log.warning(
                        f"Cross-filesystem copy verified on retry: {dst} "
                        f"(attempt {attempt}/{_COPY_MAX_RETRY})")
                return digest
            log.warning(
                f"Cross-filesystem copy SHA256 mismatch, re-copying: {dst} "
                f"(attempt {attempt}/{_COPY_MAX_RETRY}"
                + (f", hash error: {hash_err}" if hash_err else "") + ")")
        try:
            os.remove(dst)
        except OSError:
            pass
        raise SHA256Error(
            f"Cross-filesystem copy SHA256 mismatch after "
            f"{_COPY_MAX_RETRY} attempts: {dst}")

    # ------------------------------------------------------------------ #
    # DataBase 交互辅助 (调用方需持有/可重入 _db_lock)
    # ------------------------------------------------------------------ #

    def _get_or_create_info(self, sha256: str) -> int:
        """按 SHA256 复用 info 记录; 不存在则以空元数据创建 (rating=1)。"""
        existing = self.db.get_info_by_sha256(sha256)
        if existing is not None:
            return int(existing["id"])
        return int(self.db.create_info(
            sha256, seed=0, steps=0, cfg=0.0, sampler="", scheduler="",
            tool="", rating=1, positive_prompt="", negative_prompt="",
            note=""))

    def _db_rename_file(self, parent_uuid: str, old_name: str,
                        new_name: str) -> None:
        """file 表重命名: 删除旧记录并以原 info_id 新建记录。"""
        row = self._get_file_row(parent_uuid, old_name)
        if row is None:
            log.warning(f"No database record found for file, "
                        f"rename skipped: {old_name}")
            return
        # 原地更新文件名, 保留 file.id 与文件-模型关联
        if not self.db.rename_file(parent_uuid, old_name, new_name):
            log.warning(f"File record rename failed: {old_name} -> {new_name}")

    def _remove_dir_records(self, dir_uuid: str) -> None:
        """递归删除路径节点子树记录: 先文件记录, 后路径记录 (自底向上)。"""
        for f in self.db.list_files_by_path(dir_uuid):
            self.db.delete_file(dir_uuid, f["filename"])
        for child in self.db.list_paths(dir_uuid):
            self._remove_dir_records(child["uuid"])
        self.db.delete_path(dir_uuid)

    def _ensure_dir_records(self, segments: list[str]) -> str:
        """确保 /gallery 各级目录节点存在 (缺失时以 inbuilt 逐级创建)。"""
        with self._db_lock:
            root = self.db.get_root_path()
            if root is None:
                raise PathNotFoundError("Root path record is missing")
            current = root["uuid"]
            lp = GALLERY
            for seg in segments:
                child = self._get_child(current, seg)
                if child is None:
                    new_uuid = str(uuid_lib.uuid4())
                    self.db.add_path(seg, path_type="inbuilt",
                                     parent_uuid=current, path_uuid=new_uuid)
                    new_lp = lp + "/" + seg
                    self._readonly_cache[new_lp] = \
                        self._readonly_cache.get(lp, False)
                    current = new_uuid
                else:
                    current = child["uuid"]
                lp += "/" + seg
            return current

    # ------------------------------------------------------------------ #
    # 磁盘操作辅助
    # ------------------------------------------------------------------ #

    @staticmethod
    def _rename_on_disk(parent_phys: str, old_name: str, new_name: str,
                        lp: str) -> None:
        src = os.path.join(parent_phys, old_name)
        dst = os.path.join(parent_phys, new_name)
        if not os.path.exists(src):
            raise FMFileNotFound(f"Target not found: {lp}/{old_name}")
        if os.path.exists(dst):
            raise NameConflictError(f"Name already taken: {lp}/{new_name}")
        os.replace(src, dst)

    @staticmethod
    def _rmtree(physical: str) -> None:
        """递归删除目录树 (仅用 os, 不引入额外库)。"""
        for entry in os.listdir(physical):
            p = os.path.join(physical, entry)
            if os.path.isdir(p):
                FileManager._rmtree(p)
            else:
                os.remove(p)
        os.rmdir(physical)

    # ------------------------------------------------------------------ #
    # watchdog: 注册 / 事件分发
    # ------------------------------------------------------------------ #

    def _start_watch(self, physical_root: str, logical_root: str):
        """注册监控, 返回 watch 句柄 (供后续 unschedule 移除); 失败返回 None。"""
        try:
            watch = self._observer.schedule(
                self._watch_handler, physical_root, recursive=True)
        except Exception as e:  # noqa: BLE001 目录不可注册时按离线处理
            log.error(f"Failed to register watch for {physical_root}: {e}")
            return None
        self._watch_roots[_norm_physical(physical_root)] = (
            logical_root, physical_root)
        log.info(f"Watch registered: {logical_root} -> {physical_root}")
        return watch

    def _init_mount_watch(self, mount_key: str, mc: Any) -> None:
        """挂载点: 路径表中已有记录才注册监控, 并启动健康检查。"""
        segments = [s for s in mount_key.split("/") if s]
        logical = GALLERY + mount_key
        state: dict[str, Any] = {
            "physical": mc.physical_path,
            "logical": logical,
            "segments": segments,
            "online": True,
            "confirming": False,
            "watch": None,
        }
        self._mount_state[mount_key] = state
        try:
            with self._db_lock:
                self._resolver.walk_segments(segments)  # 路径表记录存在才允许监控
        except PathNotFoundError:
            log.warning(f"Mount has no path-table record, watch deferred: "
                        f"{mount_key}")
            state["online"] = False
            self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)
            return
        watch = self._start_watch(mc.physical_path, logical)
        if watch is None:
            state["online"] = False
            self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)
            return
        state["watch"] = watch
        self._spawn_timer(_HEALTH_CHECK_INTERVAL, self._health_check, mount_key)

    def _locate_root(self, physical: str) -> tuple[str, str] | None:
        """物理路径归属到已注册的监控根 -> (逻辑根, 相对逻辑路径段串)。

        候选集是 _watch_roots (当前已注册/在线的根), 匹配算法委托
        path_resolver.match_root (最长前缀)。
        """
        hit = match_root(physical, self._watch_roots)
        if hit is None:
            return None
        logical_root, original_root = hit
        rel = os.path.relpath(physical, original_root)
        if rel == ".":
            return logical_root, ""
        parts = [p for p in rel.replace(os.sep, "/").split("/") if p]
        return logical_root, "/".join(parts)

    def _dispatch_event(self, kind: str, event: Any) -> None:
        try:
            self._handle_event(kind, event)
        except (OSError, PermissionError):
            # watchdog 回调中的 OSError/PermissionError 触发离线确认
            mount_key = self._mount_key_of_physical(event.src_path)
            if mount_key is not None:
                self._begin_offline_confirmation(mount_key)
            else:
                log.error(f"Filesystem event error: {event.src_path}")
        except Exception as e:  # noqa: BLE001 单个事件失败不能拖垮监控线程
            log.error(f"Filesystem event failed: {event.src_path} ({e})")

    def _mount_key_of_physical(self, physical: str) -> str | None:
        """物理路径归属的挂载点 key (路径转换, 委托 path_resolver 最长前缀匹配)。"""
        return self._resolver.physical_to_mount_key(physical)

    def _handle_event(self, kind: str, event: Any) -> None:
        if self._shutting_down:
            return
        src = event.src_path
        dst = getattr(event, "dest_path", "") or ""
        if kind == "moved":
            # 原子写入 (临时文件 -> 目标重命名) 表现为 src 可忽略而 dst 有效的
            # moved 事件, 此时不能丢弃, 交由 _on_moved 按 dst 新增处理
            if _should_ignore(src) and _should_ignore(dst):
                return
        elif _should_ignore(src):
            return

        if kind in ("created", "deleted"):
            is_dir = event.is_directory
            op_type = None
            if kind == "created":
                op_type = _OpType.CREATE_DIR if is_dir else _OpType.CREATE_FILE
                handler = self._on_created
            else:
                op_type = _OpType.DELETE_DIR if is_dir else _OpType.DELETE_FILE
                handler = self._on_deleted
            if self._whitelist.check(src, op_type):
                log.debug(f"Whitelist hit (event skipped): {src}")
                return
            located = self._locate_root(src)
            if located is None:
                return
            logical_root, rel = located
            if not rel:
                return
            handler(is_dir, logical_root, rel)
        elif kind == "moved":
            op_type = _OpType.RENAME_DIR if event.is_directory else _OpType.RENAME_FILE
            if self._whitelist.check(dst, op_type) \
                    or self._whitelist.check(src, op_type):
                log.debug(f"Whitelist hit (event skipped): {src} -> {dst}")
                return
            self._on_moved(event.is_directory, src, dst)

    # ---- 事件处理 ---- #

    def _on_created(self, is_dir: bool, logical_root: str, rel: str) -> None:
        gallery_segments = self._gallery_segments(logical_root, rel)
        if gallery_segments is None:
            return
        if is_dir:
            self._ensure_dir_records(gallery_segments)
        else:
            parent_uuid = self._ensure_dir_records(gallery_segments[:-1])
            name = gallery_segments[-1]
            physical = self._resolver.segments_to_physical(gallery_segments, None)
            self._sha.run(
                self._ingest_file_task, parent_uuid, name, physical)

    def _on_deleted(self, is_dir: bool, logical_root: str, rel: str) -> None:
        gallery_segments = self._gallery_segments(logical_root, rel)
        if gallery_segments is None:
            return
        with self._db_lock:
            try:
                parent_uuid, _ = self._resolver.walk_segments(gallery_segments[:-1])
            except PathNotFoundError:
                return
            name = gallery_segments[-1]
            if is_dir:
                row = self._get_child(parent_uuid, name)
                if row is not None:
                    self._remove_dir_records(row["uuid"])
            else:
                self.db.delete_file(parent_uuid, name)
        if is_dir:
            self._build_readonly_cache()

    def _on_moved(self, is_dir: bool, src: str, dst: str) -> None:
        src_located = self._locate_root(src)
        dst_located = self._locate_root(dst)

        if src_located is None and dst_located is None:
            return
        if src_located is None:
            # 从监控范围外移入: 按新增处理
            logical_root, rel = dst_located  # type: ignore[misc]
            if rel:
                self._on_created(is_dir, logical_root, rel)
            return
        if dst_located is None:
            # 移出监控范围: 按删除处理
            logical_root, rel = src_located
            if rel:
                self._on_deleted(is_dir, logical_root, rel)
            return

        src_root, src_rel = src_located
        dst_root, dst_rel = dst_located
        if not src_rel and not dst_rel:
            return
        src_segments = self._gallery_segments(src_root, src_rel)
        dst_segments = self._gallery_segments(dst_root, dst_rel)
        if src_segments is None or dst_segments is None:
            return

        with self._db_lock:
            try:
                src_parent_uuid, _ = self._resolver.walk_segments(src_segments[:-1])
            except PathNotFoundError:
                src_parent_uuid = None
            dst_parent_uuid = self._ensure_dir_records(dst_segments[:-1])
            if is_dir:
                self._move_dir_records(src_parent_uuid, src_segments,
                                       dst_segments, dst_parent_uuid)
            else:
                self._move_file_records(src_parent_uuid, src_segments,
                                        dst_segments, dst_parent_uuid)
        if is_dir:
            self._build_readonly_cache()

    def _move_dir_records(self, src_parent_uuid: str | None,
                          src_segments: list[str],
                          dst_segments: list[str],
                          dst_parent_uuid: str) -> None:
        """目录移动的记录迁移: 更新节点 parent_uuid/name, 保留子树与文件 id。"""
        name_old, name_new = src_segments[-1], dst_segments[-1]
        row = (self._get_child(src_parent_uuid, name_old)
               if src_parent_uuid is not None else None)
        if row is None:
            # 旧记录不存在: 按新增处理整棵子树
            self._ensure_dir_records(dst_segments)
            self._reregister_subtree(
                self._resolver.segments_to_physical(dst_segments, None),
                dst_segments)
        elif src_segments[:-1] == dst_segments[:-1]:
            # 同级重命名: 只需更新 name 字段
            self.db.update_path(row["uuid"], name=name_new)
        else:
            # 跨目录移动: 只迁移目录节点, 子树记录与文件-模型关联保持不变
            self.db.move_path(row["uuid"], dst_parent_uuid, name_new)

    def _move_file_records(self, src_parent_uuid: str | None,
                           src_segments: list[str],
                           dst_segments: list[str],
                           dst_parent_uuid: str) -> None:
        """文件移动的记录迁移: 同级改名、跨目录重挂 info, 或按新增入库。"""
        name_old, name_new = src_segments[-1], dst_segments[-1]
        row_file = (self._get_file_row(src_parent_uuid, name_old)
                    if src_parent_uuid is not None else None)
        if src_segments[:-1] == dst_segments[:-1]:
            if row_file is not None:
                self._db_rename_file(src_parent_uuid, name_old, name_new)
            else:
                # 磁盘上存在但无记录: 按新增入库
                physical = self._resolver.segments_to_physical(dst_segments, None)
                self._sha.run(self._ingest_file_task,
                              dst_parent_uuid, name_new, physical)
        elif row_file is not None:
            # 内容未变化: 原地迁移文件记录, 保留 file.id 与模型关联
            if not self.db.relocate_file(src_parent_uuid, name_old,
                                         dst_parent_uuid, name_new):
                log.warning(f"File record move failed: "
                            f"{name_old} -> {name_new}")
        else:
            physical = self._resolver.segments_to_physical(dst_segments, None)
            self._sha.run(self._ingest_file_task,
                          dst_parent_uuid, name_new, physical)

    @staticmethod
    def _gallery_segments(logical_root: str, rel: str) -> list[str] | None:
        """(逻辑根, 相对段串) -> /gallery 下的段列表; 非 gallery 根返回 None。"""
        if not logical_root.startswith(GALLERY):
            return None  # /cache 不监控, 理论上不会出现
        base = [s for s in logical_root[len(GALLERY):].split("/") if s]
        return base + ([s for s in rel.split("/") if s] if rel else [])

    # ---- 新文件入库流程 ---- #

    @staticmethod
    def _wait_file_ready(physical: str,
                         timeout: float = _READY_TIMEOUT) -> bool:
        """等待新文件写入完成 (大小、mtime 连续一次轮询不变且非空)。

        文件创建事件通常先于写入完成触发; 若立即计算哈希/探测分辨率,
        会把半成品 (甚至 0 字节) 当作最终内容入库, 导致空哈希记录、
        无分辨率、元数据解析失败。写入超过 timeout 仍为空视为空文件。
        """
        start = time.monotonic()
        deadline = start + timeout
        last: tuple[int, int] | None = None
        while True:
            try:
                st = os.stat(physical)
            except OSError:
                return False
            current = (int(st.st_size), int(st.st_mtime_ns))
            if st.st_size > 0 and current == last:
                return True
            if st.st_size == 0 and time.monotonic() - start >= _READY_EMPTY_TIMEOUT:
                return False  # 持续为空: 视为空文件 (空文件不参与入库)
            if time.monotonic() >= deadline:
                return st.st_size > 0
            last = current
            time.sleep(_READY_POLL_INTERVAL)

    def _ingest_file_task(self, parent_uuid: str, name: str,
                          physical: str) -> None:
        """新文件入库: SHA256 → 空元数据 info → file 记录 → 元数据解析。"""
        if not self._wait_file_ready(physical):
            log.warning(f"Failed to ingest new file (empty or unstable): "
                        f"{physical}")
            return
        digest: str | None = None
        file_size = 0
        for _attempt in range(_INGEST_MAX_RETRY):
            try:
                size_before = os.path.getsize(physical)
            except OSError:
                log.warning(f"Failed to ingest new file (disappeared): {physical}")
                return
            digest, err = self._sha.compute(physical)
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
            with self._db_lock:
                existing = self._get_file_row(parent_uuid, name)
                if existing is not None:
                    if int(existing.get("file_size") or 0) == file_size:
                        return  # 已有记录且内容大小一致, 避免重复入库
                    # 大小已变化: 旧记录是过期内容 (含空文件入库竞态), 重新入库
                    self.db.delete_file(parent_uuid, name)
                # 与元数据解析解耦: 无论能否解析生成参数, 分辨率都独立探测入库
                width, height = probe_image_size(physical)
                info_id = self._get_or_create_info(digest)
                self.db.add_file(info_id, parent_uuid, name, _guess_mime(name),
                                 width=width, height=height,
                                 file_size=file_size)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to ingest new file: {physical} ({e})")
            return
        # 解析器调用放在独立任务中, 不占用本任务剩余时间
        self._sha.run(self._parse_metadata_task, parent_uuid, name,
                      info_id, physical)

    def _parse_metadata_task(self, path_uuid: str, filename: str,
                             info_id: int, physical: str) -> None:
        """后台解析任务: 解析成功则写回 info/file/模型表。"""
        if self._parse_metadata is None:
            log.warning(f"Metadata parser unavailable, skipped: {physical}")
            return
        try:
            meta = self._parse_metadata(physical)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to parse metadata: {physical} ({e})")
            return
        if not meta:
            return
        self._apply_metadata(path_uuid, filename, info_id, meta, physical)

    def _apply_metadata(self, path_uuid: str, filename: str, info_id: int,
                        meta: dict[str, Any], physical: str) -> None:
        """把解析结果写入数据库: info 字段 + 文件尺寸 + 模型绑定。"""
        # models 不在白名单内, 过滤天然将其挡在 update_info 之外
        models = meta.get("models")
        updates = {k: v for k, v in meta.items() if k in _META_UPDATABLE_FIELDS}
        file_updates = {k: v for k, v in meta.items() if k in _META_FILE_FIELDS}
        try:
            with self._db_lock:
                if updates:
                    self.db.update_info(info_id, **updates)
                if file_updates:
                    self.db.update_file(path_uuid, filename, **file_updates)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Failed to write metadata: {physical} ({e})")
        if isinstance(models, dict):
            try:
                with self._db_lock:
                    self.db.update_file_models(
                        path_uuid, filename,
                        checkpoints=models.get("checkpoints"),
                        loras=models.get("loras"),
                        embeddings=models.get("embeddings"),
                        vaes=models.get("vaes"),
                    )
            except Exception as e:  # noqa: BLE001
                log.warning(f"Failed to write model bindings: {physical} ({e})")

    def reparse_metadata(self, parent_path: str, filename: str) -> dict[str, Any]:
        """重新读取文件元数据并写回数据库 (右键菜单)。

        仅支持 /gallery。分辨率与文件大小始终独立探测刷新, 与能否解析
        生成参数无关; 无生成元数据时返回空 dict。
        :return: 解析出的元数据 dict (无元数据时为空 dict)。
        """
        validate_name(filename)
        if self._parse_metadata is None:
            raise FileManagerError("Metadata parser is not available")
        kind, segments = split_logical_path(parent_path)
        if kind != "gallery":
            raise CrossRootError(
                f"Metadata can only be parsed under /gallery: {parent_path}")
        parent_phys, parent_uuid, _ = self._resolve_parent(kind, segments)
        physical = os.path.join(parent_phys, filename)
        if not os.path.isfile(physical):
            raise FMFileNotFound(f"File not found: {parent_path}/{filename}")
        with self._db_lock:
            row = self._get_file_row(parent_uuid, filename)
        if row is None:
            raise FMFileNotFound(
                f"File record not found: {parent_path}/{filename}")
        meta = self._parse_metadata(physical)
        # 分辨率与文件大小和"能否解析生成参数"无关: 每次重新读取都独立探测
        # 刷新, 覆盖旧记录中尺寸为 0 的情况 (解析器未返回尺寸时同样生效)
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
            with self._db_lock:
                self.db.update_file(parent_uuid, filename, **file_updates)
        if not meta:
            log.info(f"No metadata found (resolution refreshed): "
                     f"{parent_path}/{filename}")
            return {}
        self._apply_metadata(parent_uuid, filename, int(row["info_id"]),
                             meta, physical)
        log.info(f"Metadata reparsed: {parent_path}/{filename}")
        return meta

    def _reregister_subtree(self, physical_dir: str,
                            segments: list[str]) -> str:
        """物理目录子树重新入库: 目录逐级确保记录, 文件异步哈希入库。"""
        base_uuid = self._ensure_dir_records(segments)
        for dirpath, _dirnames, filenames in os.walk(physical_dir):
            rel = os.path.relpath(dirpath, physical_dir)
            sub_segments = segments if rel == "." else \
                segments + rel.replace(os.sep, "/").split("/")
            dir_uuid = self._ensure_dir_records(sub_segments)
            for fn in filenames:
                if _should_ignore(fn):
                    continue
                self._sha.run(
                    self._ingest_file_task, dir_uuid, fn,
                    os.path.join(dirpath, fn))
        return base_uuid

    # ------------------------------------------------------------------ #
    # 启动对账: 程序关闭期间的文件系统改动
    # ------------------------------------------------------------------ #

    def _startup_reconcile(self) -> None:
        """后台对账 /gallery 与全部在线挂载点。

        程序关闭期间 watchdog 无法感知任何变化, 启动后通过比对磁盘与
        数据库补齐新增条目、移除失效记录; 已存在但大小变化的文件视为
        内容被替换, 重新入库。不可访问的挂载点跳过, 恢复后由恢复轮询
        重新注册监控 (其数据待下次启动或事件触发时同步)。
        """
        roots = [(self._gallery_root, GALLERY)]
        for mount_key, mc in self._resolver.iter_mounts():
            roots.append((mc.physical_path, GALLERY + mount_key))
        for physical, logical in roots:
            if not _is_accessible(physical):
                log.warning(f"Startup sync skipped (inaccessible): "
                            f"{logical} -> {physical}")
                continue
            segments = [s for s in logical[len(GALLERY):].split("/") if s]
            try:
                self._sync_dir(physical, segments)
            except Exception as e:  # noqa: BLE001 单个根失败不影响其他根
                log.error(f"Startup sync failed: {logical} ({e})")
        log.info("Startup database sync completed")

    def _sync_dir(self, physical_dir: str, segments: list[str]) -> None:
        """递归对账单个目录: 磁盘条目补齐入库, 数据库失效记录移除。"""
        try:
            with self._db_lock:
                dir_uuid, _ = self._resolver.walk_segments(segments)
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
            if _should_ignore(name):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                dir_names.add(name)
                child_segments = segments + [name]
                try:
                    with self._db_lock:
                        self._ensure_dir_records(child_segments)
                except Exception as e:  # noqa: BLE001
                    log.warning(f"Startup sync failed to register directory: "
                                f"{entry.path} ({e})")
                    continue
                self._sync_dir(entry.path, child_segments)
            else:
                file_names.add(name)
                self._sync_file(dir_uuid, name, entry.path)

        # 移除磁盘上已不存在的记录; 删除前再确认一次, 避免与并发新增竞态
        with self._db_lock:
            for child in self.db.list_paths(dir_uuid):
                if child["name"] in dir_names:
                    continue
                if os.path.isdir(os.path.join(physical_dir, child["name"])):
                    continue
                log.info(f"Startup sync removed directory record: "
                         f"{'/'.join(segments + [child['name']])}")
                self._remove_dir_records(child["uuid"])
            for row in self.db.list_files_by_path(dir_uuid):
                if row["filename"] in file_names:
                    continue
                if os.path.exists(os.path.join(physical_dir, row["filename"])):
                    continue
                log.info(f"Startup sync removed file record: "
                         f"{'/'.join(segments + [row['filename']])}")
                self.db.delete_file(dir_uuid, row["filename"])

    def _sync_file(self, parent_uuid: str, name: str, physical: str) -> None:
        """单个文件对账: 缺失则入库; 大小变化视为内容替换, 重新入库。"""
        try:
            size = os.path.getsize(physical)
        except OSError:
            return
        with self._db_lock:
            row = self._get_file_row(parent_uuid, name)
        if row is not None and int(row.get("file_size") or 0) == size:
            return
        if row is not None:
            log.info(f"Startup sync re-ingesting changed file: {physical}")
            with self._db_lock:
                self.db.delete_file(parent_uuid, name)
        if size > 0:
            self._sha.run(self._ingest_file_task, parent_uuid, name, physical)

    # ------------------------------------------------------------------ #
    # 挂载点离线检测与恢复
    # ------------------------------------------------------------------ #

    def _spawn_timer(self, delay: float, fn: Callable, *args: Any) -> None:
        def _wrapper() -> None:
            try:
                if not self._shutting_down:
                    fn(*args)
            finally:
                with self._timers_lock:
                    self._timers.discard(timer)

        timer = threading.Timer(delay, _wrapper)
        timer.daemon = True
        with self._timers_lock:
            self._timers.add(timer)
        timer.start()

    def _health_check(self, mount_key: str) -> None:
        """在线挂载点定时健康检查 (30 秒链式)。"""
        state = self._mount_state.get(mount_key)
        if state is None or self._shutting_down:
            return
        if not state["online"]:
            return  # 离线期间本链结束, 恢复在线后由恢复轮询重新启动
        if not _is_accessible(state["physical"]):
            self._begin_offline_confirmation(mount_key)
        self._spawn_timer(_HEALTH_CHECK_INTERVAL, self._health_check, mount_key)

    def _begin_offline_confirmation(self, mount_key: str) -> None:
        with self._state_lock:
            state = self._mount_state.get(mount_key)
            if state is None or state["confirming"] or not state["online"]:
                return
            state["confirming"] = True
        log.warning(f"Mount '{state['logical']}' seems offline, "
                    f"entering {_OFFLINE_CONFIRM_DELAY:.0f}s confirmation")
        self._spawn_timer(_OFFLINE_CONFIRM_DELAY, self._confirm_offline, mount_key)

    def _confirm_offline(self, mount_key: str) -> None:
        state = self._mount_state.get(mount_key)
        if state is None:
            return
        with self._state_lock:
            state["confirming"] = False
        if _is_accessible(state["physical"]):
            log.info(f"Mount '{state['logical']}' recovered during confirmation, "
                     f"offline cancelled")
            return

        log.error(f"Mount '{state['logical']}' confirmed offline")
        self._set_mount_status(state, "offline")
        # 通过注册时保存的 watch 句柄移除 schedule
        self._unschedule_mount(mount_key)
        with self._state_lock:
            state["online"] = False
        self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)

    def _set_mount_status(self, state: dict[str, Any], status: str) -> bool:
        """写挂载点路径表状态 (offline/normal), 失败记日志返回 False。"""
        try:
            with self._db_lock:
                dir_uuid, _ = self._resolver.walk_segments(state["segments"])
                self.db.update_path(dir_uuid, status=status)
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"Failed to set mount status '{status}': "
                      f"{state['logical']} ({e})")
            return False

    def _unschedule_mount(self, mount_key: str) -> None:
        state = self._mount_state.get(mount_key)
        if state is None:
            return
        watch = state.get("watch")
        if watch is not None:
            try:
                self._observer.unschedule(watch)
                log.info(f"Mount watch stopped: {state['logical']}")
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to remove mount watch: {state['logical']} ({e})")
            state["watch"] = None
        self._watch_roots.pop(_norm_physical(state["physical"]), None)

    def _recovery_poll(self, mount_key: str) -> None:
        """离线挂载点恢复轮询: 链式轮询, 恢复后重新注册监控。"""
        state = self._mount_state.get(mount_key)
        if state is None or self._shutting_down:
            return
        log.debug(f"Polling mount '{state['logical']}' ...")
        if not _is_accessible(state["physical"]):
            self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)
            return
        try:
            with self._db_lock:
                self._resolver.walk_segments(state["segments"])
        except PathNotFoundError:
            log.warning(f"Mount '{state['logical']}' still missing in path table, "
                        f"retry later")
            self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)
            return

        log.info(f"Mount '{state['logical']}' is back online")
        self._set_mount_status(state, "normal")
        watch = self._start_watch(state["physical"], state["logical"])
        if watch is None:
            self._spawn_timer(_RECOVERY_POLL_INTERVAL, self._recovery_poll, mount_key)
            return
        state["watch"] = watch
        with self._state_lock:
            state["online"] = True
        self._spawn_timer(_HEALTH_CHECK_INTERVAL, self._health_check, mount_key)
