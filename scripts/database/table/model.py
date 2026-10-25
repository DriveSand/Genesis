"""模型表与文件-模型关联: checkpoint / lora / embedding / vae。

模型注册表 (model_*) 只保存模型自身; 文件与模型的关系由 file_checkpoint、
file_lora、file_embedding、file_vae 四张中间表维护, LoRA 的权重存放在中间表。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from exceptions import InputDataError
from scripts.logger import log

# 模型种类 -> 模型表名 (表名白名单, 查询值一律走 ? 参数绑定)
MODEL_TABLES = {
    "checkpoint": "model_checkpoint",
    "lora": "model_lora",
    "embedding": "model_embedding",
    "vae": "model_vae",
}

# 模型种类 -> (中间表, 模型外键列); LoRA 中间表另有 weight 列
MODEL_JUNCTIONS = {
    "checkpoint": ("file_checkpoint", "checkpoint_id"),
    "lora": ("file_lora", "lora_id"),
    "embedding": ("file_embedding", "embedding_id"),
    "vae": ("file_vae", "vae_id"),
}

LORA_MODEL_TYPES = (0, 1)  # 0=LoRA, 1=LyCORIS


def normalize_model_fields(row: dict[str, Any] | str,
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
        if model_type not in LORA_MODEL_TYPES:
            raise InputDataError(
                f"Invalid LoRA/LyCORIS model type: {model_type}")
        if row.get("weight") is None:
            raise InputDataError(f"LoRA entry is missing weight: {name}")
        result["model_type"] = model_type
        result["weight"] = float(row["weight"])
    return result


def model_identity(row: dict[str, Any], kind: str) -> tuple:
    """模型唯一键: name + version + url (LoRA 另加 model_type)。"""
    key = (row["name"], row["version"], row["url"])
    return (row["model_type"],) + key if kind == "lora" else key


def get_or_create_model(conn: sqlite3.Connection, kind: str,
                        row: dict[str, Any]) -> int:
    """按唯一键查找模型注册表记录, 不存在则新建, 返回模型 id。"""
    table = MODEL_TABLES[kind]
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


def sync_file_models(conn: sqlite3.Connection, file_id: int, kind: str,
                     rows: list[dict[str, Any] | str]) -> None:
    """整体替换单个文件在某类模型上的绑定。

    传入空列表表示清空该类; 同一提交内按唯一键去重 (重复视为输入错误)。
    """
    junction, column = MODEL_JUNCTIONS[kind]
    conn.execute(f"DELETE FROM {junction} WHERE file_id = ?", (file_id,))
    seen: set[tuple] = set()
    for raw in rows:
        row = normalize_model_fields(raw, kind)
        key = model_identity(row, kind)
        if key in seen:
            raise InputDataError(
                f"Duplicate model in a single submission: {row['name']}")
        seen.add(key)
        model_id = get_or_create_model(conn, kind, row)
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


def get_file_id(conn: sqlite3.Connection, path_uuid: str,
                filename: str) -> int:
    row = conn.execute(
        "SELECT id FROM file WHERE path_uuid = ? AND filename = ?",
        (path_uuid, filename),
    ).fetchone()
    if row is None:
        raise InputDataError(
            f"File record does not exist: {path_uuid}/{filename}")
    return int(row["id"])


def update_file_models(conn: sqlite3.Connection, path_uuid: str,
                       filename: str, checkpoints: list | None,
                       loras: list | None, embeddings: list | None,
                       vaes: list | None) -> None:
    """整体修改某个文件使用的模型。

    参数为 None 表示该类模型保持不变; 传空列表表示清空该类。
    """
    file_id = get_file_id(conn, path_uuid, filename)
    for kind, rows in (("checkpoint", checkpoints), ("lora", loras),
                       ("embedding", embeddings), ("vae", vaes)):
        if rows is not None:
            sync_file_models(conn, file_id, kind, rows)
    log.debug(f"File models updated: file_id={file_id}")


def get_file_models(conn: sqlite3.Connection, path_uuid: str,
                    filename: str) -> dict[str, list[dict[str, Any]]]:
    """获取某个文件关联的全部模型, 按种类分组。

    LoRA/LyCORIS 行的 weight 来自中间表。
    """
    file_id = get_file_id(conn, path_uuid, filename)
    result: dict[str, list[dict[str, Any]]] = {}
    for kind, table in MODEL_TABLES.items():
        junction, column = MODEL_JUNCTIONS[kind]
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


def list_model_names(conn: sqlite3.Connection,
                     kind: str) -> list[dict[str, Any]]:
    """列出某种模型的去重名称及关联文件数量 (仅统计仍被引用的模型)。"""
    if kind not in MODEL_TABLES:
        raise InputDataError(f"Invalid model kind: {kind}")
    table = MODEL_TABLES[kind]
    junction, column = MODEL_JUNCTIONS[kind]
    rows = conn.execute(
        f"SELECT m.name, COUNT(DISTINCT j.file_id) AS file_count "
        f"FROM {table} m JOIN {junction} j ON j.{column} = m.id "
        "GROUP BY m.name ORDER BY m.name"
    )
    return [dict(r) for r in rows]


def find_infos_by_model(conn: sqlite3.Connection,
                        name: str) -> list[dict[str, Any]]:
    """按模型名称反查: 通过 file_* 中间表找到关联文件, 再关联 info。

    全程使用同一读连接 (不回门面二次派发)。
    """
    # 延迟导入: info.py 在创建记录时会调用本模块, 避免模块级循环依赖
    from scripts.database.table.file import get_files_by_info
    from scripts.database.table.info import get_info

    name = str(name).strip()
    if not name:
        return []
    matched: list[dict[str, Any]] = []
    for kind, table in MODEL_TABLES.items():
        junction, column = MODEL_JUNCTIONS[kind]
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
        info = get_info(conn, info_id)
        if info is None:
            continue  # 理论上不会发生 (CASCADE)
        result.append({
            **info,
            "matched_models": matched_by_info[info_id],
            "files": get_files_by_info(conn, info_id),
        })
    return result
