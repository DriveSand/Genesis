"""database/upgrade.py —— 数据库结构版本检查与升级。

版本号由项目根目录 version.py 的 DATABASE_VERSION 统一提供; 库内版本记录
在 SQLite 的 PRAGMA user_version 中。启动时:
- 库版本 < 期望版本: 按 _MIGRATIONS 逐版本升级;
- 库版本 > 期望版本: 抛 DatabaseVersionError, 程序拒绝启动;
- 旧程序创建的库 user_version 为 0, 从 0 开始逐版本迁移。

迁移与建表均在写线程的单个事务内执行, 失败自动回滚。
"""
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from exceptions import DatabaseVersionError
from scripts.database import creation
from scripts.logger import log
from version import DATABASE_VERSION

# 当前程序实现的数据库结构版本 (唯一来源: version.py) 。
DB_VERSION = DATABASE_VERSION


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone() is not None


# v2 时代的模型表结构 (v3 会将其重命名为 checkpoint/lora/embedding/vae)。
# 迁移 v1 -> v2 时必须按当时的表名建表, 不能直接使用当前版本的建表脚本。
_V2_MODEL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS model_checkpoint (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);
CREATE TABLE IF NOT EXISTS model_lora (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type INTEGER NOT NULL CHECK(model_type IN (0,1)),
    name       TEXT NOT NULL,
    version    TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    UNIQUE(model_type, name, version, url)
);
CREATE TABLE IF NOT EXISTS model_embedding (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);
CREATE TABLE IF NOT EXISTS model_vae (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);
CREATE TABLE IF NOT EXISTS file_checkpoint (
    file_id       INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    checkpoint_id INTEGER NOT NULL REFERENCES model_checkpoint(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS file_lora (
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    lora_id INTEGER NOT NULL REFERENCES model_lora(id) ON DELETE CASCADE,
    weight  REAL NOT NULL,
    PRIMARY KEY(file_id, lora_id)
);
CREATE TABLE IF NOT EXISTS file_embedding (
    file_id      INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    embedding_id INTEGER NOT NULL REFERENCES model_embedding(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, embedding_id)
);
CREATE TABLE IF NOT EXISTS file_vae (
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    vae_id  INTEGER NOT NULL REFERENCES model_vae(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, vae_id)
);
"""

_V2_MODEL_SCHEMA_STATEMENTS = [
    s.strip() for s in _V2_MODEL_SCHEMA_SQL.split(";") if s.strip()
]


def _ensure_v2_model_schema(conn: sqlite3.Connection) -> None:
    """创建 v2 版本的模型注册表与中间表 (旧表名)。"""
    for statement in _V2_MODEL_SCHEMA_STATEMENTS:
        conn.execute(statement)


def _migrate_v1(conn: sqlite3.Connection) -> None:
    """v0 -> v1: 把 info 表的 width/height/file_size 迁移到 file 表。

    幂等: 已迁移的库不会重复执行; 列已存在时跳过 ALTER。
    """
    file_columns = {r[1] for r in conn.execute("PRAGMA table_info(file)")}
    for column in ("width", "height", "file_size"):
        if column not in file_columns:
            conn.execute(
                f"ALTER TABLE file ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    info_columns = {r[1] for r in conn.execute("PRAGMA table_info(info)")}
    if {"width", "height", "file_size"} <= info_columns:
        conn.execute(
            "UPDATE file SET "
            "width = COALESCE((SELECT width FROM info WHERE info.id = file.info_id), 0), "
            "height = COALESCE((SELECT height FROM info WHERE info.id = file.info_id), 0), "
            "file_size = COALESCE((SELECT file_size FROM info WHERE info.id = file.info_id), 0)"
        )
        for column in ("width", "height", "file_size"):
            try:
                conn.execute(f"ALTER TABLE info DROP COLUMN {column}")
            except sqlite3.OperationalError:
                # 旧版本 SQLite 不支持 DROP COLUMN: 保留旧列不影响新代码
                log.warning(f"Could not drop legacy column info.{column}")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v1 -> v2: 模型从 info 级迁移到 file 级。

    - file 表增加自增主键 id (SQLite 无法 ALTER 增加主键, 需重建表);
    - 旧 model_* 表重命名为 old_model_*, 新建模型注册表与四张 file_* 中间表;
    - 每条旧 info 级模型记录复制到该 info 的所有文件;
    - LoRA/LyCORIS 的 weight 移入 file_lora;
    - 模型唯一键: name + version + url (LoRA 另加 model_type), 均去除首尾空格。

    兼容性处理: 旧版本可能存在"半迁移"数据库 (file_* 中间表已被提前创建,
    但仍引用旧的 file 结构; 或残留 old_file / old_model_* 表)。这些残留对象
    在旧结构下不可能有有效数据, 先清理, 避免 ALTER TABLE RENAME 的外键重写
    导致后续 "no such table" 错误。
    """
    file_columns = {r[1] for r in conn.execute("PRAGMA table_info(file)")}

    def _drop_leftover_junctions() -> None:
        for name in ("file_checkpoint", "file_lora",
                     "file_embedding", "file_vae"):
            if _table_exists(conn, name):
                log.warning("Dropping leftover table from an incomplete "
                            f"migration: {name}")
                conn.execute(f"DROP TABLE {name}")

    # 残留的 old_file: 若当前 file 仍是旧结构, 以 old_file 为原始数据恢复;
    # 若当前 file 已有 id, old_file 只是可删除的残留副本。
    if _table_exists(conn, "old_file"):
        old_columns = {r[1] for r in conn.execute("PRAGMA table_info(old_file)")}
        if "id" not in file_columns:
            _drop_leftover_junctions()
            conn.execute("DROP TABLE file")
            conn.execute("ALTER TABLE old_file RENAME TO file")
            file_columns = old_columns
        else:
            conn.execute("DROP TABLE old_file")

    if "id" not in file_columns:
        # 旧 file 结构下提前创建的中间表无有效数据, 且会在 RENAME 时被
        # 改写为引用 old_file; 必须先删除 (ensure_schema 之后会重建)
        _drop_leftover_junctions()
        conn.execute("ALTER TABLE file RENAME TO old_file")
        conn.execute(
            """
            CREATE TABLE file (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                info_id   INTEGER NOT NULL REFERENCES info(id) ON DELETE CASCADE,
                path_uuid TEXT NOT NULL REFERENCES path(uuid),
                filename  TEXT NOT NULL,
                mime_type TEXT NOT NULL,
                added_at  INTEGER NOT NULL,
                width     INTEGER NOT NULL DEFAULT 0,
                height    INTEGER NOT NULL DEFAULT 0,
                file_size INTEGER NOT NULL DEFAULT 0,
                UNIQUE(path_uuid, filename)
            )
            """)
        conn.execute(
            "INSERT INTO file (info_id, path_uuid, filename, mime_type, "
            "added_at, width, height, file_size) "
            "SELECT info_id, path_uuid, filename, mime_type, added_at, "
            "width, height, file_size FROM old_file ORDER BY rowid")
        # 先删除旧表, 释放旧索引名 (否则 ensure_schema 的 IF NOT EXISTS
        # 会误判索引已存在, 新 file 表将缺少索引)
        conn.execute("DROP TABLE old_file")

    model_tables = ("model_checkpoint", "model_lora",
                    "model_embedding", "model_vae")
    for table in model_tables:
        old_table = f"old_{table}"
        columns = ({r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                   if _table_exists(conn, table) else set())
        if _table_exists(conn, old_table):
            # 残留 old_model_*: 当前表若仍是旧结构则以其为迁移源, 丢弃残留;
            # 当前表若已是新注册表则保留, old_model_* 作为迁移源。
            if "info_id" in columns:
                conn.execute(f"DROP TABLE {old_table}")
                conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
        elif columns:
            conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")

    # v2 版本的模型注册表与中间表 (v3 迁移会将其重命名)
    _ensure_v2_model_schema(conn)

    # 模型注册表: 仅保留仍被至少一个文件引用的旧模型
    if _table_exists(conn, "old_model_checkpoint"):
        conn.execute(
            "INSERT OR IGNORE INTO model_checkpoint (name, version, url) "
            "SELECT TRIM(o.name), TRIM(COALESCE(o.version, '')), "
            "       TRIM(COALESCE(o.url, '')) "
            "FROM old_model_checkpoint o "
            "WHERE EXISTS (SELECT 1 FROM file f WHERE f.info_id = o.info_id)")
        conn.execute(
            "INSERT OR IGNORE INTO file_checkpoint (file_id, checkpoint_id) "
            "SELECT f.id, m.id FROM old_model_checkpoint o "
            "JOIN file f ON f.info_id = o.info_id "
            "JOIN model_checkpoint m "
            "  ON m.name = TRIM(o.name) "
            " AND m.version = TRIM(COALESCE(o.version, '')) "
            " AND m.url = TRIM(COALESCE(o.url, ''))")

    if _table_exists(conn, "old_model_lora"):
        conn.execute(
            "INSERT OR IGNORE INTO model_lora "
            "(model_type, name, version, url) "
            "SELECT o.model_type, TRIM(o.name), TRIM(COALESCE(o.version, '')), "
            "       TRIM(COALESCE(o.url, '')) "
            "FROM old_model_lora o "
            "WHERE EXISTS (SELECT 1 FROM file f WHERE f.info_id = o.info_id)")
        conn.execute(
            "INSERT OR IGNORE INTO file_lora (file_id, lora_id, weight) "
            "SELECT f.id, m.id, o.weight FROM old_model_lora o "
            "JOIN file f ON f.info_id = o.info_id "
            "JOIN model_lora m "
            "  ON m.model_type = o.model_type "
            " AND m.name = TRIM(o.name) "
            " AND m.version = TRIM(COALESCE(o.version, '')) "
            " AND m.url = TRIM(COALESCE(o.url, ''))")

    for table, junction, column in (
            ("embedding", "file_embedding", "embedding_id"),
            ("vae", "file_vae", "vae_id")):
        old_table = f"old_model_{table}"
        if not _table_exists(conn, old_table):
            continue
        conn.execute(
            f"INSERT OR IGNORE INTO model_{table} (name, version, url) "
            "SELECT TRIM(o.name), TRIM(COALESCE(o.version, '')), "
            "       TRIM(COALESCE(o.url, '')) "
            f"FROM {old_table} o "
            "WHERE EXISTS (SELECT 1 FROM file f WHERE f.info_id = o.info_id)")
        conn.execute(
            f"INSERT OR IGNORE INTO {junction} (file_id, {column}) "
            f"SELECT f.id, m.id FROM {old_table} o "
            "JOIN file f ON f.info_id = o.info_id "
            f"JOIN model_{table} m "
            "  ON m.name = TRIM(o.name) "
            " AND m.version = TRIM(COALESCE(o.version, '')) "
            " AND m.url = TRIM(COALESCE(o.url, ''))")

    for table in model_tables:
        old_table = f"old_{table}"
        if _table_exists(conn, old_table):
            conn.execute(f"DROP TABLE {old_table}")
    log.info("Database schema migrated: models are now bound to files")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v2 -> v3: 模型注册表去掉 model_ 前缀, 并新增 upscale 模型。

    SQLite 的 ALTER TABLE RENAME 会同步更新 file_* 中间表中的外键引用,
    因此已有文件-模型关联保持不变。新表/索引由 bootstrap 末尾的
    creation.ensure_schema 统一补齐。
    """
    for old, new in (("model_checkpoint", "checkpoint"),
                     ("model_lora", "lora"),
                     ("model_embedding", "embedding"),
                     ("model_vae", "vae")):
        if not _table_exists(conn, old):
            continue
        if _table_exists(conn, new):
            log.warning("Both legacy and new model tables exist, keeping new: "
                        f"{old} / {new}")
            continue
        conn.execute(f"ALTER TABLE {old} RENAME TO {new}")
        log.info(f"Model table renamed: {old} -> {new}")
    log.info("Database schema migrated: model tables renamed; upscale added")


# 结构迁移表: 目标版本 -> 迁移函数 (在写事务内执行)。
# 版本 0 为未记录版本号的历史库; 版本 3 为当前基线。
_MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migrate_v1,
    2: _migrate_v2,
    3: _migrate_v3,
}


def migration_needed(db_path: Path,
                     expected_version: int) -> tuple[bool, int]:
    """检查是否需要迁移, 返回 (是否需要, 当前库版本)。

    仅当 info 表已存在且库版本低于期望版本时需要迁移。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, "info"):
            return False, 0
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return current < int(expected_version), current
    finally:
        conn.close()


def backup_database(db_path: Path, version: int) -> Path:
    """迁移前备份主库: db_backup/db_<版本>_<YYYY-MM-DD HH-MM-SS>.db。

    调用方必须已执行 WAL checkpoint; 本函数复制的是主库文件本身。
    """
    db_path = Path(db_path)
    backup_dir = db_path.parent / "db_backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    target = backup_dir / f"db_{version}_{stamp}.db"
    suffix = 1
    while target.exists():
        target = backup_dir / f"db_{version}_{stamp}_{suffix}.db"
        suffix += 1
    tmp = target.with_name(target.name + ".tmp")
    try:
        shutil.copy2(db_path, tmp)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    log.info(f"Database backup created before migration: {target}")
    return target


def bootstrap(conn: sqlite3.Connection,
              expected_version: int = DB_VERSION) -> None:
    """启动引导 (单个写事务) : 版本检查/升级 + 建表/补索引 + 根目录记录。

    - 空库: 按当前代码结构建表, 版本号直接记为 expected_version;
    - 已有库且版本 < 期望: 逐版本执行迁移; 版本 > 期望: 拒绝启动;
    - 旧版程序创建的库 user_version 为 0, 视为最早的历史版本。
    """
    expected_version = int(expected_version)
    if _table_exists(conn, "info"):
        current = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if current > expected_version:
            raise DatabaseVersionError(
                f"Database schema version {current} is newer than the "
                f"version recorded by the program ({expected_version}); "
                f"refusing to start")
        for version in range(current + 1, expected_version + 1):
            migration = _MIGRATIONS.get(version)
            if migration is None:
                raise DatabaseVersionError(
                    f"No migration path from schema version {current} to "
                    f"{expected_version}")
            migration(conn)
            log.info(f"Database schema migrated to version {version}")
    elif expected_version < DB_VERSION:
        raise DatabaseVersionError(
            f"Program expects database version {expected_version}, but this "
            f"build creates version {DB_VERSION}; refusing to start")

    creation.ensure_schema(conn)
    conn.execute(f"PRAGMA user_version = {expected_version}")
    creation.ensure_root_path(conn)
