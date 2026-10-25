"""逻辑/物理路径工具。

处理 /gallery 与 /cache 的前缀分发、文件名拆分、watchdog 忽略规则等
与文件系统状态无关的纯工具函数。
"""
from __future__ import annotations

import mimetypes
import os

from exceptions import InvalidPathError
from scripts.path_resolver import GALLERY, norm_physical

# /cache 根前缀 (与 /gallery 一样由文件管理模块负责分发)
CACHE = "/cache"

# watchdog 忽略的临时/中间文件
IGNORED_EXTS = {".tmp", ".temp", ".bak", ".swp", ".swo",
                ".partial", ".crdownload", ".download"}
IGNORED_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}


def guess_mime(filename: str) -> str:
    """用标准库 mimetypes 推断 MIME; 未知类型回退为二进制流。"""
    mime, _encoding = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def should_ignore(path: str) -> bool:
    """watchdog 忽略文件判定。"""
    base = os.path.basename(path)
    if base.startswith("~$"):
        return True
    if base in IGNORED_NAMES:
        return True
    return os.path.splitext(base)[1].lower() in IGNORED_EXTS


def split_name_ext(filename: str) -> tuple[str, str]:
    """拆分文件名与完整扩展名: photo.tar.gz -> ("photo", ".tar.gz")。"""
    dot = filename.find(".", 1)  # 跳过开头的点 (隐藏文件无扩展名)
    if dot == -1:
        return filename, ""
    return filename[:dot], filename[dot:]


def is_accessible(physical: str) -> bool:
    """检测目录是否可访问 (挂载点健康检查)。"""
    try:
        if not os.path.isdir(physical):
            return False
        os.listdir(physical)
        return True
    except (OSError, PermissionError):
        return False


def split_logical_path(logical_path: str) -> tuple[str, list[str]]:
    """拆分逻辑路径 -> ("gallery"|"cache", 段列表)。非法时抛 InvalidPathError。

    两个根都拒绝 "." / ".." 段, 防止路径穿越 (如 /cache/../x) 。
    """
    if not isinstance(logical_path, str) or not logical_path.startswith("/"):
        raise InvalidPathError(
            f"Invalid logical path (must start with /gallery or /cache): "
            f"{logical_path!r}")  # 非法路径: 必须以 /gallery 或 /cache 开头
    lp = logical_path.rstrip("/") or "/"
    for prefix, kind in ((GALLERY, "gallery"), (CACHE, "cache")):
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


def gallery_segments(logical_root: str, rel: str) -> list[str] | None:
    """(逻辑根, 相对段串) -> /gallery 下的段列表; 非 gallery 根返回 None。"""
    if not logical_root.startswith(GALLERY):
        return None  # /cache 不监控, 理论上不会出现
    base = [s for s in logical_root[len(GALLERY):].split("/") if s]
    return base + ([s for s in rel.split("/") if s] if rel else [])


__all__ = [
    "CACHE",
    "IGNORED_EXTS",
    "IGNORED_NAMES",
    "gallery_segments",
    "guess_mime",
    "is_accessible",
    "join_logical_path",
    "norm_physical",
    "should_ignore",
    "split_logical_path",
    "split_name_ext",
]
