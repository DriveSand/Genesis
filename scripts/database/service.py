"""database/service.py —— 数据库对外服务。

职责:
- 管理连接与线程: 单写线程 (唯一写连接 + FIFO 队列 + Future) 与
  读线程池 (每线程 threading.local 只读连接);
- 承载全部查询语句与读写实现 (path / info / file / model_* 表);
- 提供 DataBase 门面与模块级 init()/close() 生命周期;
- 写空闲达到配置时长后自动执行 SQLite WAL checkpoint
  (PRAGMA wal_checkpoint(TRUNCATE)), 把 WAL 内容写回主库。

建表逻辑见 creation.py, 版本检查与迁移见 upgrade.py。
"""
import os
import queue
import sqlite3
import threading
import time
import uuid as uuid_lib
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

from exceptions import (
    DatabaseClosedError,
    DatabaseError,
    InputDataError,
    SchemaInitError,
)
from folder_paths import FolderPath
from scripts.logger import log
from scripts.database.creation import DB_FILE_NAME, ROOT_NAME, open_connection
from scripts.database.upgrade import (
    DB_VERSION,
    backup_database,
    bootstrap,
    migration_needed,
)

# info 表允许通过 update_info 修改的字段白名单
_INFO_UPDATABLE_FIELDS = {
    "sha256", "positive_prompt", "negative_prompt", "seed", "steps",
    "cfg", "sampler", "scheduler", "tool", "rating", "note",
}

# file 表允许通过 update_file 修改的字段白名单 (尺寸/大小属于单个文件)
_FILE_UPDATABLE_FIELDS = {"mime_type", "width", "height", "file_size"}

# 模型种类 -> 模型表名 (表名白名单, 查询值一律走 ? 参数绑定)
_MODEL_TABLES = {
    "checkpoint": "model_checkpoint",
    "lora": "model_lora",
    "embedding": "model_embedding",
    "vae": "model_vae",
}

# 模型种类 -> (中间表, 模型外键列); LoRA 中间表另有 weight 列
_MODEL_JUNCTIONS = {
    "checkpoint": ("file_checkpoint", "checkpoint_id"),
    "lora": ("file_lora", "lora_id"),
    "embedding": ("file_embedding", "embedding_id"),
    "vae": ("file_vae", "vae_id"),
}

_PATH_STATUSES = ("normal", "offline", "conflict")
_PATH_TYPES = ("inbuilt", "mount")
_CONFLICT_STATES = ("active", "shadowed")
_LORA_MODEL_TYPES = (0, 1)  # 0=LoRA, 1=LyCORIS
_RATINGS = (0, 1, 2, 3, 4)  # 0为G; 1为PG-13; 2为R; 3为X; 4为XXX

# 读线程池大小
_READ_WORKERS = 4

_CLOSED_MSG = "Database is not initialized or already closed"

# update_path 的哨兵值: 区分「未传」与「显式传 None」
_UNSET = object()

# 写空闲 WAL checkpoint: 默认 10 分钟; 0 表示关闭 (由 config.json 提供)
_CHECKPOINT_IDLE_MINUTES = 10.0
_CHECKPOINT_POLL_MAX = 30.0
# 写队列中的 checkpoint 任务标记 (不经 BEGIN/COMMIT 事务)
_CHECKPOINT_TASK = object()

# ---------------------------------------------------------------- #
# 通用纯函数 (不涉及连接获取与线程) 
# ---------------------------------------------------------------- #

def _now() -> int:
    """10 位 Unix 时间戳 (秒) 。"""
    return int(time.time())


def _info_filter(rating: int | None, start_time: int | None,
                 end_time: int | None) -> tuple[str, list[Any]]:
    """list_info / count_info 共用的 WHERE 构造。"""
    clauses: list[str] = []
    params: list[Any] = []
    if rating is not None:
        if rating not in _RATINGS:
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


def _resolve_full_path(conn: sqlite3.Connection, path_uuid: str) -> str | None:
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


def _root_uuid(conn: sqlite3.Connection) -> str:
    """取根目录 uuid (写事务内的"先查"必须用写连接) 。"""
    row = conn.execute(
        "SELECT uuid FROM path WHERE parent_uuid IS NULL AND name = ?",
        (ROOT_NAME,),
    ).fetchone()
    if row is None:
        raise DatabaseError(42110, "Root path record is missing")
    return row["uuid"]


# ---------------------------------------------------------------- #
# 写事务模板 (由写线程对每个任务套用, 纯函数自身不管事务) 
# ---------------------------------------------------------------- #

