"""thumbnail_gen.py — 缩略图生成模块。

只负责缩略图的生成逻辑 (缩放、格式转换) 。所有文件 I/O 委托 FileManager,
路径解析委托 PathResolver, 配置读取委托 ConfigManager, 本模块仅做
"/cache/image/{SHA256}/{短边长度}.webp" 这类逻辑路径的字符串拼接。

缩放规则: 等比缩放使短边等于 ThumbnailShortSideSize, 长边按比例计算;
若原图短边已小于等于目标值, 不放大, 按原尺寸转存 WebP。

线程模型: 生成在独立线程池中异步执行 (默认最多 2 个生成线程) ; 任务队列
按 (SHA256|源路径, 短边长度, overwrite) 去重, 队列中/执行中的重复提交直接
忽略 (返回 False) ; 队列线程安全, 提交接口可从任意线程调用。
单个任务失败只回调错误, 不影响其他任务与线程存活; 回调异常被隔离。

约束: 严禁绕过 FileManager 直接读写文件; 严禁直接读配置; 不引入
Pillow 之外的第三方依赖。
"""
import hashlib
import io
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from PIL import Image
from PIL import UnidentifiedImageError

from exceptions import FileManagerError, InputDataError
from scripts.logger import log
from scripts.path_resolver import GALLERY
from scripts.sha256 import normalize_sha256

# 成功回调参数: 缩略图逻辑路径 (如 "/cache/image/{sha}/512.webp") ; 失败时为 None
ThumbCallback = Callable[[str | None, str | None], None]


@dataclass(frozen=True)
class _Task:
    """单个生成任务。source: ("sha", sha256) 或 ("file", 逻辑路径, 文件名)。"""
    source: tuple
    short_side: int
    overwrite: bool
    callback: ThumbCallback | None

    @property
    def key(self) -> tuple:
        """去重键: SHA256 (未知时用源路径) + 短边长度 + overwrite。"""
        if self.source[0] == "sha":
            ident = self.source[1]
        else:
            ident = f"path:{self.source[1]}|{self.source[2]}"
        return (ident, self.short_side, self.overwrite)

    def describe(self) -> str:
        if self.source[0] == "sha":
            return f"sha={self.source[1]} size={self.short_side} overwrite={self.overwrite}"
        return (f"file={self.source[1]}/{self.source[2]} "
                f"size={self.short_side} overwrite={self.overwrite}")


