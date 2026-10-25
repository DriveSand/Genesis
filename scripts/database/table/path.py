"""path 表操作: 目录树的增删改查与完整逻辑路径解析。

所有函数均为纯 SQL 实现, 由 engine 的写线程或读线程池在事务/连接中调用。
"""
from __future__ import annotations

import sqlite3
import uuid as uuid_lib
from typing import Any

from exceptions import DatabaseError, InputDataError
from scripts.database.creation import ROOT_NAME
from scripts.database.table import UNSET
from scripts.logger import log

_PATH_STATUSES = ("normal", "offline", "conflict")
_PATH_TYPES = ("inbuilt", "mount")
_CONFLICT_STATES = ("active", "shadowed")


def resolve_full_path(conn: sqlite3.Connection, path_uuid: str) -> str | None:
    """沿 parent_uuid 向上回溯到根目录, 拼出完整逻辑路径 (不含 ROOT, 恒以 "/" 开头)。

    根节点返回 "/", 其余形如 "/abc/123"。
    """
    rows = conn.execute(
        """
        WITH RECURSIVE chain(uuid, parent_uuid, name, depth) AS (
            SELECT uuid, parent_uuid, name, 0 FROM path WHERE uuid = ?
            UNION ALL
            SELECT p.uuid, p.parent_uuid, p.name, c.depth + 1
            FROM path AS p JOIN chain AS c ON p.uuid = c.parent_uuid
        )
        SELECT parent_uuid, name, depth FROM chain ORDER BY depth DESC
        """,
        (path_uuid,),
    ).fetchall()
    if not rows:
        return None
    parts = [r["name"] for r in rows if r["parent_uuid"] is not None]
    return "/" + "/".join(parts)


def root_uuid(conn: sqlite3.Connection) -> str:
    """取根目录 uuid (写事务内的"先查"必须用写连接)。"""
    row = conn.execute(
        "SELECT uuid FROM path WHERE parent_uuid IS NULL AND name = ?",
        (ROOT_NAME,),
    ).fetchone()
    if row is None:
        raise DatabaseError(42110, "Root path record is missing")
    return row["uuid"]


def get_root_path(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM path WHERE parent_uuid IS NULL AND name = ?",
        (ROOT_NAME,),
    ).fetchone()
    return dict(row) if row else None


def add_path(conn: sqlite3.Connection, name: str, path_type: str,
             parent_uuid: str | None, path_uuid: str | None) -> str:
    """新建路径节点 (存在性与重名检查在同一事务内用写连接完成)。"""
    name = str(name).strip()
    if not name:
        raise InputDataError("Path name cannot be empty")
    if path_type not in _PATH_TYPES:
        raise InputDataError(f"Invalid path type: {path_type}")

    if parent_uuid is None:
        parent_uuid = root_uuid(conn)
    elif conn.execute(
        "SELECT 1 FROM path WHERE uuid = ?", (parent_uuid,)
    ).fetchone() is None:
        raise InputDataError(f"Parent path does not exist: {parent_uuid}")

    if conn.execute(
        "SELECT 1 FROM path WHERE parent_uuid = ? AND name = ?",
        (parent_uuid, name),
    ).fetchone():
        raise InputDataError(
            f"A sibling path with the same name already exists: {name}")

    new_uuid = path_uuid if path_uuid else str(uuid_lib.uuid4())
    conn.execute(
        "INSERT INTO path (uuid, parent_uuid, name, status, type) "
        "VALUES (?, ?, ?, 'normal', ?)",
        (new_uuid, parent_uuid, name, path_type),
    )
    log.debug(f"Path created: {name} ({new_uuid})")
    return new_uuid


def get_path(conn: sqlite3.Connection,
             path_uuid: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM path WHERE uuid = ?", (path_uuid,)
    ).fetchone()
    return dict(row) if row else None


def move_path(conn: sqlite3.Connection, path_uuid: str,
              parent_uuid: str, name: str) -> bool:
    """移动/重命名目录节点 (只改 parent_uuid 与 name, 子节点与文件记录不动)。"""
    if conn.execute(
        "SELECT 1 FROM path WHERE uuid = ?", (parent_uuid,)
    ).fetchone() is None:
        raise InputDataError(f"Parent path does not exist: {parent_uuid}")
    cur = conn.execute(
        "UPDATE path SET parent_uuid = ?, name = ? WHERE uuid = ?",
        (parent_uuid, str(name).strip(), path_uuid),
    )
    return cur.rowcount > 0