def _run_in_transaction(conn: sqlite3.Connection, fn, args: tuple) -> Any:
    """显式事务执行一个业务写操作: BEGIN IMMEDIATE -> fn -> COMMIT / ROLLBACK。

    BEGIN IMMEDIATE 在事务开始时即取写锁, 避免锁升级死锁; 
    IntegrityError 统一转换为 InputDataError (保留原因链) 。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = fn(conn, *args)
    except sqlite3.IntegrityError as e:
        _safe_rollback(conn)
        log.error(f"Database integrity error: {e}", exc_info=True)
        raise InputDataError(
            f"Database constraint violated or conflict occurred: {e}"
        ) from e
    except BaseException:
        _safe_rollback(conn)
        raise
    conn.execute("COMMIT")
    return result


def _safe_rollback(conn: sqlite3.Connection) -> None:
    """回滚当前事务; 事务已被 SQLite 自动终止时忽略错误。"""
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- #
# 纯函数: path 表
# ---------------------------------------------------------------- #

def _get_root_path_impl(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM path WHERE parent_uuid IS NULL AND name = ?",
        (ROOT_NAME,),
    ).fetchone()
    return dict(row) if row else None


def _add_path_impl(conn: sqlite3.Connection, name: str, path_type: str,
                   parent_uuid: str | None, path_uuid: str | None) -> str:
    """新建路径节点 (存在性与重名检查在同一事务内用写连接完成) 。"""
    name = str(name).strip()
    if not name:
        raise InputDataError("Path name cannot be empty")
    if path_type not in _PATH_TYPES:
        raise InputDataError(f"Invalid path type: {path_type}")

    if parent_uuid is None:
        parent_uuid = _root_uuid(conn)
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


def _get_path_impl(conn: sqlite3.Connection, path_uuid: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM path WHERE uuid = ?", (path_uuid,)
    ).fetchone()
    return dict(row) if row else None


def _move_path_impl(conn: sqlite3.Connection, path_uuid: str,
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


def _list_paths_impl(conn: sqlite3.Connection,
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


def _update_path_impl(conn: sqlite3.Connection, path_uuid: str, name: Any,
                      status: Any, conflict_state: Any,
                      last_checked: Any) -> bool:
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
    if name is not _UNSET:
        if is_root:
            raise InputDataError("Root path name cannot be modified")
        new_name = str(name).strip()
        if not new_name:
            raise InputDataError("Path name cannot be empty")
        updates["name"] = new_name
    if status is not _UNSET:
        if is_root:
            raise InputDataError("Root path status cannot be modified")
        if status not in _PATH_STATUSES:
            raise InputDataError(f"Invalid path status: {status}")
        updates["status"] = status
    if conflict_state is not _UNSET:
        updates["conflict_state"] = conflict_state
    if last_checked is not _UNSET:
        updates["last_checked"] = last_checked
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


def _delete_path_impl(conn: sqlite3.Connection, path_uuid: str) -> bool:
    """删除空路径节点 (存在子节点或关联文件时禁止删除) 。"""
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


def _resolve_path_impl(conn: sqlite3.Connection, path_uuid: str) -> str | None:
    """解析路径节点的完整相对路径 (自根目录起, '/' 连接, 不含 ROOT) """
    return _resolve_full_path(conn, path_uuid)


# ---------------------------------------------------------------- #
# 纯函数: info 表
# ---------------------------------------------------------------- #

def _create_info_impl(conn: sqlite3.Connection, sha256: str,
                      seed: int, steps: int,
                      cfg: float, sampler: str, scheduler: str, tool: str,
                      rating: int, positive_prompt: str, negative_prompt: str,
                      note: str, files: list[dict[str, Any]] | None,
                      checkpoints: list | None, loras: list | None,
                      embeddings: list | None, vaes: list | None) -> int:
    """新建 info 记录, 文件与模型记录在同一事务内写入, 返回 info_id。

    尺寸/大小属于单个文件, 由 files 元素以 width/height/file_size 提供。
    模型绑定到 files 中的每个文件 (新模型中模型不再属于 info) 。
    """
    sha256 = str(sha256).strip()
    if not sha256:
        raise InputDataError("SHA256 cannot be empty")
    if rating not in _RATINGS:
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

    now = _now()
    cur = conn.execute(
        "INSERT INTO info (sha256, positive_prompt, negative_prompt, seed, "
        "steps, cfg, sampler, scheduler, tool, rating, note, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sha256, positive_prompt, negative_prompt, seed, steps, cfg,
         sampler, scheduler, tool, rating, note, now, now),
    )
    info_id = int(cur.lastrowid)
    file_ids = _insert_files(conn, info_id, files, now) if files else []
    for kind, rows in (("checkpoint", checkpoints), ("lora", loras),
                       ("embedding", embeddings), ("vae", vaes)):
        if not rows:
            continue
        if not file_ids:
            raise InputDataError(
                "Models cannot be attached without at least one file record")
        for file_id in file_ids:
            _sync_file_models(conn, file_id, kind, rows)
    log.debug(f"Info record created: id={info_id} sha256={sha256}")
    return info_id


def _get_info_impl(conn: sqlite3.Connection, info_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM info WHERE id = ?", (info_id,)
    ).fetchone()
    return dict(row) if row else None


def _get_info_by_sha256_impl(conn: sqlite3.Connection,
                             sha256: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM info WHERE sha256 = ?", (str(sha256).strip(),)
    ).fetchone()
    return dict(row) if row else None


def _list_info_impl(conn: sqlite3.Connection, rating: int | None,
                    start_time: int | None, end_time: int | None,
                    limit: int, offset: int) -> list[dict[str, Any]]:
    """按条件分页列出 info 记录 (按创建时间倒序)"""
    where, params = _info_filter(rating, start_time, end_time)
    rows = conn.execute(
        f"SELECT * FROM info{where} ORDER BY created_at DESC, id DESC "
        "LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    )
    return [dict(r) for r in rows]


def _count_info_impl(conn: sqlite3.Connection, rating: int | None,
                     start_time: int | None, end_time: int | None) -> int:
    """与 list_info 相同条件下的记录总数 (供分页使用) """
    where, params = _info_filter(rating, start_time, end_time)
    row = conn.execute(f"SELECT COUNT(*) FROM info{where}", params).fetchone()
    return int(row[0])


def _update_info_impl(conn: sqlite3.Connection, info_id: int,
                      fields: dict[str, Any]) -> bool:
    """更新 info 字段 (白名单内的列), 自动刷新 updated_at。"""
    invalid = sorted(set(fields) - _INFO_UPDATABLE_FIELDS)
    if invalid:
        raise InputDataError(f"Fields not allowed to modify: {invalid}")
    if not fields:
        return False
    if "rating" in fields and fields["rating"] not in _RATINGS:
        raise InputDataError(f"Invalid rating: {fields['rating']}")

    set_clause = ", ".join(f"{col} = ?" for col in fields)  # 列名来自白名单
    params = [*fields.values(), _now(), info_id]
    cur = conn.execute(
        f"UPDATE info SET {set_clause}, updated_at = ? WHERE id = ?", params
    )
    return cur.rowcount > 0


def _delete_info_impl(conn: sqlite3.Connection, info_id: int) -> bool:
    """删除整条 info 记录 (file 与四张模型表按 CASCADE 联动清理) 。"""
    cur = conn.execute("DELETE FROM info WHERE id = ?", (info_id,))
    deleted = cur.rowcount > 0
    if deleted:
        log.debug(f"Info record deleted: id={info_id}")
    return deleted


# ---------------------------------------------------------------- #
# 纯函数: 文件与模型的关系 (模型注册表 + 文件-模型中间表)
# ---------------------------------------------------------------- #

def _normalize_model_fields(row: dict[str, Any] | str,
                            kind: str) -> dict[str, Any]:
    """校验并规范化单条模型输入 (名称/版本/URL 去除首尾空格)。

    LoRA/LyCORIS 额外校验 model_type 与 weight。
    """
    if isinstance(row, str):
        row = {"name": row}
    name = str(row.get("name", "")).strip()
    if not name:
        raise InputDataError("Model name cannot be empty")
    result: dict[str, Any] = {
        "name": name,
        "version": str(row.get("version") or "").strip(),
        "url": str(row.get("url") or "").strip(),
    }
    if kind == "lora":
        model_type = int(row.get("model_type", 0))
        if model_type not in _LORA_MODEL_TYPES:
            raise InputDataError(
                f"Invalid LoRA/LyCORIS model type: {model_type}")
        if row.get("weight") is None:
            raise InputDataError(f"LoRA entry is missing weight: {name}")
        result["model_type"] = model_type
        result["weight"] = float(row["weight"])
    return result


def _model_identity(row: dict[str, Any], kind: str) -> tuple:
    """模型唯一键: name + version + url (LoRA 另加 model_type)。"""
    key = (row["name"], row["version"], row["url"])
    return (row["model_type"],) + key if kind == "lora" else key


def _get_or_create_model(conn: sqlite3.Connection, kind: str,
                         row: dict[str, Any]) -> int:
    """按唯一键查找模型注册表记录, 不存在则新建, 返回模型 id。"""
    table = _MODEL_TABLES[kind]
    if kind == "lora":
        found = conn.execute(
            f"SELECT id FROM {table} "
            "WHERE model_type = ? AND name = ? AND version = ? AND url = ?",
            (row["model_type"], row["name"], row["version"], row["url"]),
        ).fetchone()
    else:
        found = conn.execute(
            f"SELECT id FROM {table} "
            "WHERE name = ? AND version = ? AND url = ?",
            (row["name"], row["version"], row["url"]),
        ).fetchone()
    if found is not None:
        return int(found["id"])
    if kind == "lora":
        cur = conn.execute(
            f"INSERT INTO {table} (model_type, name, version, url) "
            "VALUES (?, ?, ?, ?)",
            (row["model_type"], row["name"], row["version"], row["url"]),
        )
    else:
        cur = conn.execute(
            f"INSERT INTO {table} (name, version, url) VALUES (?, ?, ?)",
            (row["name"], row["version"], row["url"]),
        )
    return int(cur.lastrowid)


def _sync_file_models(conn: sqlite3.Connection, file_id: int, kind: str,
                      rows: list[dict[str, Any] | str]) -> None:
    """整体替换单个文件在某类模型上的绑定。

    传入空列表表示清空该类; 同一提交内按唯一键去重 (重复视为输入错误)。
    """
    junction, column = _MODEL_JUNCTIONS[kind]
    conn.execute(f"DELETE FROM {junction} WHERE file_id = ?", (file_id,))
    seen: set[tuple] = set()
    for raw in rows:
        row = _normalize_model_fields(raw, kind)
        key = _model_identity(row, kind)
        if key in seen:
            raise InputDataError(
                f"Duplicate model in a single submission: {row['name']}")
        seen.add(key)
        model_id = _get_or_create_model(conn, kind, row)
        if kind == "lora":
            conn.execute(
                f"INSERT INTO {junction} (file_id, {column}, weight) "
                "VALUES (?, ?, ?)",
                (file_id, model_id, row["weight"]),
            )
        else:
            conn.execute(
                f"INSERT INTO {junction} (file_id, {column}) VALUES (?, ?)",
                (file_id, model_id),
            )


def _get_file_id(conn: sqlite3.Connection, path_uuid: str,
                 filename: str) -> int:
    row = conn.execute(
        "SELECT id FROM file WHERE path_uuid = ? AND filename = ?",
        (path_uuid, filename),
    ).fetchone()
    if row is None:
        raise InputDataError(
            f"File record does not exist: {path_uuid}/{filename}")
    return int(row["id"])


def _update_file_models_impl(conn: sqlite3.Connection, path_uuid: str,
                             filename: str, checkpoints: list | None,
                             loras: list | None, embeddings: list | None,
                             vaes: list | None) -> None:
    """整体修改某个文件使用的模型。

    参数为 None 表示该类模型保持不变; 传空列表表示清空该类。
    """
    file_id = _get_file_id(conn, path_uuid, filename)
    for kind, rows in (("checkpoint", checkpoints), ("lora", loras),
                       ("embedding", embeddings), ("vae", vaes)):
        if rows is not None:
            _sync_file_models(conn, file_id, kind, rows)
    log.debug(f"File models updated: file_id={file_id}")


def _get_file_models_impl(conn: sqlite3.Connection, path_uuid: str,
                          filename: str) -> dict[str, list[dict[str, Any]]]:
    """获取某个文件关联的全部模型, 按种类分组。

    LoRA/LyCORIS 行的 weight 来自中间表。
    """
    file_id = _get_file_id(conn, path_uuid, filename)
    result: dict[str, list[dict[str, Any]]] = {}
    for kind, table in _MODEL_TABLES.items():
        junction, column = _MODEL_JUNCTIONS[kind]
        if kind == "lora":
            rows = conn.execute(
                f"SELECT m.id, m.model_type, m.name, m.version, m.url, "
                f"j.weight FROM {junction} j "
                f"JOIN {table} m ON m.id = j.{column} "
                "WHERE j.file_id = ? ORDER BY m.id",
                (file_id,),
            )
        else:
            rows = conn.execute(
                f"SELECT m.id, m.name, m.version, m.url "
                f"FROM {junction} j JOIN {table} m ON m.id = j.{column} "
                "WHERE j.file_id = ? ORDER BY m.id",
                (file_id,),
            )
        result[kind] = [dict(r) for r in rows]
    return result


# ---------------------------------------------------------------- #
# 纯函数: file 表
# ---------------------------------------------------------------- #

def _insert_files(conn: sqlite3.Connection, info_id: int,
                  files: list[dict[str, Any]], now: int) -> list[int]:
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
            (info_id, path_uuid, filename, mime_type, now,
             int(f.get("width") or 0), int(f.get("height") or 0),
             int(f.get("file_size") or 0)),
        )
        file_ids.append(int(cur.lastrowid))
    return file_ids


def _add_files_impl(conn: sqlite3.Connection, info_id: int,
                    files: list[dict[str, Any]]) -> int:
    """批量追加文件记录 (info 存在性检查在同一事务内) , 返回写入条数。"""
    if not files:
        return 0
    if conn.execute(
        "SELECT 1 FROM info WHERE id = ?", (info_id,)
    ).fetchone() is None:
        raise InputDataError(f"Info record does not exist: {info_id}")
    file_ids = _insert_files(conn, info_id, files, _now())
    log.debug(f"Files added: info_id={info_id} count={len(file_ids)}")
    return len(file_ids)


def _get_files_by_info_impl(conn: sqlite3.Connection,
                            info_id: int) -> list[dict[str, Any]]:
    """获取 info 的全部文件记录, 附带解析后的目录路径 (directory)"""
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
            dir_cache[pu] = _resolve_full_path(conn, pu)
        item["directory"] = dir_cache[pu]
        result.append(item)
    return result


def _list_files_by_path_impl(conn: sqlite3.Connection,
                             path_uuid: str) -> list[dict[str, Any]]:
    """列出某路径节点下的全部文件记录"""
    rows = conn.execute(
        "SELECT id, info_id, path_uuid, filename, mime_type, added_at, "
        "width, height, file_size "
        "FROM file WHERE path_uuid = ? ORDER BY filename",
        (path_uuid,),
    )
    directory = _resolve_full_path(conn, path_uuid)
    return [{**dict(r), "directory": directory} for r in rows]


def _like_param(value: Any) -> str:
    """LIKE 参数转义: % 与 _ 按字面匹配 (配合 ESCAPE '\\')。"""
    escaped = (str(value).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def _search_where(spec: dict[str, Any]) -> tuple[str, list[Any]]:
    """结构化搜索条件 -> WHERE 子句与参数 (供 search_files 使用)。

    语义约定: 同一条件数组内多个元素取 OR, 不同条件之间取 AND;
    include 表示"必须包含", exclusion 表示"不得包含"。
    """
    clauses: list[str] = []
    params: list[Any] = []

    def add_any_like(expr: str, values: list[Any]) -> None:
        """OR 组: 任一值被 expr 包含。"""
        if not values:
            return
        clauses.append(
            "(" + " OR ".join(f"{expr} LIKE ? ESCAPE '\\'" for _ in values) + ")")
        params.extend(_like_param(v) for v in values)

    def add_all_not_like(expr: str, values: list[Any]) -> None:
        """排除组: 每个值都不得被 expr 包含。"""
        for v in values:
            clauses.append(f"{expr} NOT LIKE ? ESCAPE '\\'")
            params.append(_like_param(v))

    def add_ranges(expr: str, ranges: list[tuple[int, int]]) -> None:
        """数值区间 OR 组: (lo, hi); 0 表示该侧不设限。"""
        if not ranges:
            return
        parts: list[str] = []
        for lo, hi in ranges:
            sub: list[str] = []
            if lo > 0:
                sub.append(f"{expr} > ?")
                params.append(lo)
            if hi > 0:
                sub.append(f"{expr} < ?")
                params.append(hi)
            parts.append("(" + " AND ".join(sub) + ")" if sub else "1 = 1")
        clauses.append("(" + " OR ".join(parts) + ")")

    # 基本搜索: content 匹配"正向提示词或文件名", paths 匹配所在目录
    content = spec.get("content", [])
    if content:
        parts: list[str] = []
        for v in content:
            parts.append("(i.positive_prompt LIKE ? ESCAPE '\\'"
                         " OR f.filename LIKE ? ESCAPE '\\')")
            params.extend((_like_param(v), _like_param(v)))
        clauses.append("(" + " OR ".join(parts) + ")")
    add_any_like("d.full", spec.get("paths", []))

    add_any_like("d.full", spec.get("path_include", []))
    add_all_not_like("d.full", spec.get("path_exclude", []))
    add_any_like("f.filename", spec.get("name_include", []))
    add_all_not_like("f.filename", spec.get("name_exclude", []))
    add_any_like("i.positive_prompt", spec.get("prompt_include", []))
    add_all_not_like("i.positive_prompt", spec.get("prompt_exclude", []))
    add_any_like("i.negative_prompt", spec.get("neg_prompt_include", []))
    add_all_not_like("i.negative_prompt", spec.get("neg_prompt_exclude", []))

    # 文件类型 (扩展名, 小写不带点)
    extensions = [str(e).lower() for e in spec.get("file_types", [])]
    if extensions:
        clauses.append("(" + " OR ".join(
            "LOWER(f.filename) LIKE ?" for _ in extensions) + ")")
        params.extend(f"%.{e}" for e in extensions)

    # 精确匹配 OR 组
    for expr, key in (("i.sha256", "sha256"), ("i.tool", "tool"),
                      ("i.sampler", "sampler"), ("i.scheduler", "scheduler"),
                      ("i.seed", "seed"), ("i.rating", "rating")):
        values = spec.get(key, [])
        if values:
            clauses.append(f"{expr} IN ({', '.join('?' for _ in values)})")
            params.extend(values)

    add_any_like("i.note", spec.get("note", []))
    add_ranges("f.file_size", spec.get("size_ranges", []))
    add_ranges("f.width", spec.get("width_ranges", []))
    add_ranges("f.height", spec.get("height_ranges", []))
    add_ranges("f.added_at", spec.get("time_ranges", []))

    # 模型匹配 (文件级关联): 同名即命中, ver 为空表示该名称下的全部版本
    for kind, key in (("checkpoint", "ckpt"), ("lora", "lora"),
                      ("embedding", "embed")):
        items = spec.get(key, [])
        if not items:
            continue
        table = _MODEL_TABLES[kind]
        junction, column = _MODEL_JUNCTIONS[kind]
        exists: list[str] = []
        for name, version in items:
            if version:
                exists.append(
                    f"EXISTS (SELECT 1 FROM {junction} j "
                    f"JOIN {table} m ON m.id = j.{column} "
                    "WHERE j.file_id = f.id AND LOWER(m.name) = LOWER(?) "
                    "AND m.version = ?)")
                params.extend((name, version))
            else:
                exists.append(
                    f"EXISTS (SELECT 1 FROM {junction} j "
                    f"JOIN {table} m ON m.id = j.{column} "
                    "WHERE j.file_id = f.id AND LOWER(m.name) = LOWER(?))")
                params.append(name)
        clauses.append("(" + " OR ".join(exists) + ")")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _search_files_impl(conn: sqlite3.Connection, spec: dict[str, Any],
                       limit: int, after: dict[str, Any] | None
                       ) -> list[dict[str, Any]]:
    """按结构化条件搜索文件记录 (文件 + info + 完整目录路径)。

    排序: added_at 倒序, 文件名升序, 目录升序 (游标恢复所需的稳定次序) 。
    after: 上一页最后一个对象 {name, sha256, path}; 从其后继续。
    """
    where, params = _search_where(spec)
    rows = conn.execute(
        f"""
        WITH RECURSIVE dirs(uuid, full) AS (
            SELECT uuid, '/' FROM path WHERE parent_uuid IS NULL
            UNION ALL
            SELECT p.uuid,
                   CASE WHEN d.full = '/' THEN '/' || p.name
                        ELSE d.full || '/' || p.name END
            FROM path p JOIN dirs d ON p.parent_uuid = d.uuid
        )
        SELECT f.filename AS name, i.sha256 AS sha256, i.rating AS rating,
               f.file_size AS file_size, f.added_at AS add_time,
               d.full AS path, f.info_id AS info_id,
               f.path_uuid AS path_uuid
        FROM file f
        JOIN info i ON i.id = f.info_id
        JOIN dirs d ON d.uuid = f.path_uuid
        {where}
        ORDER BY f.added_at DESC, f.filename ASC, d.full ASC
        """,
        params,
    )
    result: list[dict[str, Any]] = []
    skipping = after is not None
    for row in rows:
        item = dict(row)
        if skipping:
            if (item["name"] == after.get("name")
                    and item["sha256"] == after.get("sha256")
                    and item["path"] == after.get("path")):
                skipping = False
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _update_file_impl(conn: sqlite3.Connection, path_uuid: str,
                      filename: str, fields: dict[str, Any]) -> bool:
    """更新单个文件记录的可修改字段 (mime_type/width/height/file_size)。"""
    invalid = sorted(set(fields) - _FILE_UPDATABLE_FIELDS)
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


def _rename_file_impl(conn: sqlite3.Connection, path_uuid: str,
                      old_name: str, new_name: str) -> bool:
    """原地重命名文件记录 (保留 file.id, 模型关联不丢失)。"""
    cur = conn.execute(
        "UPDATE file SET filename = ? WHERE path_uuid = ? AND filename = ?",
        (new_name, path_uuid, old_name),
    )
    return cur.rowcount > 0


def _relocate_file_impl(conn: sqlite3.Connection, src_path_uuid: str,
                        old_name: str, dst_path_uuid: str,
                        new_name: str) -> bool:
    """原地移动文件记录 (保留 file.id, 模型关联不丢失)。"""
    cur = conn.execute(
        "UPDATE file SET path_uuid = ?, filename = ? "
        "WHERE path_uuid = ? AND filename = ?",
        (dst_path_uuid, new_name, src_path_uuid, old_name),
    )
    return cur.rowcount > 0


def _delete_file_impl(conn: sqlite3.Connection, path_uuid: str,
                      filename: str) -> bool:
    """删除单条文件记录 (file_* 中间表按 CASCADE 联动清理) """
    cur = conn.execute(
        "DELETE FROM file WHERE path_uuid = ? AND filename = ?",
        (path_uuid, filename),
    )
    deleted = cur.rowcount > 0
    if deleted:
        log.debug(f"File record deleted: {path_uuid}/{filename}")
    return deleted


def _delete_files_by_info_impl(conn: sqlite3.Connection, info_id: int) -> int:
    """删除 info 名下全部文件记录 (仅删 file 表), 返回删除条数。"""
    cur = conn.execute(
        "DELETE FROM file WHERE info_id = ?", (info_id,)
    )
    deleted = cur.rowcount
    if deleted:
        log.debug(f"File records deleted: info_id={info_id} count={deleted}")
    return deleted


# ---------------------------------------------------------------- #
# 纯函数: 模型查询 (去重与反查) 
# ---------------------------------------------------------------- #

def _list_model_names_impl(conn: sqlite3.Connection,
                           kind: str) -> list[dict[str, Any]]:
    """列出某种模型的去重名称及关联文件数量 (仅统计仍被引用的模型)。"""
    if kind not in _MODEL_TABLES:
        raise InputDataError(f"Invalid model kind: {kind}")
    table = _MODEL_TABLES[kind]
    junction, column = _MODEL_JUNCTIONS[kind]
    rows = conn.execute(
        f"SELECT m.name, COUNT(DISTINCT j.file_id) AS file_count "
        f"FROM {table} m JOIN {junction} j ON j.{column} = m.id "
        "GROUP BY m.name ORDER BY m.name"
    )
    return [dict(r) for r in rows]


def _find_infos_by_model_impl(conn: sqlite3.Connection,
                              name: str) -> list[dict[str, Any]]:
    """按模型名称反查: 通过 file_* 中间表找到关联文件, 再关联 info。

    全程使用同一读连接 (不回门面二次派发) 。
    """
    name = str(name).strip()
    if not name:
        return []
    matched: list[dict[str, Any]] = []
    for kind, table in _MODEL_TABLES.items():
        junction, column = _MODEL_JUNCTIONS[kind]
        if kind == "lora":
            sql = (
                f"SELECT '{kind}' AS model_kind, f.info_id AS info_id, "
                "m.name, m.version, m.url, j.weight AS weight, "
                "m.model_type AS model_type "
                f"FROM {junction} j JOIN {table} m ON m.id = j.{column} "
                "JOIN file f ON f.id = j.file_id WHERE m.name = ?")
        else:
            sql = (
                f"SELECT '{kind}' AS model_kind, f.info_id AS info_id, "
                "m.name, m.version, m.url, NULL AS weight, "
                "NULL AS model_type "
                f"FROM {junction} j JOIN {table} m ON m.id = j.{column} "
                "JOIN file f ON f.id = j.file_id WHERE m.name = ?")
        matched.extend(dict(r) for r in conn.execute(sql, (name,)))
    if not matched:
        return []

    grouped: dict[int, dict[tuple, dict[str, Any]]] = {}
    for m in matched:
        key = (m["model_kind"], m["name"], m["version"], m["url"],
               m.get("model_type"))
        grouped.setdefault(m["info_id"], {}).setdefault(key, m)
    matched_by_info = {k: list(v.values()) for k, v in grouped.items()}

    result = []
    for info_id in sorted(matched_by_info):
        info = _get_info_impl(conn, info_id)
        if info is None:
            continue  # 理论上不会发生 (CASCADE) 
        result.append({
            **info,
            "matched_models": matched_by_info[info_id],
            "files": _get_files_by_info_impl(conn, info_id),
        })
    return result


# ---------------------------------------------------------------- #
# 纯函数: 孤儿记录清理
# ---------------------------------------------------------------- #

_ORPHAN_CONDITION = "NOT EXISTS (SELECT 1 FROM file WHERE file.info_id = info.id)"


def _count_orphan_info_impl(conn: sqlite3.Connection) -> int:
    """统计 file 表中已无任何关联文件的 info 记录数量 (供用户确认) """
    row = conn.execute(
        f"SELECT COUNT(*) FROM info WHERE {_ORPHAN_CONDITION}"
    ).fetchone()
    return int(row[0])


def _cleanup_orphan_info_impl(conn: sqlite3.Connection) -> int:
    """删除已无关联文件的 info 记录 (模型表经 CASCADE 联动清理), 返回删除数。"""
    cur = conn.execute(f"DELETE FROM info WHERE {_ORPHAN_CONDITION}")
    deleted = cur.rowcount
    if deleted:
        log.debug(f"Orphan info records cleaned up: count={deleted}")
    return deleted


# ---------------------------------------------------------------- #
# 引擎: 唯一写线程 + 读连接池 (连接管理集中于此) 
# ---------------------------------------------------------------- #

class _Engine:
    """连接与线程基础设施。

    写侧: 单后台线程持有唯一写连接 (线程内创建) , 任务经线程安全 FIFO
    队列提交, 结果经 concurrent.futures.Future 返回。
    读侧: ThreadPoolExecutor(4), 工作线程经 threading.local() 独享只读连接。
    """

    def __init__(self, db_path: Path,
                 idle_checkpoint_minutes: float = _CHECKPOINT_IDLE_MINUTES):
        self.db_path = db_path
        self._closed = False
        self._close_lock = threading.Lock()
        self._write_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._writer = threading.Thread(
            target=self._write_loop, name="db-writer", daemon=True)
        self._read_executor = ThreadPoolExecutor(
            max_workers=_READ_WORKERS, thread_name_prefix="db-read")
        self._local = threading.local()
        self._read_conns: list[sqlite3.Connection] = []
        self._read_conns_lock = threading.Lock()
        # 写空闲计时与 WAL checkpoint 定时器 (0 表示关闭该功能)
        self._idle_checkpoint_seconds = max(
            0.0, float(idle_checkpoint_minutes)) * 60.0
        self._last_write_at = time.monotonic()
        self._checkpoint_stop = threading.Event()
        self._checkpoint_thread: threading.Thread | None = None
        self._writer.start()
        if self._idle_checkpoint_seconds > 0:
            self._checkpoint_thread = threading.Thread(
                target=self._checkpoint_loop, name="db-checkpoint", daemon=True)
            self._checkpoint_thread.start()

    # ---- 写侧 ----
    def submit_write(self, fn, *args) -> Future:
        """提交写任务到 FIFO 队列, 返回 Future。

        入队在 close 锁内完成: 与 close() 的关闭哨兵之间有确定先后顺序, 
        保证 close() 返回后不会仍有任务处于未完成状态。
        """
        with self._close_lock:
            if self._closed:
                raise DatabaseClosedError(_CLOSED_MSG)
            future = Future()
            self._write_queue.put((fn, args, future))
            self._last_write_at = time.monotonic()
            return future

    # ---- WAL checkpoint ----
    def submit_checkpoint(self) -> Future | None:
        """排队执行一次 WAL checkpoint; 引擎已关闭时返回 None。

        checkpoint 在写线程中直接执行 (不在 BEGIN/COMMIT 事务内),
        避免长事务阻止 WAL 合并。
        """
        with self._close_lock:
            if self._closed:
                return None
            future = Future()
            self._write_queue.put((_CHECKPOINT_TASK, (), future))
            self._last_write_at = time.monotonic()
            return future

    def _checkpoint_loop(self) -> None:
        """写空闲达到阈值后执行 WAL checkpoint (链式轮询)。"""
        interval = self._idle_checkpoint_seconds
        poll = min(_CHECKPOINT_POLL_MAX, max(1.0, interval / 4.0))
        while not self._checkpoint_stop.wait(poll):
            if time.monotonic() - self._last_write_at < interval:
                continue
            try:
                future = self.submit_checkpoint()
                if future is not None:
                    future.result()
            except Exception as e:  # noqa: BLE001 checkpoint 失败不影响服务
                log.warning(f"WAL checkpoint failed: {e}")

    def _run_checkpoint(self, conn: sqlite3.Connection) -> tuple:
        """在写连接上执行 TRUNCATE checkpoint, 返回 (busy, wal, moved)。"""
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        result = tuple(int(v) for v in (row or (0, 0, 0)))
        busy, wal_frames, moved = (result + (0, 0, 0))[:3]
        if busy:
            log.warning("WAL checkpoint skipped: database is busy")
        else:
            # TRUNCATE 模式下 SQLite 可能在返回前已截断 WAL, 计数常为 0
            log.debug(f"WAL checkpoint completed "
                      f"(wal frames={wal_frames}, moved={moved})")
        return result

    def _write_loop(self) -> None:
        """写线程主体: 创建并持有唯一写连接, 逐个执行队列中的业务写操作。"""
        try:
            conn = open_connection(self.db_path)
        except Exception:
            log.error("Failed to open the write connection", exc_info=True)
            self._drain_with_error_on_startup()
            return
        try:
            while True:
                task = self._write_queue.get()
                if task is None:  # 关闭哨兵: 先于哨兵入队的任务均已处理完毕
                    break
                fn, args, future = task
                try:
                    if future.set_running_or_notify_cancel():
                        try:
                            if fn is _CHECKPOINT_TASK:
                                future.set_result(self._run_checkpoint(conn))
                            else:
                                future.set_result(
                                    _run_in_transaction(conn, fn, args))
                        except InputDataError as e:
                            # 业务校验类拒绝: 属调用方输入问题, 降为 DEBUG
                            log.debug(f"Write operation rejected: {e}")
                            future.set_exception(e)
                        except Exception as e:
                            log.error(
                                "Write operation failed: "
                                f"{getattr(fn, '__name__', fn)}",
                                exc_info=True,
                            )
                            future.set_exception(e)
                except Exception:
                    # Future 状态异常等极端情形: 不允许写线程死亡, 
                    # 否则所有等待者将永久阻塞
                    log.error("Write worker internal error", exc_info=True)
        finally:
            conn.close()
            log.debug("Write connection closed")

    def _drain_with_error_on_startup(self) -> None:
        """写连接建立失败: 持续排空队列, 等待者全部以该异常收尾, 避免永久阻塞。"""
        while True:
            task = self._write_queue.get()
            if task is None:
                return
            _, _, future = task
            if future.set_running_or_notify_cancel():
                future.set_exception(
                    SchemaInitError("Database write connection failed to open"))

    # ---- 读侧 ----
    def submit_read(self, fn, *args) -> Future:
        """提交读任务到线程池 (并发执行, 不排队等待前序任务) 。"""
        with self._close_lock:
            if self._closed:
                raise DatabaseClosedError(_CLOSED_MSG)
            return self._read_executor.submit(self._read_job, fn, args)

    def _read_job(self, fn, args) -> Any:
        try:
            return fn(self._get_read_conn(), *args)
        except InputDataError as e:
            log.debug(f"Read operation rejected: {e}")
            raise
        except Exception:
            log.error(
                f"Read operation failed: {getattr(fn, '__name__', fn)}",
                exc_info=True,
            )
            raise

    def _get_read_conn(self) -> sqlite3.Connection:
        """取当前读线程的独享连接; 不存在则在当前线程内创建。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_connection(self.db_path)
            # 读连接严禁执行任何写语句: 数据库层面强制只读
            conn.execute("PRAGMA query_only = ON")
            self._local.conn = conn
            with self._read_conns_lock:
                self._read_conns.append(conn)
        return conn

    # ---- 生命周期 ----
    def close(self) -> None:
        """等待写队列清空、进行中任务全部完成后关闭所有连接 (幂等) 。"""
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        # 先停止 checkpoint 定时器, 再投递关闭哨兵 (此时已无新任务可入队)
        self._checkpoint_stop.set()
        if self._checkpoint_thread is not None:
            self._checkpoint_thread.join(timeout=5)
        self._write_queue.put(None)  # FIFO 保证排在所有已提交任务之后
        self._writer.join()
        self._read_executor.shutdown(wait=True)
        # 读线程此时均已退出, 集中兜底关闭其连接 (无线程仍在使用, 
        # 不构成运行期跨线程共享; 线程本地对象本身亦随线程退出释放) 
        with self._read_conns_lock:
            for conn in self._read_conns:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._read_conns.clear()
        log.debug("Database engine closed")


