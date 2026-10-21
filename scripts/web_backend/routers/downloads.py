"""下载: 单个文件直出; 多文件/目录流式打包为仅存储 ZIP。"""
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from zipstream import ZipStream

from exceptions import FileManagerError
from scripts.logger import log
from scripts.path_resolver import validate_name
from scripts.web_backend.helpers import (
    gallery_full,
    iter_stream,
    join_rel,
    resolve_dir_uuid,
)
from scripts.web_backend.schemas import DownloadBatchRequest

router = APIRouter(prefix="/api/v1/downloads", tags=["downloads"])


def _attachment(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"


@router.get("/single")
def single(request: Request, name: str, path: str = "/") -> StreamingResponse:
    """下载单个文件; 多个文件或包含目录请使用批量接口。"""
    state = request.app.state.genesis
    validate_name(name)
    parent = gallery_full(path)
    stream = state.fm.read_file(parent, name)
    return StreamingResponse(iter_stream(stream),
                             media_type="application/octet-stream",
                             headers={"Content-Disposition": _attachment(name)})


def _walk_into_zip(state, zs: ZipStream, uuid: str, rel: str,
                   arc_prefix: str) -> None:
    """把一个目录子树下的全部文件加入 ZIP (原路径保存) 。"""
    for row in state.db.list_files_by_path(uuid):
        parent = gallery_full(rel)
        filename = row["filename"]
        st = state.fm.entry_stat(parent, filename)
        stream = state.fm.read_file(parent, filename)
        zs.add(iter_stream(stream), f"{arc_prefix}/{filename}",
               size=st["size"] if st else None)
    for child in state.db.list_paths(uuid):
        _walk_into_zip(state, zs, child["uuid"], join_rel(rel, child["name"]),
                       f"{arc_prefix}/{child['name']}")


@router.post("/batch")
def batch(request: Request, body: DownloadBatchRequest) -> StreamingResponse:
    """批量下载: 目录按原路径入包, 根级文件散放 (重名自动追加 (N)) 。"""
    state = request.app.state.genesis
    zs = ZipStream()
    used: set[str] = set()

    def _unique(name: str) -> str:
        if name not in used:
            used.add(name)
            return name
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        index = 1
        while True:
            candidate = f"{stem} ({index})" + (f".{ext}" if dot else "")
            if candidate not in used:
                used.add(candidate)
                return candidate
            index += 1

    for folder in body.folders:
        validate_name(folder.name)
        rel = join_rel(folder.path, folder.name)
        try:
            uuid, _ = resolve_dir_uuid(state, rel)
        except Exception as exc:  # noqa: BLE001 缺失目录跳过, 不影响其余内容
            log.warning(f"Download folder skipped: {rel} ({exc})")
            continue
        _walk_into_zip(state, zs, uuid, rel, _unique(folder.name))

    for item in body.files:
        validate_name(item.name)
        try:
            stream = state.fm.read_file(gallery_full(item.path), item.name)
        except FileManagerError as exc:
            log.warning(f"Download file skipped: {item.path}/{item.name} "
                        f"({exc})")
            continue
        st = state.fm.entry_stat(gallery_full(item.path), item.name)
        zs.add(iter_stream(stream), _unique(item.name),
               size=st["size"] if st else None)

    return StreamingResponse(
        zs, media_type="application/zip",
        headers={"Content-Disposition": _attachment("genesis_images.zip")})
