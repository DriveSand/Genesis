import argparse
import os
import signal
import threading
import webbrowser

import uvicorn

from exceptions import DatabaseVersionError
from folder_paths import FolderPath
from scripts.config_manager import ConfigManager
from scripts.database import DB_VERSION, DataBase
from scripts.file_manager import FileManager
from scripts.logger import log, setup_logging
from scripts.thumbnail_gen import ThumbnailGenerator
from scripts.web_backend.app import create_app
from scripts.web_backend.helpers import iter_gallery_tree
from scripts.web_backend.state import AppState
from version import DATABASE_VERSION, PROGRAM_VERSION

DEFAULT_PORT = 5580
DEFAULT_HOST = "127.0.0.1"


def set_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--host",
        type=str,
        default=DEFAULT_HOST,
        help="Listening address"
        )
    parser.add_argument("--port",
        type=int,
        default=DEFAULT_PORT,
        help="Listening port"
        )
    parser.add_argument("--read_only",
        action="store_true",
        help="Run the program in read-only mode"
        )
    parser.add_argument("--headless_mode",
        action="store_true",
        help="Doesn't open the frontend after starting"
        )
    parser.add_argument("--log_level",
        type=str,
        default="warning",
        help="Level recorded in the log file"
        )
    parser.add_argument("--generate_video_cache",
        action="store_true",
        help="Pre-generate video thumbnails and low-resolution videos"
        )
    parser.add_argument("--generate_image_cache",
        action="store_true",
        help="Pre-generate thumbnail"
        )
    return parser


def _open_browser(host: str, port: int) -> None:
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    webbrowser.open(f"http://{host}:{port}/")


class _GenesisServer(uvicorn.Server):
    """uvicorn Server: 优雅关闭开始时通知应用层主动结束 SSE 等长连接。

    uvicorn 会在优雅关闭超时后强制取消剩余任务, 并输出 CancelledError
    堆栈。提前置位 shutdown_event 可让 SSE 生成器正常返回, 避免该噪声。
    """

    def __init__(self, config: uvicorn.Config,
                 shutdown_event: threading.Event) -> None:
        super().__init__(config)
        self._shutdown_event = shutdown_event

    def handle_exit(self, sig: int, frame) -> None:
        self._shutdown_event.set()
        super().handle_exit(sig, frame)


def _install_signal_handlers(server: uvicorn.Server,
                             shutdown_event: threading.Event) -> None:
    """为终止信号安装"请求优雅停止"的处理器。

    uvicorn 在运行期间会接管 SIGINT/SIGTERM/SIGBREAK, 并在优雅关闭后恢复
    原处理器、重新抛出捕获的信号。预先安装本处理器可避免 SIGTERM 等信号的
    默认动作在 finally 清理前直接结束进程。
    """
    def _handle(signum, _frame) -> None:
        log.info(f"Signal {signum} received, stopping FastAPI")
        shutdown_event.set()
        server.should_exit = True

    # SIGINT 保持 Python 默认处理 (KeyboardInterrupt): 启动阶段也能中断;
    # 运行阶段由 uvicorn 接管, 关闭完成后重新抛出并由下方 except 捕获
    names = ("SIGTERM", "SIGBREAK", "SIGHUP")
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handle)
        except (ValueError, OSError):
            pass  # 非主线程或平台不支持时忽略


