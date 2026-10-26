"""图片: 元数据/模型查询与修改、缩略图、原图。"""
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from exceptions import InputDataError, PathNotFoundError
from scripts.sha256 import normalize_sha256
from scripts.web_backend.helpers import (
    iter_stream,
    join_rel,
    locate_file,
    split_image_path,
)
from scripts.web_backend.schemas import MetadataUpdateRequest

router = APIRouter(prefix="/api/v1/images", tags=["images"])

_RATINGS = (0, 1, 2, 3, 4)
_INFO_TEXT_FIELDS = ("positive_prompt", "negative_prompt", "tool", "sampler",
                     "scheduler", "note")
_INFO_NUMBER_FIELDS = ("seed", "steps", "cfg", "rating")


def _simple_models(rows: list[dict]) -> list[dict]:
    return [{"model_name": r.get("name", ""),
             "version": r.get("version", ""),
             "model_url": r.get("url") or ""} for r in rows]


def _lora_models(rows: list[dict]) -> list[dict]:
    return [{"model_name": r.get("name", ""),
             "version": r.get("version", ""),
             "model_url": r.get("url") or "",
             "weight": float(r.get("weight") or 0.0),
             "type": int(r.get("model_type") or 0)} for r in rows]


@router.get("/model-names")
def model_names(request: Request, kind: str, limit: int = 500) -> dict:
    """某种模型在图库中出现过的去重名称 (供编辑界面联想) 。"""
    state = request.app.state.genesis
    if kind not in ("checkpoint", "lora", "embedding", "vae", "upscale"):
        raise InputDataError(f"Invalid model kind: {kind}")
    rows = state.db.list_model_names(kind)
    return {"kind": kind,
            "names": [r["name"] for r in rows[:max(1, min(limit, 2000))]]}


@router.get("/models")
def models(request: Request, image_path: str) -> dict:
    """获取图片使用的模型 (checkpoint / lora / embedding / vae / upscale) 。"""
    state = request.app.state.genesis
    loc = locate_file(state, image_path)
    grouped = state.db.get_file_models(loc["path_uuid"], loc["filename"])
    return {
        "checkpoint": _simple_models(grouped.get("checkpoint", [])),
        "lora": _lora_models(grouped.get("lora", [])),
        "embedding": _simple_models(grouped.get("embedding", [])),
        "vae": _simple_models(grouped.get("vae", [])),
        "upscale": _simple_models(grouped.get("upscale", [])),
    }


@router.get("/metadata")
def metadata(request: Request, image_path: str) -> dict:
    """获取图片生成信息 (info 表字段 + file 表字段) 。"""
    state = request.app.state.genesis
    loc = locate_file(state, image_path)
    info = loc["info"] or {}
    row = loc["file"]
    return {
        "positive_prompt": info.get("positive_prompt", ""),
        "negative_prompt": info.get("negative_prompt", ""),
        "seed": int(info.get("seed") or 0),
        "steps": int(info.get("steps") or 0),
        "cfg": float(info.get("cfg") or 0.0),
        "sampler": info.get("sampler", ""),
        "scheduler": info.get("scheduler", ""),
        "tool": info.get("tool", ""),
        "rating": int(info.get("rating") or 0),
        "note": info.get("note", ""),
        "width": int(row.get("width") or 0),
        "height": int(row.get("height") or 0),
        "file_size": int(row.get("file_size") or 0),
        "file_name": row["filename"],
        "added_at": int(row.get("added_at") or 0),
        "sha256": info.get("sha256", ""),
        "mime_type": row.get("mime_type", ""),
    }


@router.post("/metadata/refresh")
def refresh_metadata(request: Request, image_path: str) -> dict:
    """重新读取图片元数据并写回数据库 (右键菜单: 重新读取元数据) 。

    分辨率与文件大小总会刷新; 图中没有生成元数据时 has_metadata 为 false。
    """
    state = request.app.state.genesis
    parent, filename = split_image_path(image_path)
    meta = state.fm.reparse_metadata(parent, filename)
    return {"ok": True, "has_metadata": bool(meta),
            "tool": str(meta.get("tool") or "")}


