"""文件操作: 删除 / 移动 / 重命名 / 新建文件夹。"""
from fastapi import APIRouter, Request

from exceptions import FileManagerError, NameConflictError
from scripts.path_resolver import validate_name
from scripts.web_backend.helpers import gallery_full, rel_of
from scripts.web_backend.schemas import (
    CreateFolderRequest,
    DeleteRequest,
    MoveRequest,
    RenameRequest,
)

router = APIRouter(prefix="/api/v1/files", tags=["files"])

# 源侧操作 (删除/移动) 始终作用于实际位置: 挂载目录内即操作挂载物理路径
_SOURCE_DESTINATION = "mount"


def _fail(name: str, path: str, exc: Exception) -> dict:
    return {"name": name, "path": path, "ok": False, "error": str(exc)}


@router.post("/delete")
def delete(request: Request, body: DeleteRequest) -> dict:
    """批量删除文件/文件夹 (文件夹递归删除, 等同于用户已确认) 。"""
    state = request.app.state.genesis
    results = []
    for item in body.content:
        try:
            parent = gallery_full(item.path)
            if item.type == "folder":
                state.fm.delete_folder(
                    parent, item.name, confirm=True,
                    destination=_SOURCE_DESTINATION)
            else:
                state.fm.delete_file(parent, item.name,
                                     destination=_SOURCE_DESTINATION)
            results.append({"name": item.name, "path": item.path, "ok": True})
        except FileManagerError as exc:
            results.append(_fail(item.name, item.path, exc))
    return {"results": results}


@router.post("/move")
def move(request: Request, body: MoveRequest) -> dict:
    """批量移动文件/文件夹 (跨目录, 仅 /gallery) 。"""
    state = request.app.state.genesis
    results = []
    for item in body.content:
        try:
            new_full = state.fm.move_entry(
                gallery_full(item.source_path), item.name,
                gallery_full(item.target_path),
                is_dir=(item.type == "folder"),
                destination=item.destination or _SOURCE_DESTINATION)
            results.append({"name": item.name, "path": item.source_path,
                            "ok": True, "new_path": rel_of(new_full)})
        except FileManagerError as exc:
            results.append(_fail(item.name, item.source_path, exc))
    return {"results": results}


@router.post("/copy")
def copy(request: Request, body: MoveRequest) -> dict:
    """批量复制文件/文件夹到目标目录 (仅 /gallery)。"""
    state = request.app.state.genesis
    results = []
    for item in body.content:
        try:
            new_full = state.fm.copy_entry(
                gallery_full(item.source_path), item.name,
                gallery_full(item.target_path),
                is_dir=(item.type == "folder"),
                destination=item.destination or _SOURCE_DESTINATION)
            results.append({"name": item.name, "path": item.source_path,
                            "ok": True, "new_path": rel_of(new_full)})
        except FileManagerError as exc:
            results.append(_fail(item.name, item.source_path, exc))
    return {"results": results}


@router.post("/rename")
def rename(request: Request, body: RenameRequest) -> dict:
    """重命名文件/文件夹。"""
    state = request.app.state.genesis
    validate_name(body.new_name)
    parent = gallery_full(body.path)
    if body.type == "folder":
        state.fm.rename_folder(parent, body.name, body.new_name,
                               destination=_SOURCE_DESTINATION)
    else:
        state.fm.rename_file(parent, body.name, body.new_name,
                             destination=_SOURCE_DESTINATION)
    return {"ok": True, "name": body.new_name, "path": body.path}


@router.post("/folder")
def create_folder(request: Request, body: CreateFolderRequest) -> dict:
    """在指定目录下新建文件夹; auto_rename 时同名自动追加 " (N)"。"""
    state = request.app.state.genesis
    validate_name(body.name)
    parent = gallery_full(body.path)
    destination = body.destination or "mount"
    if not body.auto_rename:
        new_full = state.fm.create_folder(parent, body.name,
                                          destination=destination)
        return {"ok": True, "name": body.name, "path": body.path,
                "new_path": rel_of(new_full)}
    for index in range(1000):
        candidate = body.name if index == 0 else f"{body.name} ({index})"
        try:
            new_full = state.fm.create_folder(parent, candidate,
                                              destination=destination)
            return {"ok": True, "name": candidate, "path": body.path,
                    "new_path": rel_of(new_full)}
        except NameConflictError:
            continue
    raise FileManagerError(
        f"Failed to find an available folder name: {body.name}")
