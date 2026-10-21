"""文件上传: 分块上传 + 秒传 (SHA256 已存在时后端直接复制副本) 。"""
import asyncio
import shutil
import threading
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile

from exceptions import (
    FileManagerError,
    InputDataError,
    InvalidPathError,
    PathNotFoundError,
)
from scripts.logger import log
from scripts.path_resolver import validate_name
from scripts.sha256 import normalize_sha256
from scripts.web_backend.helpers import gallery_full, join_rel, rel_of, resolve_dir_uuid
from scripts.web_backend.schemas import FolderCheckRequest, PrepareUploadRequest

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

CHUNK_SIZE = 10 * 1024 * 1024      # 分块最大字节数
_COPY_BUFFER = 1024 * 1024         # 流复制缓冲区
_UPLOAD_TIMEOUT = 600              # 等待移动/复制完成的最长秒数


# ---------------------------------------------------------------------- #
# 秒传: 从已有副本复制一份
# ---------------------------------------------------------------------- #

def _copy_into_upload(state, source_parent: str, source_name: str,
                      item, sha: str, target_full: str,
                      destination: str) -> None:
    """把已有文件流式复制到上传临时文件并提交移动, 失败抛 FileManagerError。"""
    tmp_name, out = state.fm.open_upload_file(item.name, target_full)
    ok = False
    try:
        src = state.fm.read_file(source_parent, source_name)
        try:
            shutil.copyfileobj(src, out, _COPY_BUFFER)
        finally:
            src.close()
        out.close()

        event = threading.Event()
        result: dict[str, Any] = {}

        def _done(path: str | None, error: str | None) -> None:
            result["path"], result["error"] = path, error
            event.set()

        state.fm.upload_move(tmp_name, target_full, sha,
                             destination=destination, callback=_done)
        if not event.wait(_UPLOAD_TIMEOUT):
            raise FileManagerError("Instant upload timed out")
        if result.get("error"):
            raise FileManagerError(f"Instant upload failed: {result['error']}")
        ok = True
    finally:
        try:
            out.close()
        except OSError:
            pass
        if not ok:
            state.fm.discard_upload(tmp_name)


def _try_instant_upload(state, item, sha: str, target_full: str,
                        destination: str) -> bool:
    """尝试秒传: 同一 SHA256 已有可读副本时直接复制, 成功返回 True。"""
    info = state.db.get_info_by_sha256(sha)
    if info is None:
        return False
    for row in state.db.get_files_by_info(int(info["id"])):
        directory = row.get("directory")
        if not directory:
            continue
        source_parent = gallery_full(directory)
        try:
            if not state.fm.file_exists(source_parent, row["filename"]):
                continue
            _copy_into_upload(state, source_parent, row["filename"], item,
                              sha, target_full, destination)
            log.info(f"Instant upload succeeded: {item.name} <- "
                     f"{directory}/{row['filename']}")
            return True
        except FileManagerError as exc:
            log.warning(f"Instant upload copy failed, fallback to upload: "
                        f"{directory}/{row['filename']} ({exc})")
    return False


# ---------------------------------------------------------------------- #
# 准备上传
# ---------------------------------------------------------------------- #

@router.post("/check-folders")
def check_folders(request: Request, body: FolderCheckRequest) -> dict:
    """检查目标目录下哪些相对文件夹路径已存在 (树状上传冲突处理)。"""
    state = request.app.state.genesis
    existing: list[str] = []
    for raw in body.paths:
        rel = str(raw or "").strip("/")
        if not rel:
            continue
        segments = [seg for seg in rel.split("/") if seg]
        try:
            for seg in segments:
                validate_name(seg)
            resolve_dir_uuid(state, join_rel(body.target, rel))
        except (InvalidPathError, PathNotFoundError):
            continue
        existing.append(rel)
    return {"existing": existing}


@router.post("/prepare")
def prepare(request: Request, body: PrepareUploadRequest) -> dict:
    """登记本次上传任务, 逐文件返回是否需要真实上传 (duplicate 秒传判定) 。"""
    state = request.app.state.genesis
    state.purge_stale_uploads()
    target_rel = body.target if body.target.startswith("/") else \
        "/" + body.target
    target_full = gallery_full(target_rel)
    resolve_dir_uuid(state, target_rel)  # 目标目录必须存在
    destination = body.destination or "mount"

    task_id = "uptk-" + str(uuid.uuid4())
    entries: dict[str, dict[str, Any]] = {}
    results = []
    for item in body.files:
        validate_name(item.name)
        sha = normalize_sha256(item.sha256)
        if sha is None:
            raise InputDataError(
                f"Invalid SHA256 format for {item.name!r}: {item.sha256!r}")
        if _try_instant_upload(state, item, sha, target_full, destination):
            results.append({"name": item.name, "duplicate": True})
            continue
        token = "fstk-" + str(uuid.uuid4())
        entries[token] = {"name": item.name, "mime": item.mime,
                          "sha256": sha, "size": int(item.size),
                          "chunks": {}, "finalizing": False}
        results.append({"name": item.name, "duplicate": False, "token": token})

    with state.upload_lock:
        state.uploads[task_id] = {"target": target_full,
                                  "created": int(time.time()),
                                  "destination": destination,
                                  "files": entries}
    return {"task_id": task_id, "chunk_size": CHUNK_SIZE, "results": results}