def _install_console_close_handler(server: uvicorn.Server,
                                   done_event: threading.Event,
                                   shutdown_event: threading.Event):
    """Windows 控制台关闭/注销/关机: 触发优雅停止并尽量等待清理完成。

    Windows 在控制台关闭事件后约 5 秒强制结束进程, 因此等待上限设为 4.5 秒;
    Ctrl+C / Ctrl+Break 返回 False, 交由 Python/uvicorn 的默认处理。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001 非 Windows 或受限环境
        return None

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    close_events = (2, 5, 6)  # CTRL_CLOSE / CTRL_LOGOFF / CTRL_SHUTDOWN

    def _handle(ctrl_type: int) -> bool:
        if ctrl_type not in close_events:
            return False
        log.info("Console close event received, stopping FastAPI")
        shutdown_event.set()
        server.should_exit = True
        done_event.wait(4.5)
        return True

    callback = handler_type(_handle)
    try:
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True):
            return None
    except Exception:  # noqa: BLE001
        return None
    return callback


def _pregenerate_thumbnails(state: AppState) -> None:
    """为图库中全部图片提交缩略图生成任务 (后台执行) 。"""
    seen: set[str] = set()
    submitted = 0
    for uuid, _rel in iter_gallery_tree(state):
        for row in state.db.list_files_by_path(uuid):
            info = state.db.get_info(int(row["info_id"])) or {}
            sha = info.get("sha256")
            if not sha or sha in seen:
                continue
            seen.add(sha)
            try:
                if state.thumb.submit_by_sha(sha):
                    submitted += 1
            except Exception as exc:  # noqa: BLE001 单张失败不影响其余任务
                log.warning(f"Thumbnail pre-generation failed for {sha}: {exc}")
    log.info(f"Image cache pre-generation submitted: {submitted} tasks")


def main():
    args = set_parser().parse_args()
    setup_logging(args.log_level)

    FolderPath().create_folder()

    if DATABASE_VERSION != DB_VERSION:
        log.error(
            f"version.py DATABASE_VERSION ({DATABASE_VERSION}) does not match "
            f"the database package ({DB_VERSION}); refusing to start")
        raise SystemExit(1)
    log.info(f"Genesis {PROGRAM_VERSION} (database schema v{DATABASE_VERSION})")

    config = ConfigManager()
    try:
        # 同步完成建库/迁移 (含迁移前 WAL 回写与备份) 后, 才启动依赖数据库的服务
        db = DataBase(
            db_version=DATABASE_VERSION,
            idle_checkpoint_minutes=float(
                config.get_config("DatabaseIdleCheckpointMinutes")),
        )
    except DatabaseVersionError as exc:
        # 数据库版本高于程序记录版本: 拒绝启动并明确告知用户
        log.error(f"Refusing to start: {exc}")
        raise SystemExit(1) from exc
    fm: FileManager | None = None
    thumb: ThumbnailGenerator | None = None
    server: uvicorn.Server | None = None
    shutdown_done = threading.Event()
    try:
        mounts, skipped = config.load_external_folders(db)
        if skipped:
            log.warning(f"External folder entries skipped: {skipped}")

        fm = FileManager(
            db,
            str(FolderPath.GALLERY_ROOT),
            str(FolderPath.CACHE_FOLDER),
            "upload",
            mounts,
            read_only=args.read_only,
        )
        thumb = ThumbnailGenerator(fm, fm.resolver, config)
        state = AppState(config=config, db=db, fm=fm, thumb=thumb,
                         read_only=args.read_only,
                         program_version=PROGRAM_VERSION,
                         db_version=DATABASE_VERSION)

        if args.generate_image_cache:
            threading.Thread(target=_pregenerate_thumbnails, args=(state,),
                             name="image-cache", daemon=True).start()

        app = create_app(state)
        if not args.headless_mode:
            threading.Timer(1.5, _open_browser,
                            args=(args.host, args.port)).start()

        log.info(f"Genesis listening on http://{args.host}:{args.port}/")
        # log_config=None: uvicorn 的 dictConfig 会关闭进程内已有的全部
        # handler (包括本程序的日志文件), 这里保留现有日志配置
        uvicorn_config = uvicorn.Config(
            app, host=args.host, port=args.port, log_level="warning",
            log_config=None,
            # SSE 等长连接不会主动结束; 限时等待后强制关闭, 否则无法退出
            timeout_graceful_shutdown=3)
        server = _GenesisServer(uvicorn_config, state.shutdown_event)
        _install_signal_handlers(server, state.shutdown_event)
        # 持有回调引用, 防止被垃圾回收后控制台处理器失效
        console_handler = _install_console_close_handler(
            server, shutdown_done, state.shutdown_event)
        if console_handler is not None:
            server._genesis_console_handler = console_handler  # type: ignore[attr-defined]
        try:
            server.run()
        except KeyboardInterrupt:
            log.info("Keyboard interrupt received")
    finally:
        # 1) server.run() 返回时 FastAPI 已停止监听并结束在途请求 (含 SSE)
        if server is not None:
            log.info("FastAPI stopped")
        # 2) 放弃缩略图队列: 清空排队任务, 正在生成的任务不再等待
        if thumb is not None:
            try:
                thumb.shutdown(wait_for_pending=False)
            except Exception as exc:  # noqa: BLE001 单步失败不阻断后续清理
                log.error(f"Thumbnail shutdown failed: {exc}", exc_info=True)
        # 3) 停止目录监控与文件管理后台线程
        if fm is not None:
            try:
                fm.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.error(f"FileManager shutdown failed: {exc}", exc_info=True)
        # 4) 数据库退出 (优先保证写事务完成与连接正常关闭)
        try:
            db.close()
        except Exception as exc:  # noqa: BLE001
            log.error(f"Database shutdown failed: {exc}", exc_info=True)
        # 5) 通知控制台关闭处理器清理完成, 程序退出
        shutdown_done.set()
        log.info("Genesis stopped")


if __name__ == "__main__":
    main()