# ---------------------------------------------------------------- #
# 模块级生命周期与任务派发
# ---------------------------------------------------------------- #

_ENGINE: _Engine | None = None
_ENGINE_LOCK = threading.Lock()


def _norm_db_path(db_path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(db_path)))


def _require_engine() -> _Engine:
    engine = _ENGINE
    if engine is None:
        raise DatabaseClosedError(_CLOSED_MSG)
    return engine


def init(db_path: Any, expected_version: int = DB_VERSION,
         idle_checkpoint_minutes: float = _CHECKPOINT_IDLE_MINUTES) -> None:
    """初始化数据库引擎 (写线程 + 读连接池) , 并完成建表与根目录记录。

    :param db_path: SQLite 数据库文件路径。
    :param expected_version: 程序记录的数据库版本; 低于该值的库会被升级,
        高于该值抛 DatabaseVersionError (拒绝启动) 。
    :param idle_checkpoint_minutes: 写空闲多少分钟后自动合并 WAL;
        0 表示关闭。该值在初始化时读取, 修改后需重启程序生效。
    同路径重复调用为幂等操作; 不同路径重复调用会先关闭旧引擎。
    """
    global _ENGINE
    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    path_key = _norm_db_path(db_path)
    # 迁移前准备: 先回写 WAL, 再备份主库 (必须在任何业务服务启动前完成)
    need_migration, current_version = migration_needed(
        db_file, int(expected_version))
    with _ENGINE_LOCK:
        current = _ENGINE
        if current is not None:
            if current.path_key == path_key:
                return  # 幂等: 同一路径重复初始化直接复用
            log.warning("Database re-initializing with a different path")
            _ENGINE = None
        engine = _Engine(db_file, idle_checkpoint_minutes)
        engine.path_key = path_key
        _ENGINE = engine
    try:
        if need_migration:
            future = engine.submit_checkpoint()
            if future is not None:
                result = future.result()
                if result and result[0]:
                    log.warning("WAL checkpoint before migration reported busy")
            backup_database(db_file, current_version)
        engine.submit_write(bootstrap, int(expected_version)).result()
    except BaseException:
        engine.close()
        with _ENGINE_LOCK:
            if _ENGINE is engine:
                _ENGINE = None
        raise