def list_paths(conn: sqlite3.Connection,
               parent_uuid: str | None) -> list[dict[str, Any]]:
    """列出路径节点; 给定 parent_uuid 时只返回其直接子节点。"""
    if parent_uuid is None:
        rows = conn.execute("SELECT * FROM path ORDER BY name")
    else:
        rows = conn.execute(
            "SELECT * FROM path WHERE parent_uuid = ? ORDER BY name",
            (parent_uuid,),
        )
    return [dict(r) for r in rows]


def update_path(conn: sqlite3.Connection, path_uuid: str, name: Any,
                status: Any, conflict_state: Any, last_checked: Any,
                path_type: Any = UNSET) -> bool:
    """更新路径节点字段。未传入的参数保持不变 (显式传 None 表示置空)。

    先查后改均在同一写事务内 (写连接) , 避免并发下的检查失效。
    """
    row = conn.execute(
        "SELECT * FROM path WHERE uuid = ?", (path_uuid,)
    ).fetchone()
    if row is None:
        return False
    is_root = row["parent_uuid"] is None

    updates: dict[str, Any] = {}
    if name is not UNSET:
        if is_root:
            raise InputDataError("Root path name cannot be modified")
        new_name = str(name).strip()
        if not new_name:
            raise InputDataError("Path name cannot be empty")
        updates["name"] = new_name
    if status is not UNSET:
        if is_root:
            raise InputDataError("Root path status cannot be modified")
        if status not in _PATH_STATUSES:
            raise InputDataError(f"Invalid path status: {status}")
        updates["status"] = status
    if conflict_state is not UNSET:
        updates["conflict_state"] = conflict_state
    if last_checked is not UNSET:
        updates["last_checked"] = last_checked
    if path_type is not UNSET:
        if is_root:
            raise InputDataError("Root path type cannot be modified")
        if path_type not in _PATH_TYPES:
            raise InputDataError(f"Invalid path type: {path_type}")
        if path_type == "mount" and row["type"] != "mount":
            # 只允许把空目录转换为挂载点, 防止原有内容被外部目录遮蔽
            has_child = conn.execute(
                "SELECT 1 FROM path WHERE parent_uuid = ? LIMIT 1",
                (path_uuid,)).fetchone() is not None
            has_file = conn.execute(
                "SELECT 1 FROM file WHERE path_uuid = ? LIMIT 1",
                (path_uuid,)).fetchone() is not None
            if has_child or has_file:
                raise InputDataError(
                    "Only an empty directory can be converted to a mount point")
        updates["type"] = path_type
    if not updates:
        return False

    # status 与 conflict_state 联合约束校验
    final_status = updates.get("status", row["status"])
    final_conflict = updates.get("conflict_state", row["conflict_state"])
    if final_status == "conflict":
        if final_conflict not in _CONFLICT_STATES:
            raise InputDataError(
                f"When status is 'conflict', conflict_state must be one of "
                f"{_CONFLICT_STATES}"
            )
    elif final_conflict is not None:
        raise InputDataError(
            "conflict_state can only be set when status is 'conflict'")

    # 同级重名校验
    if "name" in updates and conn.execute(
        "SELECT 1 FROM path WHERE parent_uuid = ? AND name = ? AND uuid != ?",
        (row["parent_uuid"], updates["name"], path_uuid),
    ).fetchone():
        raise InputDataError(
            f"A sibling path with the same name already exists: {updates['name']}")

    set_clause = ", ".join(f"{col} = ?" for col in updates)
    params = [*updates.values(), path_uuid]
    cur = conn.execute(f"UPDATE path SET {set_clause} WHERE uuid = ?", params)
    return cur.rowcount > 0


def delete_path(conn: sqlite3.Connection, path_uuid: str) -> bool:
    """删除空路径节点 (存在子节点或关联文件时禁止删除)。"""
    row = conn.execute(
        "SELECT * FROM path WHERE uuid = ?", (path_uuid,)
    ).fetchone()
    if row is None:
        return False
    if row["parent_uuid"] is None and row["name"] == ROOT_NAME:
        raise InputDataError("Root path cannot be deleted")
    if conn.execute(
        "SELECT 1 FROM path WHERE parent_uuid = ?", (path_uuid,)
    ).fetchone():
        raise InputDataError("Path still has child paths and cannot be deleted")
    if conn.execute(
        "SELECT 1 FROM file WHERE path_uuid = ?", (path_uuid,)
    ).fetchone():
        raise InputDataError(
            "Path still has associated file records and cannot be deleted")

    conn.execute("DELETE FROM path WHERE uuid = ?", (path_uuid,))
    log.debug(f"Path deleted: {row['name']} ({path_uuid})")
    return True


def resolve_path(conn: sqlite3.Connection, path_uuid: str) -> str | None:
    """解析路径节点的完整相对路径 (自根目录起, '/' 连接, 不含 ROOT)。"""
    return resolve_full_path(conn, path_uuid)