class ThumbnailGenerator:
    """缩略图生成器: 队列去重 + 固定线程池 + 生成逻辑。"""

    def __init__(self, file_manager, path_resolver, config_manager,
                 max_workers: int = 2):
        """
        :param file_manager: FileManager 实例 (全部文件 I/O 经它完成)
        :param path_resolver: PathResolver 实例 (SHA256 -> 逻辑路径查询)
        :param config_manager: ConfigManager 实例 (ThumbnailShortSideSize)
        """
        self._fm = file_manager
        self._resolver = path_resolver
        self._config = config_manager
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="thumbnail")
        self._inflight: dict[tuple, Future] = {}   # 任务键 -> Future
        self._lock = threading.Lock()
        self._shutting_down = False

    # ---------------------------------------------------------------- #
    # 提交入口 (可从任意线程调用)
    # ---------------------------------------------------------------- #

    def submit_by_sha(self, sha256: str, *, overwrite: bool = False,
                      callback: ThumbCallback | None = None) -> bool:
        """按 SHA256 提交: 经 path_resolver 选取该内容的一个副本作为源。

        :return: True=已入队, False=重复提交被忽略 (队列中/执行中) 。
        """
        sha = normalize_sha256(sha256)
        if sha is None:
            raise InputDataError(f"Invalid SHA256 format: {sha256!r}")
        return self._enqueue(
            _Task(("sha", sha), self._resolve_short_side(), overwrite, callback))

    def submit_by_file(self, logical_path: str, filename: str, *,
                       overwrite: bool = False,
                       callback: ThumbCallback | None = None) -> bool:
        """按 逻辑路径+文件名 提交; SHA256 在执行时读取文件后计算。"""
        if not isinstance(logical_path, str) or not logical_path.startswith("/"):
            raise InputDataError(f"Invalid logical path: {logical_path!r}")
        return self._enqueue(
            _Task(("file", logical_path, filename),
                  self._resolve_short_side(), overwrite, callback))

    def submit_by_folder(self, logical_path: str, *, overwrite: bool = False,
                         callback: ThumbCallback | None = None) -> int:
        """遍历逻辑目录下全部文件, 按 SHA256 去重后批量提交。

        无 SHA256 的记录 (/cache 文件、未入库文件) 跳过。
        :return: 实际入队的任务数 (去重后) 。
        """
        short_side = self._resolve_short_side()
        records = self._fm.list_files(logical_path)
        seen: set[str] = set()
        count = 0
        for r in records:
            sha = normalize_sha256(r.get("sha256"))
            if sha is None or sha in seen:
                continue  # 相同 SHA256 只生成一次
            seen.add(sha)
            if self._enqueue(_Task(("sha", sha), short_side, overwrite, callback)):
                count += 1
        log.info(f"Thumbnail batch submitted: {logical_path} "
                 f"({count} tasks from {len(records)} files)")
        return count

    def shutdown(self, wait_for_pending: bool = True) -> None:
        """停止线程池 (程序退出时调用)。

        wait_for_pending=True (默认) : 不再接受新任务, 等待队列中所有任务执行完毕;
        False: 取消尚未开始的任务, 仅等待执行中任务完成。
        """
        with self._lock:
            if self._shutting_down:
                return
            self._shutting_down = True
            futures = list(self._inflight.values())
        if wait_for_pending:
            log.info(f"ThumbnailGenerator draining {len(futures)} tasks")
            for future in futures:
                try:
                    future.result()
                except Exception:  # noqa: BLE001 任务内部已记录日志
                    pass
        else:
            for future in futures:
                future.cancel()
        with self._lock:
            # 被取消(或已完成)的任务不会继续占用任务键
            for key, future in list(self._inflight.items()):
                if future.done():
                    self._inflight.pop(key, None)
        self._executor.shutdown(wait=True)
        log.info("ThumbnailGenerator shut down")

    # ---------------------------------------------------------------- #
    # 内部: 入队与执行
    # ---------------------------------------------------------------- #

    def _resolve_short_side(self) -> int:
        """从配置读取短边长度 (一切配置获取经 config_manager) 。"""
        value = self._config.get_config("ThumbnailShortSideSize")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InputDataError(
                f"Invalid ThumbnailShortSideSize config: {value!r}")
        return value

    def _enqueue(self, task: _Task) -> bool:
        with self._lock:
            if self._shutting_down:
                return False
            if task.key in self._inflight:
                log.debug(f"Duplicate thumbnail task ignored: {task.describe()}")
                return False
            future = self._executor.submit(self._worker, task)
            self._inflight[task.key] = future
        log.debug(f"Thumbnail task submitted: {task.describe()}")
        return True

    def _worker(self, task: _Task) -> None:
        """工作线程主体: 单任务失败只回调错误, 不得让线程退出。"""
        if task.overwrite:
            log.info(f"Thumbnail overwrite requested: {task.describe()}")
        log.info(f"Thumbnail generation started: {task.describe()}")
        try:
            logical = self._generate(task)
        except Exception as e:  # noqa: BLE001 源缺失/损坏/非图片/无权限等
            log.warning(f"Thumbnail generation failed: {task.describe()} ({e})")
            self._notify(task.callback, None, str(e))
        else:
            log.info(f"Thumbnail generated: {logical}")
            self._notify(task.callback, logical, None)
        finally:
            with self._lock:
                self._inflight.pop(task.key, None)

    @staticmethod
    def _notify(callback: ThumbCallback | None,
                result: str | None, error: str | None) -> None:
        """回调在生成线程中执行; 回调自身异常被捕获, 不影响线程存活。"""
        if callback is None:
            return
        try:
            callback(result, error)
        except Exception as e:  # noqa: BLE001
            log.error(f"Thumbnail callback raised: {e}", exc_info=True)

    # ---------------------------------------------------------------- #
    # 内部: 单任务生成逻辑
    # ---------------------------------------------------------------- #

    def _generate(self, task: _Task) -> str:
        """执行单个任务, 返回缩略图逻辑路径; 失败抛异常 (由 _worker 统一回调) 。"""
        size = task.short_side

        # 1) 源文件定位: sha 源经 path_resolver 选副本; 文件源直接用给定路径
        sha: str | None = None
        if task.source[0] == "sha":
            sha = task.source[1]
        thumb_dir = f"/cache/image/{sha}" if sha else None
        thumb_name = f"{size}.webp" if sha else None

        # 2) 已存在检查 (sha 已知时提前跳过, 免读源文件)
        if sha is not None and not task.overwrite \
                and self._fm.file_exists(thumb_dir, thumb_name):
            log.debug(f"Thumbnail already exists, skipped: {thumb_dir}/{thumb_name}")
            return f"{thumb_dir}/{thumb_name}"

        # 3) 读取源文件 (sha 源按副本顺序尝试, 全部失败才报错)
        data = self._read_source(task, sha)

        # 4) 文件源的 SHA256 在读入内存后计算 (纯内存哈希, 不落盘)
        if sha is None:
            sha = hashlib.sha256(data).hexdigest()
            thumb_dir = f"/cache/image/{sha}"
            thumb_name = f"{size}.webp"
            if not task.overwrite and self._fm.file_exists(thumb_dir, thumb_name):
                log.debug(f"Thumbnail already exists, skipped: {thumb_dir}/{thumb_name}")
                return f"{thumb_dir}/{thumb_name}"
        thumb_logical = f"{thumb_dir}/{thumb_name}"

        # 5) PIL 校验并生成 (非法图片在此抛异常, 不写入任何内容)
        webp_bytes = self._render(data, size)

        # 6) 建目录 (已存在则跳过) + 7) 原子写入 (临时文件 + 替换, 由 FileManager 完成)
        self._ensure_thumb_dirs(sha)
        self._fm.write_file(thumb_dir, thumb_name, webp_bytes)
        return thumb_logical

    def _read_source(self, task: _Task, sha: str | None) -> bytes:
        """读取源文件全部字节。

        sha 源: 经 path_resolver 取副本列表, 选取其中一个 (不可用时顺延下一个) ;
        文件源: 直接按给定 逻辑路径+文件名 读取。
        """
        if task.source[0] != "sha":
            parent, filename = task.source[1], task.source[2]
            stream = self._fm.read_file(parent, filename)
            try:
                return stream.read()
            finally:
                stream.close()

        entries = self._resolver.sha_to_files(task.source[1]).get("content") or []
        if not entries:
            raise FileManagerError(
                f"No file found for SHA256: {task.source[1]}")
        last_error: Exception | None = None
        for pick in entries:
            # resolver 契约为不带 /gallery 前缀的完整逻辑路径, 此处仅拼接前缀
            parent = GALLERY + pick["logical_path"]
            try:
                stream = self._fm.read_file(parent, pick["file_name"])
            except Exception as e:  # noqa: BLE001 副本不可用时尝试下一个
                last_error = e
                continue
            try:
                return stream.read()
            finally:
                stream.close()
        raise FileManagerError(
            f"All copies of SHA256 are unreadable: {task.source[1]} ({last_error})")

    def _ensure_thumb_dirs(self, sha: str) -> None:
        """确保 /cache/image/{sha} 目录存在 (已存在则跳过) 。"""
        try:
            self._fm.create_folder("/cache", "image")
        except FileManagerError:
            pass  # 已存在
        try:
            self._fm.create_folder("/cache/image", sha)
        except FileManagerError:
            pass  # 已存在

    @staticmethod
    def _render(data: bytes, short_side: int) -> bytes:
        """用 PIL 校验图片合法性并生成 WebP 字节 (纯内存, 无文件 I/O) 。"""
        try:
            img = Image.open(io.BytesIO(data))
            img.load()  # 强制解码: 损坏/截断文件在此抛异常
        except (UnidentifiedImageError, OSError) as e:
            raise FileManagerError(f"Not a valid image: {e}") from e

        if img.mode not in ("RGB", "RGBA"):
            # WebP 不支持 P/CMYK 等模式, 含透明信息的转 RGBA, 其余转 RGB
            if img.mode in ("P", "LA", "PA") or "transparency" in img.info:
                img = img.convert("RGBA")
            else:
                img = img.convert("RGB")

        width, height = img.size
        if width <= 0 or height <= 0:
            raise FileManagerError(f"Invalid image size: {width}x{height}")
        if min(width, height) <= short_side:
            # 短边已达目标: 不放大, 按原尺寸转存
            out = img
        else:
            scale = short_side / min(width, height)
            target = (max(1, round(width * scale)), max(1, round(height * scale)))
            out = img.resize(target, Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        out.save(buf, format="WEBP")
        return buf.getvalue()