def close() -> None:
    """关闭数据库: 等待写队列清空、所有进行中任务完成后关闭全部连接。

    程序退出时必须调用; 重复调用为幂等操作。
    """
    global _ENGINE
    with _ENGINE_LOCK:
        engine = _ENGINE
        _ENGINE = None
    if engine is not None:
        engine.close()


def _submit_write(fn, *args) -> Any:
    """提交写任务并阻塞等待 (保持既有同步调用风格) 。"""
    return _require_engine().submit_write(fn, *args).result()


def _submit_read(fn, *args) -> Any:
    """提交读任务并阻塞等待 (保持既有同步调用风格) 。"""
    return _require_engine().submit_read(fn, *args).result()


# ---------------------------------------------------------------- #
# DataBase 门面: 对外接口名称 / 调用方式与旧版一致
# ---------------------------------------------------------------- #

class DataBase:
    """Genesis 数据库封装: 写操作经单写线程串行事务化执行, 
    读操作经读线程池并发执行。生命周期亦可用模块级 init()/close() 管理。
    """

    def __init__(self, db_folder: Path = FolderPath.DATABASE_FOLDER,
                 db_version: int = DB_VERSION,
                 idle_checkpoint_minutes: float = _CHECKPOINT_IDLE_MINUTES):
        self.db_file = Path(db_folder) / DB_FILE_NAME
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        init(self.db_file, expected_version=db_version,
             idle_checkpoint_minutes=idle_checkpoint_minutes)
        log.debug(f"Database ready: {self.db_file} (schema v{db_version}, "
                  f"idle checkpoint={idle_checkpoint_minutes} min)")

    def close(self) -> None:
        """关闭数据库 (程序退出时必须调用) 。等待写队列清空与进行中任务完成。"""
        close()  # 模块级 close()

    def __enter__(self) -> "DataBase":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # path 表
    def get_root_path(self) -> dict[str, Any] | None:
        """获取根目录记录。"""
        return _submit_read(_get_root_path_impl)

    def add_path(self, name: str, path_type: str = "mount",
                 parent_uuid: str | None = None,
                 path_uuid: str | None = None) -> str:
        """新建路径节点, 返回新节点 uuid。parent_uuid 缺省挂在根目录下。

        path_uuid 缺省时自动生成; 调用方 (如 file_manager) 可自行生成后传入。
        """
        return _submit_write(_add_path_impl, name, path_type,
                             parent_uuid, path_uuid)

    def get_path(self, path_uuid: str) -> dict[str, Any] | None:
        return _submit_read(_get_path_impl, path_uuid)

    def list_paths(self, parent_uuid: str | None = None) -> list[dict[str, Any]]:
        """列出路径节点; 给定 parent_uuid 时只返回其直接子节点。"""
        return _submit_read(_list_paths_impl, parent_uuid)

    def update_path(self, path_uuid: str, name: Any = _UNSET, status: Any = _UNSET,
                    conflict_state: Any = _UNSET, last_checked: Any = _UNSET) -> bool:
        """更新路径节点字段。未传入的参数保持不变 (显式传 None 表示置空) """
        return _submit_write(_update_path_impl, path_uuid, name, status,
                             conflict_state, last_checked)

    def move_path(self, path_uuid: str, parent_uuid: str, name: str) -> bool:
        """移动/重命名目录节点 (保留节点 uuid, 子树与文件记录不变)。"""
        return _submit_write(_move_path_impl, path_uuid, parent_uuid, name)

    def delete_path(self, path_uuid: str) -> bool:
        """删除空路径节点 (存在子节点或关联文件时禁止删除) 。"""
        return _submit_write(_delete_path_impl, path_uuid)

    def resolve_path(self, path_uuid: str) -> str | None:
        """解析路径节点的完整逻辑路径 (自根目录起, '/' 连接, 不含 ROOT)。

        恒以 "/" 开头: 根节点返回 "/", 其余形如 "/abc/123"；节点不存在返回 None。
        """
        return _submit_read(_resolve_path_impl, path_uuid)

    # info 表
    def create_info(
        self,
        sha256: str,
        *,
        seed: int = 0, # 应该没几个人用 0 做随机种吧
        steps: int = 0,
        cfg: float = 0.0,
        sampler: str = "",
        scheduler: str = "",
        tool: str = "",
        rating: int = 1,
        positive_prompt: str = "",
        negative_prompt: str = "",
        note: str = "",
        files: list[dict[str, Any]] | None = None,
        checkpoints: list[dict[str, Any] | str] | None = None,
        loras: list[dict[str, Any] | str] | None = None,
        embeddings: list[dict[str, Any] | str] | None = None,
        vaes: list[dict[str, Any] | str] | None = None,
    ) -> int:
        """新建 info 记录, 可在同一事务内附带文件与模型记录, 返回 info_id。

        files 元素: {"path_uuid", "filename", "mime_type",
        "width"?, "height"?, "file_size"?} (尺寸/大小属于单个文件)
        checkpoints / embeddings / vaes 元素: {"name", "version"?, "url"?} 或纯名称字符串
        loras 元素: {"name", "weight", "model_type"?(0=LoRA,1=LyCORIS), "version"?, "url"?}
        模型会绑定到 files 中的每个文件; 未提供文件时不能附带模型。
        """
        return _submit_write(
            _create_info_impl, sha256, seed, steps,
            cfg, sampler, scheduler, tool, rating, positive_prompt,
            negative_prompt, note, files, checkpoints, loras, embeddings, vaes,
        )

    def get_info(self, info_id: int) -> dict[str, Any] | None:
        return _submit_read(_get_info_impl, info_id)

    def get_info_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        return _submit_read(_get_info_by_sha256_impl, sha256)

    def list_info(self, rating: int | None = None, start_time: int | None = None,
                  end_time: int | None = None, limit: int = 50,
                  offset: int = 0) -> list[dict[str, Any]]:
        """按条件分页列出 info 记录 (按创建时间倒序)"""
        return _submit_read(_list_info_impl, rating, start_time, end_time,
                            limit, offset)

    def count_info(self, rating: int | None = None, start_time: int | None = None,
                   end_time: int | None = None) -> int:
        """与 list_info 相同条件下的记录总数 (供分页使用) """
        return _submit_read(_count_info_impl, rating, start_time, end_time)

    def update_info(self, info_id: int, **fields: Any) -> bool:
        """更新 info 字段 (白名单内的列), 自动刷新 updated_at。"""
        return _submit_write(_update_info_impl, info_id, dict(fields))

    def delete_info(self, info_id: int) -> bool:
        """删除整条 info 记录 (其文件与文件-模型关联按 CASCADE 联动清理) 。"""
        return _submit_write(_delete_info_impl, info_id)

    # file 表
    def add_file(self, info_id: int, path_uuid: str, filename: str,
                 mime_type: str, width: int = 0, height: int = 0,
                 file_size: int = 0) -> None:
        """为 info 追加一条文件记录 (可附带单个文件的尺寸/大小)。"""
        self.add_files(info_id, [{
            "path_uuid": path_uuid,
            "filename": filename,
            "mime_type": mime_type,
            "width": width,
            "height": height,
            "file_size": file_size,
        }])

    def add_files(self, info_id: int, files: list[dict[str, Any]]) -> int:
        """批量追加文件记录 (同一事务) , 返回写入条数。

        元素格式: {"path_uuid", "filename", "mime_type",
        "width"?, "height"?, "file_size"?}
        """
        return _submit_write(_add_files_impl, info_id, files)

    def update_file(self, path_uuid: str, filename: str, **fields: Any) -> bool:
        """更新单个文件记录的字段 (白名单: mime_type/width/height/file_size)。"""
        return _submit_write(_update_file_impl, path_uuid, filename,
                             dict(fields))

    def rename_file(self, path_uuid: str, old_name: str,
                    new_name: str) -> bool:
        """原地重命名文件记录 (保留 file.id 与模型关联)。"""
        return _submit_write(_rename_file_impl, path_uuid, old_name, new_name)

    def relocate_file(self, src_path_uuid: str, old_name: str,
                      dst_path_uuid: str, new_name: str) -> bool:
        """原地移动文件记录到另一目录 (保留 file.id 与模型关联)。"""
        return _submit_write(_relocate_file_impl, src_path_uuid, old_name,
                             dst_path_uuid, new_name)

    # 文件关联的模型记录
    def update_file_models(self, path_uuid: str, filename: str,
                           checkpoints: list[dict[str, Any] | str] | None = None,
                           loras: list[dict[str, Any] | str] | None = None,
                           embeddings: list[dict[str, Any] | str] | None = None,
                           vaes: list[dict[str, Any] | str] | None = None) -> None:
        """整体修改某个文件使用的模型 (同一事务)

        参数为 None 表示该类模型保持不变; 传空列表表示清空该类。
        模型唯一键为 name + version + url (LoRA 另加 model_type),
        已存在则复用模型注册表记录, 否则新建。
        """
        _submit_write(_update_file_models_impl, path_uuid, filename,
                      checkpoints, loras, embeddings, vaes)

    def get_file_models(self, path_uuid: str,
                        filename: str) -> dict[str, list[dict[str, Any]]]:
        """获取某个文件关联的全部模型, 按种类分组 (LoRA 含 weight)。"""
        return _submit_read(_get_file_models_impl, path_uuid, filename)

    def get_files_by_info(self, info_id: int) -> list[dict[str, Any]]:
        """获取 info 的全部文件记录, 附带解析后的目录路径 (directory)"""
        return _submit_read(_get_files_by_info_impl, info_id)

    def list_files_by_path(self, path_uuid: str) -> list[dict[str, Any]]:
        """列出某路径节点下的全部文件记录"""
        return _submit_read(_list_files_by_path_impl, path_uuid)

    def search_files(self, spec: dict[str, Any], limit: int = 50,
                     after: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
        """按结构化条件搜索文件记录 (基本/高级搜索共用, 供 Web 层分页)。

        spec 支持键 (均缺省为空): content, paths, path_include,
        path_exclude, name_include, name_exclude, prompt_include,
        prompt_exclude, neg_prompt_include, neg_prompt_exclude,
        file_types, sha256, tool, sampler, scheduler, seed, rating, note,
        size_ranges, width_ranges, height_ranges, time_ranges, ckpt, lora,
        embed。同一数组内多元素取 OR, 不同条件间取 AND。

        after: 上一页最后一个对象 {name, sha256, path}。
        返回项: name/sha256/rating/file_size/add_time/path/info_id/path_uuid。
        """
        return _submit_read(_search_files_impl, spec, max(1, min(int(limit), 200)),
                            after)

    def delete_file(self, path_uuid: str, filename: str) -> bool:
        """删除单条文件记录 (仅删 file 表, 不触碰 info 与模型记录) """
        return _submit_write(_delete_file_impl, path_uuid, filename)

    def delete_files_by_info(self, info_id: int) -> int:
        """删除 info 名下全部文件记录 (仅删 file 表), 返回删除条数。"""
        return _submit_write(_delete_files_by_info_impl, info_id)

    # 模型查询 (去重与反查) 
    def list_model_names(self, kind: str) -> list[dict[str, Any]]:
        """列出某种模型的去重名称及关联文件数量。

        kind: checkpoint / lora / embedding / vae
        返回: [{"name", "file_count"}, ...]
        """
        return _submit_read(_list_model_names_impl, kind)

    def find_infos_by_model(self, name: str) -> list[dict[str, Any]]:
        """按模型名称反查: 经 file_* 中间表关联 info 与 file。

        返回 list, 每项为 info 字段 + matched_models (命中的模型记录)
        + files(完整路径在 directory 字段). 注意: files 可能为空列表,
        (info 存在但无关联文件) 由应用层自行处理。
        """
        return _submit_read(_find_infos_by_model_impl, name)

    # 主表孤儿记录清理
    def count_orphan_info(self) -> int:
        """统计 file 表中已无任何关联文件的 info 记录数量 (供用户确认) """
        return _submit_read(_count_orphan_info_impl)

    def cleanup_orphan_info(self) -> int:
        """删除已无关联文件的 info 记录 (模型表经 CASCADE 联动清理), 返回删除数。"""
        return _submit_write(_cleanup_orphan_info_impl)
