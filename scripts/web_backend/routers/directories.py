"""目录浏览: 分页内容 (folders 先于 files) 与变更事件 (SSE) 。"""
import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from scripts.web_backend.helpers import (
    decode_cursor,
    dir_fingerprint,
    encode_cursor,
    file_object,
    rel_of,
    resolve_dir_uuid,
)

router = APIRouter(prefix="/api/v1/directories", tags=["directories"])


def _resume(items: list[dict], cursor: dict, same) -> int:
    """游标对象在列表中的下一位下标; 找不到时从头开始。"""
    for i, item in enumerate(items):
        if same(item):
            return i + 1
    return 0


@router.get("/contents")
def contents(request: Request, path: str = "/", max_quantity: int = 50,
             cursor: str | None = None) -> dict:
    """返回目录内容: 先返回 folders, 全部返回完后返回 files。

    max_quantity 限制本次 folders + files 的总数 (最小 10) 。
    """
    state = request.app.state.genesis
    quantity = max(10, min(int(max_quantity or 50), 500))
    cur = decode_cursor(cursor)

    dir_uuid, dir_full = resolve_dir_uuid(state, path)
    parent_rel = rel_of(dir_full)
    folders = sorted(state.db.list_paths(dir_uuid),
                     key=lambda c: c["name"].lower())
    info_cache: dict[int, dict] = {}
    files = [file_object(state, r, info_cache)
             for r in state.db.list_files_by_path(dir_uuid)]
    files.sort(key=lambda f: (-f["add_time"], f["name"].lower()))

    if cur is None:
        start_folder, start_file = 0, 0
    elif cur.get("is_file"):
        start_folder = len(folders)
        start_file = _resume(
            files, cur,
            lambda f: (f["name"] == cur.get("name")
                       and f["sha256"] == cur.get("sha256")
                       and f["path"] == cur.get("path")))
    else:
        start_folder = _resume(
            folders, cur,
            lambda c: (c["uuid"] == cur.get("uuid")
                       or c["name"] == cur.get("name")))
        start_file = 0

    remaining = quantity
    picked_folders = folders[start_folder:start_folder + remaining]
    remaining -= len(picked_folders)
    picked_files = files[start_file:start_file + remaining]

    folder_objs = []
    for child in picked_folders:
        st = state.fm.entry_stat(dir_full, child["name"])
        folder_objs.append({
            "name": child["name"],
            "uuid": child["uuid"],
            "created_time": int(st["created_time"]) if st else 0,
            "edit_time": int(st["edit_time"]) if st else 0,
            "path": parent_rel,
            "scanned_path": bool(st and st["is_dir"]),
        })

    if picked_files:
        last = picked_files[-1]
        next_cursor = encode_cursor({
            "is_file": True, "name": last["name"],
            "sha256": last["sha256"], "path": last["path"]})
    elif picked_folders:
        last_folder = picked_folders[-1]
        next_cursor = encode_cursor({
            "is_file": False, "name": last_folder["name"],
            "uuid": last_folder["uuid"], "path": parent_rel})
    else:
        next_cursor = cursor or ""

    return {"folders": folder_objs, "files": picked_files,
            "current_cursor": next_cursor, "uuid": dir_uuid}


@router.get("/scan")
async def scan(request: Request, name: str, uuid: str) -> StreamingResponse:
    """目录变更事件流 (text/event-stream) 。

    目录内容 (子目录数/文件数/时间戳) 发生变化时推送:
        event: change
        data: true
    """
    state = request.app.state.genesis

    async def event_stream():
        last: tuple | None = None
        tick = 0
        while True:
            if await request.is_disconnected():
                break
            try:
                current = await asyncio.to_thread(dir_fingerprint, state, uuid)
            except Exception:  # noqa: BLE001 目录记录被删除等: 结束事件流
                yield "event: error\ndata: false\n\n"
                break
            if last is not None and current != last:
                yield "event: change\ndata: true\n\n"
            elif tick % 5 == 0:
                yield ": keepalive\n\n"
            last = current
            tick += 1
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )
