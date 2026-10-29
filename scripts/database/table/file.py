"""file 表操作: 文件记录的新增、查询、修改、移动与删除。

尺寸/大小等属性属于单个文件; 文件与模型的关系由 model.py 负责。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from exceptions import InputDataError
from scripts.database.table import now
from scripts.database.table.path import resolve_full_path
from scripts.logger import log

# file 表允许通过 update_file 修改的字段白名单 (尺寸/大小属于单个文件)
FILE_UPDATABLE_FIELDS = {"mime_type", "width", "height", "file_size"}


def insert_files(conn: sqlite3.Connection, info_id: int,
                 files: list[dict[str, Any]], added_at: int) -> list[int]:
    """插入文件记录, 返回新文件 id 列表 (供模型绑定使用)。"""
    file_ids: list[int] = []
    for f in files:
        path_uuid = str(f.get("path_uuid") or "")
        filename = str(f.get("filename") or "").strip()
        mime_type = str(f.get("mime_type") or "")
        if not path_uuid or not filename:
            raise InputDataError("File record is missing path_uuid or filename")
        cur = conn.execute(
            "INSERT INTO file (info_id, path_uuid, filename, mime_type, "
            "added_at, width, height, file_size) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (info_id, path_uuid, filename, mime_type, added_at,
             int(f.get("width") or 0), int(f.get("height") or 0),
             int(f.get("file_size") or 0)),
        )
        file_ids.append(int(cur.lastrowid))
    return file_ids


def add_files(conn: sqlite3.Connection, info_id: int,
              files: list[dict[str, Any]]) -> int:
    """批量追加文件记录 (info 存在性检查在同一事务内) , 返回写入条数。"""
    if not files:
        return 0
    if conn.execute(
        "SELECT 1 FROM info WHERE id = ?", (info_id,)
    ).fetchone() is None:
        raise InputDataError(f"Info record does not exist: {info_id}")
    file_ids = insert_files(conn, info_id, files, now())
    log.debug(f"Files added: info_id={info_id} count={len(file_ids)}")
    return len(file_ids)


def get_files_by_info(conn: sqlite3.Connection,
                      info_id: int) -> list[dict[str, Any]]:
    """获取 info 的全部文件记录, 附带解析后的目录路径 (directory)。"""
    rows = conn.execute(
        "SELECT id, info_id, path_uuid, filename, mime_type, added_at, "
        "width, height, file_size "
        "FROM file WHERE info_id = ? ORDER BY added_at, filename",
        (info_id,),
    )
    result = []
    dir_cache: dict[str, str | None] = {}
    for r in rows:
        item = dict(r)
        pu = item["path_uuid"]
        if pu not in dir_cache:
            dir_cache[pu] = resolve_full_path(conn, pu)
        item["directory"] = dir_cache[pu]
        result.append(item)
    return result


def list_files_by_path(conn: sqlite3.Connection,
                       path_uuid: str) -> list[dict[str, Any]]:
    """列出某路径节点下的全部文件记录。"""
    rows = conn.execute(
        "SELECT id, info_id, path_uuid, filename, mime_type, added_at, "
        "width, height, file_size "
        "FROM file WHERE path_uuid = ? ORDER BY filename",
        (path_uuid,),
    )
    directory = resolve_full_path(conn, path_uuid)
    return [{**dict(r), "directory": directory} for r in rows]


def update_file(conn: sqlite3.Connection, path_uuid: str,
                filename: str, fields: dict[str, Any]) -> bool:
    """更新单个文件记录的可修改字段 (mime_type/width/height/file_size)。"""
    invalid = sorted(set(fields) - FILE_UPDATABLE_FIELDS)
    if invalid:
        raise InputDataError(f"Fields not allowed to modify: {invalid}")
    if not fields:
        return False
    set_clause = ", ".join(f"{col} = ?" for col in fields)  # 列名来自白名单
    cur = conn.execute(
        f"UPDATE file SET {set_clause} WHERE path_uuid = ? AND filename = ?",
        [*fields.values(), path_uuid, filename],
    )
    return cur.rowcount > 0


def rename_file(conn: sqlite3.Connection, path_uuid: str,
                old_name: str, new_name: str) -> bool:
    """原地重命名文件记录 (保留 file.id, 模型关联不丢失)。"""
    cur = conn.execute(
        "UPDATE file SET filename = ? WHERE path_uuid = ? AND filename = ?",
        (new_name, path_uuid, old_name),
    )
    return cur.rowcount > 0


def relocate_file(conn: sqlite3.Connection, src_path_uuid: str,
                  old_name: str, dst_path_uuid: str,
                  new_name: str) -> bool:
    """原地移动文件记录 (保留 file.id, 模型关联不丢失)。"""
    cur = conn.execute(
        "UPDATE file SET path_uuid = ?, filename = ? "
        "WHERE path_uuid = ? AND filename = ?",
        (dst_path_uuid, new_name, src_path_uuid, old_name),
    )
    return cur.rowcount > 0


def delete_file(conn: sqlite3.Connection, path_uuid: str,
                filename: str) -> bool:
    """删除单条文件记录 (file_* 中间表按 CASCADE 联动清理)。"""
    cur = conn.execute(
        "DELETE FROM file WHERE path_uuid = ? AND filename = ?",
        (path_uuid, filename),
    )
    deleted = cur.rowcount > 0
    if deleted:
        log.debug(f"File record deleted: {path_uuid}/{filename}")
    return deleted


def delete_files_by_info(conn: sqlite3.Connection, info_id: int) -> int:
    """删除 info 名下全部文件记录 (仅删 file 表), 返回删除条数。"""
    cur = conn.execute(
        "DELETE FROM file WHERE info_id = ?", (info_id,)
    )
    deleted = cur.rowcount
    if deleted:
        log.debug(f"File records deleted: info_id={info_id} count={deleted}")
    return deleted
