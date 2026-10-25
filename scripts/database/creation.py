"""database/creation.py —— 数据库结构创建。

职责:
- 建表脚本与索引 (幂等);
- 根目录记录 (ROOT) 的创建。

本模块只负责数据库结构的定义与创建, 不涉及连接管理:
连接工厂见 engine.py, 版本检查与升级见 upgrade.py, 对外门面见 service.py。
"""
import sqlite3
import uuid as uuid_lib

from scripts.logger import log

DB_FILE_NAME = "genesis.db"

# 根目录: parent_uuid 为 NULL (外键对 NULL 放行) , name 为 ROOT
ROOT_NAME = "ROOT"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS info (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256          TEXT NOT NULL UNIQUE,
    positive_prompt TEXT NOT NULL DEFAULT '',
    negative_prompt TEXT NOT NULL DEFAULT '',
    seed            INTEGER NOT NULL,
    steps           INTEGER NOT NULL,
    cfg             REAL NOT NULL,
    sampler         TEXT NOT NULL,
    scheduler       TEXT NOT NULL,
    tool            TEXT NOT NULL,
    rating          INTEGER NOT NULL CHECK(rating IN (0,1,2,3,4)),
    note            TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_info_created ON info(created_at);
CREATE INDEX IF NOT EXISTS idx_info_rating  ON info(rating);

CREATE TABLE IF NOT EXISTS path (
    uuid           TEXT PRIMARY KEY,
    parent_uuid    TEXT REFERENCES path(uuid)
                   CHECK (name = 'ROOT' OR parent_uuid IS NOT NULL),
    name           TEXT NOT NULL,
    status         TEXT NOT NULL CHECK(status IN ('normal', 'offline', 'conflict')),
    type           TEXT NOT NULL CHECK(type IN ('inbuilt', 'mount')),
    conflict_state TEXT CHECK(
        (status != 'conflict' AND conflict_state IS NULL) OR
        (status = 'conflict' AND conflict_state IN ('active', 'shadowed'))
    ),
    last_checked   TEXT,
    UNIQUE(parent_uuid, name)
);

CREATE INDEX IF NOT EXISTS idx_path_parent ON path(parent_uuid);

CREATE TABLE IF NOT EXISTS file (
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
);

CREATE INDEX IF NOT EXISTS idx_file_path ON file(path_uuid);
CREATE INDEX IF NOT EXISTS idx_file_info ON file(info_id);

-- 模型注册表: 同一 (name, version, url) 视为同一模型, 与文件解耦
CREATE TABLE IF NOT EXISTS model_checkpoint (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);

CREATE INDEX IF NOT EXISTS idx_model_ckpt_name ON model_checkpoint(name);

CREATE TABLE IF NOT EXISTS model_lora (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    model_type INTEGER NOT NULL CHECK(model_type IN (0,1)),
    name       TEXT NOT NULL,
    version    TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    UNIQUE(model_type, name, version, url)
);

CREATE INDEX IF NOT EXISTS idx_model_lora_name ON model_lora(name);

CREATE TABLE IF NOT EXISTS model_embedding (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);

CREATE INDEX IF NOT EXISTS idx_model_emb_name ON model_embedding(name);

CREATE TABLE IF NOT EXISTS model_vae (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    url     TEXT NOT NULL DEFAULT '',
    UNIQUE(name, version, url)
);

CREATE INDEX IF NOT EXISTS idx_model_vae_name ON model_vae(name);

-- 文件与模型的多对多中间表 (LoRA/LyCORIS 的权重属于文件-模型关系)
CREATE TABLE IF NOT EXISTS file_checkpoint (
    file_id       INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    checkpoint_id INTEGER NOT NULL REFERENCES model_checkpoint(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_fc_checkpoint ON file_checkpoint(checkpoint_id);

CREATE TABLE IF NOT EXISTS file_lora (
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    lora_id INTEGER NOT NULL REFERENCES model_lora(id) ON DELETE CASCADE,
    weight  REAL NOT NULL,
    PRIMARY KEY(file_id, lora_id)
);

CREATE INDEX IF NOT EXISTS idx_fl_lora ON file_lora(lora_id);

CREATE TABLE IF NOT EXISTS file_embedding (
    file_id      INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    embedding_id INTEGER NOT NULL REFERENCES model_embedding(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, embedding_id)
);

CREATE INDEX IF NOT EXISTS idx_fe_embedding ON file_embedding(embedding_id);

CREATE TABLE IF NOT EXISTS file_vae (
    file_id INTEGER NOT NULL REFERENCES file(id) ON DELETE CASCADE,
    vae_id  INTEGER NOT NULL REFERENCES model_vae(id) ON DELETE CASCADE,
    PRIMARY KEY(file_id, vae_id)
);

CREATE INDEX IF NOT EXISTS idx_fv_vae ON file_vae(vae_id);
"""

# 建表脚本按 ";" 拆分为单条语句 (脚本内无嵌套分号) ,
# 使整个建表过程可以纳入单个显式事务
_SCHEMA_STATEMENTS = [s.strip() for s in _SCHEMA_SQL.split(";") if s.strip()]


# ---------------------------------------------------------------- #
# 结构创建
# ---------------------------------------------------------------- #

def ensure_schema(conn: sqlite3.Connection) -> None:
    """执行建表脚本 (幂等) , 补全缺失的表与索引。"""
    for statement in _SCHEMA_STATEMENTS:
        conn.execute(statement)


def ensure_root_path(conn: sqlite3.Connection) -> str:
    """确保根目录记录存在, 返回根节点 uuid。"""
    row = conn.execute(
        "SELECT uuid FROM path WHERE parent_uuid IS NULL AND name = ?",
        (ROOT_NAME,),
    ).fetchone()
    if row is not None:
        return str(row["uuid"])
    root_uuid = str(uuid_lib.uuid4())
    conn.execute(
        "INSERT INTO path (uuid, parent_uuid, name, status, type) "
        "VALUES (?, NULL, ?, 'normal', 'inbuilt')",
        (root_uuid, ROOT_NAME),
    )
    log.debug(f"Root path record created: {root_uuid}")
    return root_uuid
