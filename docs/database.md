# Database模块使用说明

## 使用库：标准库 sqlite3, 无 ORM

## 模块划分（`scripts/database/` 包）

| 文件 | 职责 |
|---|---|
| `creation.py` | 数据库创建：连接工厂、建表脚本与索引、根目录（ROOT）记录 |
| `upgrade.py` | 数据库升级：结构版本检查、`_MIGRATIONS` 逐版本迁移 |
| `service.py` | 对外服务：全部查询语句、读写引擎（写线程 / 读线程池）与 `DataBase` 门面 |
| `__init__.py` | 包入口，导出 `DataBase` / `DB_VERSION` / `init` / `close`（接口与旧版一致） |

版本号唯一来源为项目根目录的 `version.py`：
`PROGRAM_VERSION`（程序版本）与 `DATABASE_VERSION`（结构版本）。
`upgrade.DB_VERSION` 直接引用 `DATABASE_VERSION`。

## 并发架构（读/写分离）
- 写侧：单个后台线程持有唯一写连接；外部经线程安全 FIFO 队列提交任务，
  调用方通过 Future 获取结果或异常。所有写操作使用显式事务包裹
  （BEGIN IMMEDIATE / COMMIT / ROLLBACK，事务粒度 = 一个业务操作，
  多表写入在同一事务内）；事务内的"先查后改"一律使用写连接。
- 读侧：ThreadPoolExecutor(max_workers=4)，每个工作线程经
  threading.local() 独享一个只读连接，并发执行、无排队；
  读操作不开启显式事务，且连接以 PRAGMA query_only 强制只读。
- 禁止 check_same_thread=False，禁止跨线程共享连接对象。
- 每个连接创建时独立执行：
    PRAGMA journal_mode = WAL;
    PRAGMA synchronous = NORMAL;
    PRAGMA foreign_keys = ON;
- 关闭：close() 等待写队列清空、所有进行中任务完成后再关闭连接。

## 事务与查询约定
- 全部使用参数化查询; SQL 中的表名只来自模块内置白名单，不接受外部输入
- 时间戳统一为 10 位 Unix 时间戳
- 缩略图不入库, 由前端根据 SHA256 拼接路径向后端请求

## WAL 空闲合并（Checkpoint）
- 写空闲达到配置时长（`config.json` 的 `DatabaseIdleCheckpointMinutes`，
  默认 10 分钟，0 = 关闭）后，由数据库后台线程在写连接上执行
  `PRAGMA wal_checkpoint(TRUNCATE)`，把 WAL 内容合并回主库并截断 WAL 文件。
- "空闲"以最后一次**写操作**为准；读操作（如前端 2 秒一次的目录指纹查询）
  不会推迟合并。
- 配置在数据库初始化时读取一次，修改后需重启程序生效。

## 表结构要点
- `info`：内容级元数据（sha256、提示词、seed/steps/cfg、采样器/调度器/工具、分级、备注）
- `file`：单个文件记录（**自增 id**、info_id、path_uuid、filename、mime_type、
  added_at、**width / height / file_size**）。尺寸与大小随文件而非内容，
  同一内容的多份副本各自记录
- `path`：逻辑目录表
- 模型关系为 **info 1—N file**、**file N—N 模型**：
  - `model_checkpoint` / `model_lora` / `model_embedding` / `model_vae`
    为模型注册表，**不再保存 info_id / file_id**；
  - `file_checkpoint` / `file_lora` / `file_embedding` / `file_vae`
    为中间表；LoRA/LyCORIS 的 `weight` 存放于 `file_lora.weight`；
  - 唯一键（去除首尾空格后）：checkpoint / embedding / vae 为
    `name + version + url`；lora 另加 `model_type`。
- 文件重命名/移动采用原地更新，保留 `file.id` 与模型关联；目录移动只更新
  目录节点的 parent_uuid/name，子树文件与关联保持不变。

## 数据库版本与升级
- 版本号存于 SQLite 内置的 `PRAGMA user_version`；当前结构版本为 `DB_VERSION = 2`。
- 期望版本来自项目根目录的 `version.py` 的 `DATABASE_VERSION`。启动时：
  - 库版本 < 期望：按 `_MIGRATIONS` 逐版本升级
    （v0→v1 把 info 的 width/height/file_size 迁移到 file 表；
    v1→v2 引入 file.id、模型注册表与四张 file_* 中间表，并把旧 info 级
    模型复制到该 info 的所有文件，LoRA weight 迁入中间表）
  - 库版本 > 期望：抛 `DatabaseVersionError`（42113），程序拒绝启动
  - 旧程序创建的库 `user_version` 为 0，自动从 v0 逐版本迁移
- **迁移前备份**：先执行 `PRAGMA wal_checkpoint(TRUNCATE)` 回写 WAL，
  再把主库复制到 `db_backup/db_<当前版本>_<YYYY-MM-DD HH-MM-SS>.db`。
- 迁移函数在写线程的单个事务内执行，失败自动回滚；备份在迁移前独立完成。
- `DataBase` 初始化（含迁移）是同步阻塞的；`main.py` 在 `DataBase` 创建完成
  后才启动文件管理、缩略图与 Web 服务，因此迁移完成前不会启动依赖数据库的服务。

## 生命周期：模块级 init(db_path, expected_version, idle_checkpoint_minutes) / close()
```
from scripts.database import init, close
init("data/db/genesis.db")   # 建表 + 版本检查 + 根目录记录，幂等
...                          # 通过 DataBase 门面读写
close()                      # 程序退出时必须调用
```

## 兼容用法（接口与旧版一致）：
```
from scripts.database import DataBase

db = DataBase()          # 默认 FolderPath.DATABASE_FOLDER/genesis.db
info_id = db.create_info("ab12...", seed=0, steps=20)
db.add_file(info_id, path_uuid, "a.png", "image/png",
            width=1024, height=1024, file_size=204800)
db.update_file(path_uuid, "a.png", file_size=204800)  # 文件级字段更新
...
db.close()               # 等价于模块级 close()
```