# ---------------------------------------------------------------------- #
# 正式上传 (逐块)
# ---------------------------------------------------------------------- #

def _write_chunk(state, original_name: str, target_full: str,
                 data: bytes) -> str:
    tmp_name, stream = state.fm.open_upload_file(original_name, target_full)
    try:
        stream.write(data)
    except OSError:
        stream.close()
        state.fm.discard_upload(tmp_name)
        raise
    finally:
        try:
            stream.close()
        except OSError:
            pass
    return tmp_name


def _finalize(state, task: dict, entry: dict) -> dict[str, Any]:
    """合并分块 -> 校验 SHA256 -> 移动到目标目录; 返回 {"path"} 或 {"error"}。"""
    target_full = task["target"]
    destination = task.get("destination") or "mount"
    chunks = dict(entry["chunks"])
    merged_name: str | None = None
    abandoned = False  # 移动任务超时未完成时, 临时文件所有权仍归上传线程
    try:
        merged_name, out = state.fm.open_upload_file(entry["name"], target_full)
        try:
            for index in range(len(chunks)):
                chunk_name = chunks.get(index)
                if chunk_name is None:
                    raise FileManagerError(f"Missing chunk: {index}")
                src = state.fm.read_file("/cache/upload", chunk_name)
                try:
                    shutil.copyfileobj(src, out, _COPY_BUFFER)
                finally:
                    src.close()
        finally:
            out.close()

        event = threading.Event()
        result: dict[str, Any] = {}

        def _done(path: str | None, error: str | None) -> None:
            result["path"], result["error"] = path, error
            event.set()

        state.fm.upload_move(merged_name, target_full, entry["sha256"],
                             destination=destination, callback=_done)
        if not event.wait(_UPLOAD_TIMEOUT):
            abandoned = True
            return {"error": "Upload finalization timed out"}
        if result.get("error"):
            return {"error": result["error"]}
        return {"path": result.get("path")}
    except FileManagerError as exc:
        return {"error": str(exc)}
    finally:
        for chunk_name in chunks.values():
            state.fm.discard_upload(chunk_name)
        if merged_name is not None and not abandoned:
            state.fm.discard_upload(merged_name)


@router.post("/execute")
async def execute(request: Request,
                  task_id: str = Form(...),
                  token: str = Form(...),
                  chunk_index: int = Form(...),
                  total_chunks: int = Form(...),
                  file: UploadFile = File(...)) -> dict:
    """接收一个分块; 最后一块接收完毕后合并、校验并落盘。"""
    state = request.app.state.genesis
    with state.upload_lock:
        task = state.uploads.get(task_id)
        entry = task["files"].get(token) if task else None
    if task is None or entry is None:
        raise InputDataError("Unknown upload task or token")
    if total_chunks < 1 or not 0 <= chunk_index < total_chunks:
        raise InputDataError(
            f"Invalid chunk index: {chunk_index}/{total_chunks}")

    target_full = task["target"]
    data = await file.read()
    tmp_name = await asyncio.to_thread(
        _write_chunk, state, entry["name"], target_full, data)

    should_finalize = False
    with state.upload_lock:
        if not entry["finalizing"]:
            entry["chunks"][chunk_index] = tmp_name
            if len(entry["chunks"]) >= total_chunks:
                entry["finalizing"] = True
                should_finalize = True
    if not should_finalize:
        return {"token": token, "name": entry["name"],
                "saved_path": join_rel(rel_of(target_full), entry["name"]),
                "chunk_index": chunk_index, "status": "received"}

    result = await asyncio.to_thread(_finalize, state, task, entry)
    saved_path = rel_of(result["path"]) if result.get("path") else \
        join_rel(rel_of(target_full), entry["name"])
    if result.get("path"):
        with state.upload_lock:
            task["files"].pop(token, None)
            if not task["files"]:
                state.uploads.pop(task_id, None)
        log.info(f"Chunked upload finished: {saved_path}")
        return {"token": token, "name": entry["name"],
                "saved_path": saved_path, "status": "success"}
    log.warning(f"Chunked upload failed: {entry['name']} "
                f"({result.get('error')})")
    return {"token": token, "name": entry["name"],
            "saved_path": saved_path, "status": "fail",
            "error": result.get("error")}