@router.post("/metadata/update")
def update_metadata(request: Request, body: MetadataUpdateRequest) -> dict:
    """修改图片元数据 (只修改数据库, 不改动图片本身) 。

    change 中未出现的字段保持不变; 模型数组整体替换该类模型。
    """
    state = request.app.state.genesis
    loc = locate_file(state, join_rel(body.target.path, body.target.name))
    change = body.change.model_dump(exclude_unset=True)

    updates: dict = {}
    for key in _INFO_TEXT_FIELDS:
        if change.get(key) is not None:
            updates[key] = str(change[key])
    for key in _INFO_NUMBER_FIELDS:
        if change.get(key) is not None:
            updates[key] = change[key]
    if "rating" in updates and int(updates["rating"]) not in _RATINGS:
        raise InputDataError(f"Invalid rating: {updates['rating']}")
    if "seed" in updates:
        updates["seed"] = int(updates["seed"])
    if "steps" in updates:
        updates["steps"] = int(updates["steps"])
        if updates["steps"] < 0:
            raise InputDataError("Steps cannot be negative")
    if "cfg" in updates:
        updates["cfg"] = float(updates["cfg"])
    if updates:
        state.db.update_info(loc["info_id"], **updates)

    model_updates: dict = {}
    if change.get("checkpoint") is not None:
        model_updates["checkpoints"] = [
            {"name": m["model_name"], "version": m.get("version", ""),
             "url": m.get("model_url")} for m in change["checkpoint"]]
    if change.get("embedding") is not None:
        model_updates["embeddings"] = [
            {"name": m["model_name"], "version": m.get("version", ""),
             "url": m.get("model_url")} for m in change["embedding"]]
    if change.get("vae") is not None:
        model_updates["vaes"] = [
            {"name": m["model_name"], "version": m.get("version", ""),
             "url": m.get("model_url")} for m in change["vae"]]
    if change.get("upscale") is not None:
        model_updates["upscales"] = [
            {"name": m["model_name"], "version": m.get("version", ""),
             "url": m.get("model_url")} for m in change["upscale"]]
    if change.get("lora") is not None:
        loras = []
        for m in change["lora"]:
            if m.get("weight") is None:
                raise InputDataError(
                    f"LoRA entry is missing weight: {m.get('model_name')}")
            loras.append({"name": m["model_name"],
                          "version": m.get("version", ""),
                          "url": m.get("model_url"),
                          "weight": float(m["weight"]),
                          "model_type": int(m.get("type", 0))})
        model_updates["loras"] = loras
    if model_updates:
        state.db.update_file_models(loc["path_uuid"], loc["filename"],
                                    **model_updates)
    return {"ok": True}


@router.get("/thumbnail")
def thumbnail(request: Request, image_sha256: str) -> StreamingResponse:
    """获取缩略图 (WebP) ; 缓存不存在时按需生成并等待完成。"""
    state = request.app.state.genesis
    sha = normalize_sha256(image_sha256)
    if sha is None:
        raise InputDataError(f"Invalid SHA256 format: {image_sha256!r}")
    short_side = int(state.config.get_config("ThumbnailShortSideSize"))
    parent = f"/cache/image/{sha}"
    name = f"{short_side}.webp"

    if not state.fm.file_exists(parent, name):
        # 去重提交: 同一 SHA256 已在队列中时返回 False, 此处统一轮询等结果
        state.thumb.submit_by_sha(sha)
        deadline = time.time() + 30
        while time.time() < deadline:
            if state.fm.file_exists(parent, name):
                break
            time.sleep(0.1)

    if not state.fm.file_exists(parent, name):
        raise PathNotFoundError(
            f"Thumbnail is not available for SHA256: {sha}")
    stream = state.fm.read_file(parent, name)
    return StreamingResponse(
        iter_stream(stream), media_type="image/webp",
        headers={"Cache-Control": "public, max-age=86400"})


@router.post("/thumbnail/regenerate")
def regenerate_thumbnail(request: Request, image_sha256: str) -> dict:
    """重新生成单张缩略图 (覆盖旧文件, 后台执行) 。"""
    state = request.app.state.genesis
    sha = normalize_sha256(image_sha256)
    if sha is None:
        raise InputDataError(f"Invalid SHA256 format: {image_sha256!r}")
    submitted = state.thumb.submit_by_sha(sha, overwrite=True)
    return {"ok": True, "submitted": bool(submitted)}


@router.get("/original")
def original(request: Request, image_path: str) -> StreamingResponse:
    """获取原图。"""
    state = request.app.state.genesis
    loc = locate_file(state, image_path)
    stream = state.fm.read_file(loc["parent"], loc["filename"])
    mime = loc["file"].get("mime_type") or "application/octet-stream"
    return StreamingResponse(
        iter_stream(stream), media_type=mime,
        headers={"Cache-Control": "public, max-age=3600"})
