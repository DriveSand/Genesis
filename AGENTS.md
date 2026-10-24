# AGENTS.md

本文件为 Claude Code 在本仓库中工作时提供指引。

## 项目概述

Genesis（源纪）是一个自托管的 AI 绘画图库：FastAPI 后端 + Vue 3 + TypeScript SPA。它扫描图库目录，将图片入库到 SQLite，解析内嵌的生成元数据（A1111 / LibLibAI / Civitai / ComfyUI），生成 WebP 缩略图，并提供浏览、搜索、元数据编辑、上传、下载以及外部目录挂载功能。

文档在 `docs/` 目录下（中文），新增的说明文档都放置于此。

## 常用命令

环境要求：Python `>=3.10, <3.13`（推荐 3.12），Node.js `>=24.12`。

```bash
# 后端依赖（仓库根目录）
python -m venv .venv
source .venv/Scripts/activate        # Windows Git Bash；Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python main.py

# 前端：后端提供 UI 前必须先构建（web/dist 已被 gitignore）
cd web
npm install
npm run build

# 前端开发服务器：http://127.0.0.1:5173，将 /api 代理到 http://127.0.0.1:5580
npm run dev
```

启动参数（`python main.py --help`）：`--host`、`--port`（默认 5580）、`--read_only`、`--headless_mode`、`--log_level`、`--generate_image_cache`。

项目没有自动化测试、linter 或格式化工具。`test/` 已被 gitignore，仅存放解析测试素材和功能测试的临时文件。

## 目录结构

| 路径 | 说明 |
|---|---|
| `gallery/` | 图片目录 |
| `data/config.json` | 配置文件 |
| `data/db/genesis.db` | SQLite 数据库 |
| `data/cache/image` | 缩略图缓存 |
| `data/cache/upload` | 上传缓存 |
| `web/dist` | 前端构建产物（gitignored） |

以上数据目录自动创建且被 gitignore。

## 启动流程

`main.py` 按以下顺序组装：

1. 日志初始化 + 文件夹创建
2. 断言 `version.py:DATABASE_VERSION` 与 `scripts/database.py:DB_VERSION` 一致
3. `ConfigManager`（加载 `data/config.json`，创建默认值）
4. `DataBase`（创建/迁移 schema，迁移前备份）
5. `ConfigManager.load_external_folders(db)`（读取 `external_folder.json`）
6. `FileManager`（watchdog + 文件操作 + SHA256 池）
7. `ThumbnailGenerator`
8. `AppState` 注入 FastAPI（`create_app()`），启动 `uvicorn`
关闭顺序：停止服务器（设 `shutdown_event` 终止 SSE）→ 排空缩略图 → 停止 watchdog → 关闭 DB。

## 硬性模块边界

- **`scripts/file_manager.py`** — 唯一执行文件 I/O 的模块。缩略图生成、上传、下载、路由均通过它。（`parser_scheduler` 有一个显式例外：后台任务中只读 `Image.open`）
- **`scripts/path_resolver.py`** — 唯一负责逻辑路径与物理路径转换的模块。
- **`scripts/database/`** — 唯一执行 SQL 的地方。其他模块通过 `DataBase` 门面调用。
- **`scripts/config_manager.py`** — 唯一读写配置的入口。
- **`scripts/parser_scheduler.py`** — 唯一元数据/EXIF 解析入口，唯一允许导入 `scripts/parsers/` 的模块。
- `FileManager._db_lock`（RLock，与 `PathResolver` 共享）序列化文件层的所有 DB 访问。
- 写操作提交到单写线程；读操作在 4 线程池运行（线程局部只读连接）；两者阻塞于一个 Future。**禁止自行创建 SQLite 连接或在线程间共享连接。**

## 路径约定（容易出错）

| 上下文 | 路径格式 | 示例 |
|---|---|---|
| Web API 查询/JSON | gallery 相对路径，前导 `/`，无前缀 | `/test/a.png` |
| `FileManager` 公共方法 | 完整逻辑路径，必须以 `/gallery` 或 `/cache` 开头 | `/gallery/test/a.png` |
| `PathResolver` 公共方法 | gallery 相对路径，前导 `/` | `/test` |
| `PathResolver.walk_segments()`（内部） | 返回 `/gallery` 前缀的逻辑路径 | `/gallery/test` |
| 缩略图 | SHA 寻址缓存路径 | `/cache/image/{sha256}/{short_side}.webp` |

`/gallery` 基于数据库（路径表）且感知挂载点；`/cache` 直接映射到 `data/cache`，不被监控也不存入 DB。两个根目录均拒绝 `.` / `..` 路径段。物理路径解析优先选择最深配置的挂载点，否则使用 `GALLERY_ROOT`；`destination="local"` 强制使用本地影子目录。

## 文件系统同步与入库

`FileManager` 对 `/gallery` 及每个挂载运行递归 watchdog 观察器。经 `FileManager` 的写操作会记入去重白名单，观察器忽略自身事件。

外部变更流程：

```
watchdog 事件 → SHA256（8 MB 分块，mtime 变化则重试）→ 按 SHA 复用/创建 info → 创建 file 行（用 Pillow 探测尺寸）→ 后台解析元数据
```

