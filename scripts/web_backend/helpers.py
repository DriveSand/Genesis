"""Web 后端通用辅助: 游标编解码、逻辑路径与数据库定位。

本模块不直接执行文件 I/O 与 SQL, 全部委托 file_manager / path_resolver /
database 模块。
"""
import base64
import json
from typing import Any

from exceptions import InputDataError, PathNotFoundError
from scripts.path_resolver import GALLERY, validate_name

# 前端可选的"文件类型" -> 扩展名集合 (小写, 不含点)
FILE_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    "jpeg": ("jpg", "jpeg"),
    "jpg": ("jpg", "jpeg"),
    "png": ("png",),
    "webp": ("webp",),
    "gif": ("gif",),
    "bmp": ("bmp",),
    "tiff": ("tif", "tiff"),
    "tif": ("tif", "tiff"),
    "mp4": ("mp4",),
    "webm": ("webm",),
    "mov": ("mov",),
    "mkv": ("mkv",),
}

# 评分等级的展示名 (0=G ... 4=XXX)
RATING_NAMES = ("G", "PG-13", "R", "X", "XXX")


# ---------------------------------------------------------------------- #
# 游标 (Base64 编码的 JSON 字符串, 前端无需解码)
# ---------------------------------------------------------------------- #

def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_cursor(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------- #
# 逻辑路径
# ---------------------------------------------------------------------- #

def gallery_full(rel: str) -> str:
    """gallery 相对逻辑路径 ("/" 或 "/test") -> 带 /gallery 前缀的完整逻辑路径。"""
    if not isinstance(rel, str) or not rel.startswith("/"):
        raise InputDataError(f"Invalid logical path (must start with '/'): {rel!r}")
    rel = rel.rstrip("/")
    return GALLERY + ("" if rel == "/" else rel)


def rel_of(full: str) -> str:
    """带 /gallery 前缀的完整逻辑路径 -> 相对逻辑路径 ("/" 或 "/test")。"""
    return full[len(GALLERY):] or "/"


def join_rel(base: str, name: str) -> str:
    """拼接相对逻辑路径: ("/", "test") -> "/test"。"""
    return (base.rstrip("/") + "/" + name) if base != "/" else "/" + name


def split_image_path(image_path: str) -> tuple[str, str]:
    """图片逻辑路径+文件名 ("/test/a.png") -> (父目录完整逻辑路径, 文件名)。"""
    if not isinstance(image_path, str) or not image_path.startswith("/"):
        raise InputDataError(
            f"Invalid image path (must start with '/'): {image_path!r}")
    parent, _, filename = image_path.rstrip("/").rpartition("/")
    validate_name(filename)
    return gallery_full(parent or "/"), filename


def type_extensions(values: list[str]) -> list[str]:
    """前端文件类型列表 -> 扩展名列表 (去重保序)。"""
    result: list[str] = []
    for value in values:
        for ext in FILE_TYPE_ALIASES.get(str(value).lower(), (str(value).lower(),)):
            if ext and ext not in result:
                result.append(ext)
    return result


# ---------------------------------------------------------------------- #
# 数据库定位
# ---------------------------------------------------------------------- #

def resolve_dir_uuid(state, rel: str) -> tuple[str, str]:
    """相对逻辑路径 -> (目录节点 uuid, 带 /gallery 前缀的完整逻辑路径)。

    路径表中不存在时抛 PathNotFoundError。
    """
    from scripts.file_manager import split_logical_path

    kind, segments = split_logical_path(gallery_full(rel))
    return state.fm.resolver.walk_segments(segments)


def locate_file(state, image_path: str) -> dict[str, Any]:
    """按 "逻辑路径+文件名" 定位文件与 info 记录。

    :return: {"parent": 完整逻辑路径, "filename": str, "file": file 行,
              "info": info 行 | None, "info_id": int, "path_uuid": str}
    文件记录不存在时抛 PathNotFoundError (404) 。
    """
    parent_full, filename = split_image_path(image_path)
    segments = parent_full[len(GALLERY):].split("/")
    segments = [s for s in segments if s]
    path_uuid, _ = state.fm.resolver.walk_segments(segments)
    row = next((r for r in state.db.list_files_by_path(path_uuid)
                if r["filename"] == filename), None)
    if row is None:
        raise PathNotFoundError(f"File record not found: {image_path}")
    info = state.db.get_info(int(row["info_id"]))
    return {"parent": parent_full, "filename": filename, "file": row,
            "info": info, "info_id": int(row["info_id"]),
            "path_uuid": path_uuid}


def file_object(state, row: dict[str, Any],
                info_cache: dict[int, dict[str, Any] | None] | None = None
                ) -> dict[str, Any]:
    """file 行 -> API 文件对象 {name, sha256, rating, file_size, add_time, path}。"""
    info_id = int(row["info_id"])
    if info_cache is not None and info_id in info_cache:
        info = info_cache[info_id]
    else:
        info = state.db.get_info(info_id)
        if info_cache is not None:
            info_cache[info_id] = info
    info = info or {}
    return {
        "name": row["filename"],
        "sha256": info.get("sha256", ""),
        "rating": int(info.get("rating", 0)),
        "file_size": int(row.get("file_size") or 0),
        "width": int(row.get("width") or 0),
        "height": int(row.get("height") or 0),
        "add_time": int(row.get("added_at", 0)),
        "path": row.get("directory") or "/",
    }


def iter_gallery_tree(state):
    """深度优先遍历 /gallery 路径表 -> (目录 uuid, 相对逻辑路径)。"""
    root = state.db.get_root_path()
    if root is None:
        return
    stack: list[tuple[str, str]] = [(root["uuid"], "/")]
    while stack:
        uuid, rel = stack.pop()
        yield uuid, rel
        for child in state.db.list_paths(uuid):
            stack.append((child["uuid"], join_rel(rel, child["name"])))


def dir_fingerprint(state, uuid: str) -> tuple:
    """目录变更指纹: (子目录数, 文件数, 最大/合计 added_at) 。"""
    folders = state.db.list_paths(uuid)
    files = state.db.list_files_by_path(uuid)
    times = [int(f.get("added_at") or 0) for f in files]
    return (len(folders), len(files), max(times) if times else 0, sum(times))


def iter_stream(stream, chunk_size: int = 256 * 1024):
    """流式读取并最终关闭文件流 (供 StreamingResponse 使用) 。"""
    try:
        while True:
            data = stream.read(chunk_size)
            if not data:
                break
            yield data
    finally:
        stream.close()
