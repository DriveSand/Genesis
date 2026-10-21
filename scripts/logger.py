import logging
import os
import sys
from folder_paths import FolderPath

LOG_FILE_NAME = "genesis.log"

_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

# 命令行字符串 -> 文件日志等级
_LEVEL_FROM_STRING = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}

# 控制台颜色
_RESET = "\033[0m"
_LEVEL_COLORS = {
    logging.DEBUG: "\033[37m",       # 白色
    logging.INFO: "\033[32m",        # 绿色
    logging.WARNING: "\033[93m",     # 浅黄色
    logging.ERROR: "\033[31m",       # 红色
    logging.CRITICAL: "\033[31;1m",  # 红色 (加粗) 
}


def _enable_windows_ansi() -> None:
    """在 Windows 终端开启 ANSI 转义序列支持。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        std_output_handle = -11
        enable_virtual_terminal_processing = 0x0004
        handle = kernel32.GetStdHandle(std_output_handle)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(
                handle, mode.value | enable_virtual_terminal_processing
            )
    except Exception:
        # 非控制台环境 (如 IDE 捕获输出) 下忽略即可
        pass


# 模块名解析: scripts/database/service.py -> database.service
_BASE_PATH = os.path.normcase(os.path.abspath(str(FolderPath.BASE_PATH)))
_SCRIPTS_PREFIX = os.path.join(_BASE_PATH, "scripts") + os.sep


def _qualified_module(record: logging.LogRecord) -> str:
    """根据调用方文件路径生成限定模块名; 项目外的模块回退为 module。"""
    try:
        path = os.path.normcase(os.path.abspath(record.pathname))
        if path.startswith(_SCRIPTS_PREFIX):
            rel = path[len(_SCRIPTS_PREFIX):]
        elif path.startswith(_BASE_PATH + os.sep):
            rel = path[len(_BASE_PATH) + 1:]
        else:
            return record.module
        parts = rel.replace(os.sep, ".").split(".")
        parts = parts[:-1]  # 去掉 .py
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts) or record.module
    except Exception:  # noqa: BLE001 日志格式化不应抛出异常
        return record.module


class _ModuleNameFilter(logging.Filter):
    """非侵入式: 仅改写 LogRecord.module, 供两个 Formatter 统一使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.module = _qualified_module(record)
        return True


# 格式化器
class _ConsoleFormatter(logging.Formatter):
    """控制台格式：[等级] [模块] 内容，等级带颜色。"""

    def format(self, record: logging.LogRecord) -> str:
        levelname = _LEVEL_NAMES.get(record.levelno, record.levelname)
        color = _LEVEL_COLORS.get(record.levelno, "")
        level_text = f"{color}{levelname}{_RESET}" if color else levelname

        line = f"[{level_text}] [{record.module}] {record.getMessage()}"
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + record.exc_text
        return line


class _FileFormatter(logging.Formatter):
    """日志文件格式：[yyyy/mm/dd HH:MM:ss] [等级] [模块] 内容。"""

    def __init__(self) -> None:
        super().__init__(
            fmt="[%(asctime)s] [%(levelname)s] [%(module)s] %(message)s",
            datefmt="%Y/%m/%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        record.levelname = _LEVEL_NAMES.get(record.levelno, record.levelname)
        return super().format(record)


# 自定义 Logger：自动识别业务调用方模块
class _GenesisLogger(logging.Logger):
    """重写 findCaller，额外跳过本模块，使 %(module)s 指向真正的调用方。"""

    _own_file = os.path.normcase(os.path.abspath(__file__))

    def findCaller(self, stack_info=False, stacklevel=1):
        f = logging.currentframe()
        if f is not None:
            f = f.f_back
        while f and stacklevel > 1:
            f = f.f_back
            stacklevel -= 1

        rv = "(unknown file)", 0, "(unknown function)", None
        while f is not None and hasattr(f, "f_code"):
            co = f.f_code
            filename = os.path.normcase(co.co_filename)
            if filename == logging._srcfile or filename == self._own_file:
                f = f.f_back
                continue
            rv = (co.co_filename, f.f_lineno, co.co_name, None)
            break
        return rv


# 全局 logger 与初始化
log = _GenesisLogger("genesis")
log.setLevel(logging.DEBUG)
log.propagate = False

_configured = False


def setup_logging(log_level: str = "warn") -> None:
    """初始化日志模块 (在 main.py 启动时调用一次) 。

    :param log_level: 记录到日志文件的最低等级 (debug/info/warn/error/critical) 。
        控制台始终输出所有等级。
    """
    global _configured

    # 支持重复调用：移除旧的 handler，避免重复输出
    if _configured:
        for handler in log.handlers[:]:
            log.removeHandler(handler)
            handler.close()

    _enable_windows_ansi()

    file_level = _LEVEL_FROM_STRING.get(
        (log_level or "warn").strip().lower(), logging.WARNING
    )

    log_file = FolderPath.BASE_PATH / LOG_FILE_NAME
    # 每次启动清空日志文件
    with open(log_file, "w", encoding="utf-8"):
        pass

    # 文件日志：按 --log_level 过滤
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setLevel(file_level)
    file_handler.setFormatter(_FileFormatter())
    file_handler.addFilter(_ModuleNameFilter())
    log.addHandler(file_handler)

    # 控制台日志：输出所有等级
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(_ConsoleFormatter())
    console_handler.addFilter(_ModuleNameFilter())
    log.addHandler(console_handler)

    _configured = True