- 新文件需稳定（大小 + mtime 不变）后才入库，空文件不入库。
- 启动时执行磁盘与 DB 的对账，捕获程序关闭期间的外部变更。
- 挂载点每 30 秒健康检查，5 秒无响应标记为 `offline` 并轮询恢复。
- **新增 `/gallery` 下的写入路径时，必须通过 `FileManager`，否则需自行处理白名单/入库逻辑。**

## 元数据解析器

`parser_scheduler.read_raw_metadata()` 一次性读取容器（PNG text chunks 或 JPEG/WEBP EXIF `UserComment`）到 `RawMetadata`；解析器接收该对象，**禁止执行文件 I/O 和 SQL**。

注册表在 `scripts/parsers/__init__.py`：第一个 `can_parse()` 返回 true 的模块拥有解析权，失败不级联到后续解析器。ComfyUI 优先但 fallback 到 A1111 infotext 补全缺失字段。`parse_metadata()` 永不抛异常；无法解析的图片仍以分辨率/大小入库。

解析输出键：`positive_prompt`、`negative_prompt`、`seed`、`steps`、`cfg`、`sampler`、`scheduler`、`note`、`models: {checkpoints, loras, embeddings, vaes}`。`FileManager._apply_metadata` 在写入前按白名单过滤字段。Sampler/scheduler 名称在 `parser_scheduler._normalize_generation_params` 中统一标准化。

新增解析器：实现 `TOOL_NAME` / `can_parse(raw)` / `parse(raw)`，在 `_PARSERS` 中按优先级插入并导入。

## 数据库

SQLite，无 ORM。建表在 `scripts/database/creation.py`；迁移在 `scripts/database/upgrade.py`（`_MIGRATIONS`，按目标版本索引），在单个写事务中执行。迁移前会 checkpoint WAL 并将数据库复制到 `data/db/db_backup/`。若数据库 schema 版本高于程序版本，拒绝启动。

`version.py` 是唯一版本来源：`PROGRAM_VERSION` 与 `DATABASE_VERSION`。改 schema 需：更新 `creation.py` 的 `_SCHEMA_SQL`、递增 `DATABASE_VERSION`、追加迁移函数。

核心表结构：
- `info`：内容级（sha256 → prompts, seed/steps/cfg, sampler, rating, note）
- `file`：物理副本（path_uuid, filename, mime, added_at, width/height/file_size）
- 模型单独注册，通过 `file_checkpoint` / `file_lora`(含权重) / `file_embedding` / `file_vae` 绑定到文件
- 重命名/移动就地更新行，`file.id` 及模型绑定不受影响

## Web 后端

路由在 `scripts/web_backend/routers/`，在 `scripts/web_backend/app.py` 中显式注册。每个处理器通过 `request.app.state.genesis` 获取依赖（`AppState`：config, db, fm, thumb, read_only, upload registry, shutdown event）。请求体为 `schemas.py` 中的 Pydantic v2 模型。

- API 前缀 `/api/v1`，Swagger 文档在 `/api/docs`
- 错误在 `errors.py` 中统一映射为 `{"error": {"code", "message"}}`
- 分页使用不透明 base64 JSON 游标（`helpers.encode_cursor`），客户端原样回传 `current_cursor`
- 目录刷新走 SSE（`/api/v1/directories/scan`）：服务端每 2 秒轮询目录指纹并发送 `event: change`。`state.shutdown_event` 停止流以确保优雅关闭不挂起
- 前端构建产物在 `web/dist` 直接托管；缺少 dist 时为纯 API 模式，手动 UI 测试前需执行 `npm run build`
- `--read_only` 会以 403 拒绝 `/gallery` 写入；`/cache` 保持可写。挂载点级 `readonly` 继承至子树。在读写挂载点内写入但未指定 `destination` 时抛出 `UserChoiceRequired`（409 + `options: ["mount", "local"]`）

## 前端

Vue 3 组合式 API + TypeScript + Vite，单页应用。无路由库、无状态库：`web/src/store.ts` 是唯一 `reactive()` 状态 store，`App.vue` 根据 store 标志条件渲染组件。`main.ts` 在 `/view?image=<path>` 独立查看器窗口中挂载 `ViewerApp.vue` 替代 `App.vue`。`api.ts` 为 API 客户端；`types.ts` 持 API 类型定义。UI 字符串为中文。

设置默认值在以下三处重复：`web/src/store.ts`（`DEFAULT_SETTINGS`）、`scripts/config_manager.py`（`DEFAULT_CONFIG`）、`routers/settings.py`（`_validate_change`）。新增用户可编辑设置必须在三处同步添加。

## 扩展检查清单

| 变更类型 | 操作步骤 |
|---|---|
| 新接口 | 在 `routers/` 下添加路由 → `app.py` 注册 → `schemas.py` 定义请求体 → 更新 `docs/web_api.md` |
| 新解析器 | 参考「元数据解析器」章节；用 `test/image/*.png|jpg` + `.txt` 样本手动对比 |
| Schema 变更 | `creation.py` + `upgrade.py` 迁移 + `DATABASE_VERSION` 递增 |
| 新配置项 | `DEFAULT_CONFIG` + `_validate_change` + 前端 `Settings` 类型 + `DEFAULT_SETTINGS` |
| 文件操作 | 扩展 `FileManager`；路由层禁止直接操作文件系统 |
