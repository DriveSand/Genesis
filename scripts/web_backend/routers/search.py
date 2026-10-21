"""图片搜索: 基本搜索与高级搜索。"""
from fastapi import APIRouter, Request

from scripts.web_backend.helpers import (
    decode_cursor,
    encode_cursor,
    type_extensions,
)
from scripts.web_backend.schemas import AdvancedSearchRequest, BasicSearchRequest

router = APIRouter(prefix="/api/v1/search", tags=["search"])


def _clean(values: list[str]) -> list[str]:
    return [v.strip() for v in values if isinstance(v, str) and v.strip()]


def _basic_spec(body: BasicSearchRequest) -> dict:
    return {"content": _clean(body.content), "paths": _clean(body.path)}


def _advanced_spec(body: AdvancedSearchRequest) -> dict:
    return {
        "path_include": _clean(body.path.include),
        "path_exclude": _clean(body.path.exclusion),
        "name_include": _clean(body.file_name.include),
        "name_exclude": _clean(body.file_name.exclusion),
        "prompt_include": _clean(body.prompt.include),
        "prompt_exclude": _clean(body.prompt.exclusion),
        "neg_prompt_include": _clean(body.neg_prompt.include),
        "neg_prompt_exclude": _clean(body.neg_prompt.exclusion),
        "file_types": type_extensions(_clean(body.file_type)),
        "sha256": [s.strip().lower() for s in _clean(body.sha256)],
        "tool": _clean(body.tool),
        "sampler": _clean(body.sampler),
        "scheduler": _clean(body.scheduler),
        "seed": [int(s) for s in body.seed],
        "rating": [int(r) for r in body.rating],
        "note": _clean(body.note),
        "size_ranges": [(int(r.bigger_than), int(r.smaller_than))
                        for r in body.file_size],
        "width_ranges": [(int(r.bigger_than), int(r.smaller_than))
                         for r in body.width],
        "height_ranges": [(int(r.bigger_than), int(r.smaller_than))
                          for r in body.height],
        "time_ranges": [(int(r.after), int(r.before)) for r in body.add_time],
        "ckpt": [(m.name.strip(), m.ver.strip()) for m in body.ckpt
                 if m.name.strip()],
        "lora": [(m.name.strip(), m.ver.strip()) for m in body.lora
                 if m.name.strip()],
        "embed": [(m.name.strip(), m.ver.strip()) for m in body.embed
                  if m.name.strip()],
    }


def _run(state, spec: dict, max_quantity: int,
         cursor: str | None) -> dict:
    limit = max(10, min(int(max_quantity or 50), 200))
    rows = state.db.search_files(spec, limit=limit,
                                 after=decode_cursor(cursor))
    files = [{"name": r["name"], "sha256": r["sha256"],
              "rating": int(r["rating"]), "file_size": int(r["file_size"]),
              "add_time": int(r["add_time"]), "path": r["path"]}
             for r in rows]
    if rows:
        last = rows[-1]
        next_cursor = encode_cursor({"is_file": True, "name": last["name"],
                                     "sha256": last["sha256"],
                                     "path": last["path"]})
    else:
        next_cursor = cursor or ""
    return {"files": files, "current_cursor": next_cursor}


@router.post("/basic")
def basic(request: Request, body: BasicSearchRequest) -> dict:
    """基本搜索: content 匹配正向提示词或文件名, path 匹配所在目录。"""
    state = request.app.state.genesis
    return _run(state, _basic_spec(body), body.max_quantity, body.cursor)


@router.post("/advanced")
def advanced(request: Request, body: AdvancedSearchRequest) -> dict:
    """高级搜索: 各条件数组内取 OR, 条件之间取 AND。"""
    state = request.app.state.genesis
    return _run(state, _advanced_spec(body), body.max_quantity, body.cursor)
