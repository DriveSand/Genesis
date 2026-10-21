"""全局配置与缩略图缓存管理。"""
import threading
from typing import Any

from fastapi import APIRouter, Request

from exceptions import FileNotFoundError as FMFileNotFound
from exceptions import InputDataError, NameConflictError
from scripts.logger import log
from scripts.web_backend.helpers import iter_gallery_tree
from scripts.web_backend.schemas import SettingsUpdateRequest

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

_SIZES = ("ThumbnailShortSideSize", "GridCellSize")
_FLAGS = ("EnableThumbnail", "AutoRefresh", "SafeBrowsingMode")
_RATING_KEYS = ("NewlyAddedImageLevel", "BlurLevel")


def _validate_change(change: dict[str, Any]) -> None:
    for key in _SIZES:
        if key in change:
            value = change[key]
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 64 <= value <= 1024:
                raise InputDataError(f"{key} must be an integer in [64, 1024]")
    for key in _FLAGS:
        if key in change and not isinstance(change[key], bool):
            raise InputDataError(f"{key} must be a boolean")
    for key in _RATING_KEYS:
        if key in change:
            value = change[key]
            if isinstance(value, bool) or value not in (0, 1, 2, 3, 4):
                raise InputDataError(f"{key} must be an integer in [0, 4]")
    if "DatabaseIdleCheckpointMinutes" in change:
        value = change["DatabaseIdleCheckpointMinutes"]
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= 1440:
            raise InputDataError(
                "DatabaseIdleCheckpointMinutes must be an integer in [0, 1440]")
    if "ShowLevel" in change:
        levels = change["ShowLevel"]
        if (not isinstance(levels, list)
                or any(isinstance(v, bool) or v not in (0, 1, 2, 3, 4)
                       for v in levels)):
            raise InputDataError("ShowLevel must be a list of levels 0-4")


@router.get("")
def get_settings(request: Request) -> dict:
    """获取全部配置。"""
    return {"settings": dict(request.app.state.genesis.config.config)}


@router.post("/update")
def update_settings(request: Request, body: SettingsUpdateRequest) -> dict:
    """更新配置 (只发送发生改变的字段) 。"""
    state = request.app.state.genesis
    _validate_change(body.change)
    state.config.update_configs(dict(body.change))
    return {"ok": True, "settings": dict(state.config.config)}


@router.get("/thumbnail-cache")
def thumbnail_cache(request: Request) -> dict:
    """缩略图缓存占用统计。"""
    state = request.app.state.genesis
    stats = state.fm.folder_stats("/cache/image")
    return {"files": stats["files"], "bytes": stats["bytes"],
            "short_side": int(state.config.get_config(
                "ThumbnailShortSideSize"))}


@router.post("/thumbnail-cache/clear")
def clear_thumbnail_cache(request: Request) -> dict:
    """清空缩略图缓存 (目录随后重建为空) 。"""
    state = request.app.state.genesis
    try:
        state.fm.delete_folder("/cache", "image", confirm=True)
    except FMFileNotFound:
        pass
    try:
        state.fm.create_folder("/cache", "image")
    except NameConflictError:
        pass
    return {"ok": True, "files": 0, "bytes": 0}


def _collect_unique_shas(state) -> list[str]:
    """遍历路径表收集去重后的 SHA256 列表。"""
    seen: set[str] = set()
    result: list[str] = []
    for uuid, _rel in iter_gallery_tree(state):
        for row in state.db.list_files_by_path(uuid):
            info = state.db.get_info(int(row["info_id"])) or {}
            sha = info.get("sha256")
            if sha and sha not in seen:
                seen.add(sha)
                result.append(sha)
    return result


@router.post("/regenerate-thumbnails")
def regenerate_thumbnails(request: Request) -> dict:
    """重新生成全部缩略图 (后台异步执行, 立即返回任务数) 。"""
    state = request.app.state.genesis
    shas = _collect_unique_shas(state)

    def _run() -> None:
        submitted = 0
        for sha in shas:
            try:
                if state.thumb.submit_by_sha(sha, overwrite=True):
                    submitted += 1
            except Exception as exc:  # noqa: BLE001 单个失败不影响其余任务
                log.warning(f"Thumbnail regeneration failed for {sha}: {exc}")
        log.info(f"Thumbnail regeneration submitted: {submitted} tasks")

    threading.Thread(target=_run, name="thumb-regenerate", daemon=True).start()
    return {"ok": True, "files": len(shas)}
