"""info 表操作: 生成信息的创建、查询、更新、删除与孤儿记录清理。"""
from __future__ import annotations

import sqlite3
from typing import Any

from exceptions import InputDataError
from scripts.database.table import now
from scripts.database.table.file import insert_files
from scripts.database.table.model import sync_file_models
from scripts.logger import log

# info 表允许通过 update_info 修改的字段白名单
INFO_UPDATABLE_FIELDS = {
    "sha256", "positive_prompt", "negative_prompt", "seed", "steps",
    "cfg", "sampler", "scheduler", "tool", "rating", "note",
}

RATINGS = (0, 1, 2, 3, 4)  # 0为G; 1为PG-13; 2为R; 3为X; 4为XXX

_ORPHAN_CONDITION = "NOT EXISTS (SELECT 1 FROM file WHERE file.info_id = info.id)"


def info_filter(rating: int | None, start_time: int | None,
                end_time: int | None) -> tuple[str, list[Any]]:
    """list_info / count_info 共用的 WHERE 构造。"""
    clauses: list[str] = []
    params: list[Any] = []
    if rating is not None:
        if rating not in RATINGS:
            raise InputDataError(f"Invalid rating: {rating}")
        clauses.append("rating = ?")
        params.append(rating)
    if start_time is not None:
        clauses.append("created_at >= ?")
        params.append(int(start_time))
    if end_time is not None:
        clauses.append("created_at <= ?")
        params.append(int(end_time))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def create_info(conn: sqlite3.Connection, sha256: str,
                seed: int, steps: int,
                cfg: float, sampler: str, scheduler: str, tool: str,
                rating: int, positive_prompt: str, negative_prompt: str,
                note: str, files: list[dict[str, Any]] | None,
                checkpoints: list | None, loras: list | None,
                embeddings: list | None, vaes: list | None,
                upscales: list | None = None) -> int:
    """新建 info 记录, 文件与模型记录在同一事务内写入, 返回 info_id。

    尺寸/大小属于单个文件, 由 files 元素以 width/height/file_size 提供。
    模型绑定到 files 中的每个文件 (新模型中模型不再属于 info) 。
    """
    sha256 = str(sha256).strip()
    if not sha256:
        raise InputDataError("SHA256 cannot be empty")
    if rating not in RATINGS:
        raise InputDataError(f"Invalid rating: {rating}")
    try:
        seed, steps = int(seed), int(steps)
        cfg = float(cfg)
    except (TypeError, ValueError) as e:
        raise InputDataError(f"Invalid info field types: {e}") from e

    if conn.execute(
        "SELECT 1 FROM info WHERE sha256 = ?", (sha256,)
    ).fetchone():
        raise InputDataError(
            f"A record with the same SHA256 already exists: {sha256}")

    timestamp = now()
    cur = conn.execute(
        "INSERT INTO info (sha256, positive_prompt, negative_prompt, seed, "
        "steps, cfg, sampler, scheduler, tool, rating, note, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sha256, positive_prompt, negative_prompt, seed, steps, cfg,
         sampler, scheduler, tool, rating, note, timestamp, timestamp),
    )
    info_id = int(cur.lastrowid)
    file_ids = (insert_files(conn, info_id, files, timestamp)
                if files else [])
    for kind, rows in (("checkpoint", checkpoints), ("lora", loras),
                       ("embedding", embeddings), ("vae", vaes),
                       ("upscale", upscales)):
        if not rows:
            continue
        if not file_ids:
            raise InputDataError(
                "Models cannot be attached without at least one file record")
        for file_id in file_ids:
            sync_file_models(conn, file_id, kind, rows)
    log.debug(f"Info record created: id={info_id} sha256={sha256}")
    return info_id


def get_info(conn: sqlite3.Connection, info_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM info WHERE id = ?", (info_id,)
    ).fetchone()
    return dict(row) if row else None


def get_info_by_sha256(conn: sqlite3.Connection,
                       sha256: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM info WHERE sha256 = ?", (str(sha256).strip(),)
    ).fetchone()
    return dict(row) if row else None


def list_info(conn: sqlite3.Connection, rating: int | None,
              start_time: int | None, end_time: int | None,
              limit: int, offset: int) -> list[dict[str, Any]]:
    """按条件分页列出 info 记录 (按创建时间倒序)。"""
    where, params = info_filter(rating, start_time, end_time)
    rows = conn.execute(
        f"SELECT * FROM info{where} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    )
    return [dict(r) for r in rows]


def count_info(conn: sqlite3.Connection, rating: int | None,
               start_time: int | None, end_time: int | None) -> int:
    """与 list_info 相同条件下的记录总数 (供分页使用)。"""
    where, params = info_filter(rating, start_time, end_time)
    row = conn.execute(f"SELECT COUNT(*) FROM info{where}", params).fetchone()
    return int(row[0])


def update_info(conn: sqlite3.Connection, info_id: int,
                fields: dict[str, Any]) -> bool:
    """更新 info 字段 (白名单内的列), 自动刷新 updated_at。"""
    invalid = sorted(set(fields) - INFO_UPDATABLE_FIELDS)
    if invalid:
        raise InputDataError(f"Fields not allowed to modify: {invalid}")
    if not fields:
        return False
    if "rating" in fields and fields["rating"] not in RATINGS:
        raise InputDataError(f"Invalid rating: {fields['rating']}")

    set_clause = ", ".join(f"{col} = ?" for col in fields)  # 列名来自白名单
    params = [*fields.values(), now(), info_id]
    cur = conn.execute(
        f"UPDATE info SET {set_clause}, updated_at = ? WHERE id = ?", params
    )
    return cur.rowcount > 0


def delete_info(conn: sqlite3.Connection, info_id: int) -> bool:
    """删除整条 info 记录 (file 与文件-模型中间表按 CASCADE 联动清理)。"""
    cur = conn.execute("DELETE FROM info WHERE id = ?", (info_id,))
    deleted = cur.rowcount > 0
    if deleted:
        log.debug(f"Info record deleted: id={info_id}")
    return deleted


def count_orphan_info(conn: sqlite3.Connection) -> int:
    """统计 file 表中已无任何关联文件的 info 记录数量 (供用户确认)。"""
    row = conn.execute(
        f"SELECT COUNT(*) FROM info WHERE {_ORPHAN_CONDITION}"
    ).fetchone()
    return int(row[0])


def cleanup_orphan_info(conn: sqlite3.Connection) -> int:
    """删除已无关联文件的 info 记录 (模型表经 CASCADE 联动清理), 返回删除数。"""
    cur = conn.execute(f"DELETE FROM info WHERE {_ORPHAN_CONDITION}")
    deleted = cur.rowcount
    if deleted:
        log.debug(f"Orphan info records cleaned up: count={deleted}")
    return deleted
