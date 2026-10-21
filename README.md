# Genesis · 源纪

Genesis 是一个面向本地 AI 绘画作品的图片画廊：FastAPI 提供后端与 Web 界面，自动读取图片中的生成参数，按目录浏览、搜索、编辑元数据，并支持缩略图、外部目录挂载与只读模式。

---

## 目录

- [主要特性](#主要特性)
- [元数据解析支持](#元数据解析支持)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [启动参数](#启动参数)
- [运行与停止](#运行与停止)
- [数据目录](#数据目录)
- [配置](#配置)
- [外部目录挂载](#外部目录挂载)
- [前端开发](#前端开发)
- [API 与文档](#api-与文档)
- [常见问题](#常见问题)
- [AI 辅助开发](#ai-辅助开发)

---

## 主要特性

**浏览与展示**

- 基于 FastAPI + Vue 3 的 Web 界面，启动后通过浏览器访问；配合 `--host 0.0.0.0` 可供局域网内多设备同时使用。
- 缩略图按需生成并缓存，缩略图短边与网格单元大小均可调（64–1024 px）。
- 图片查看器支持滚轮缩放、拖动、旋转、上一张/下一张；连续快速缩放时自动增大缩放步进。
- 分级标识（G / PG-13 / R / X / XXX）：可按等级过滤、对敏感图片模糊处理、启用仅显示 G 级的安全浏览模式。
- 文件夹与图片在同一网格中顺次排列；支持在空白处右键新建文件夹或上传文件。

**管理与检索**

- 目录浏览、游标分页、目录变更自动刷新（SSE）。
- 基本搜索与高级搜索：提示词、文件名、路径、文件类型、大小/宽高/时间区间、分级、工具、采样器、调度器、Checkpoint / LoRA / Embedding 等条件。
- 基本搜索的目录模式：`$p:"/123"`（根目录下）、`$p:"*/123"`（任意层级）、`$p:"/123/*"`（含子目录）；可连续写多个引号模式取 OR，如 `$p:"/123" "*/test"`。
- 上传支持拖拽、文件选择、文件夹选择；文件夹可选择扁平或树状结构上传，同名目录可合并或自动新建 `(N)`。
- 文件/文件夹重命名、移动、删除；多选批量下载（流式 ZIP）。
- 编辑图片元数据：提示词、生成参数、分级、备注，以及按文件绑定的模型信息（Checkpoint / LoRA / LyCORIS / Embedding / VAE，LoRA 可单独设置权重）。
- 右键菜单支持「重新读取元数据」「复制提示词」等快捷操作。

**系统与数据**

- 所有文件操作由统一的文件管理模块负责，目录变更通过 watchdog 实时同步到数据库。
- 支持只读模式运行（`--read_only`），拒绝一切文件写入。
- 支持将外部目录挂载到图库中的空目录（类似 Linux mount），并可设置只读。
- SQLite 数据库带版本管理：启动时自动升级，升级前回写 WAL 并备份到 `data/db/db_backup/`。
- 写空闲一段时间后可自动执行 WAL checkpoint（默认 10 分钟，可配置）。
- Ctrl+C / 关闭终端时按顺序优雅退出：停止 Web 服务 → 等待缩略图队列处理完毕 → 停止目录监控 → 关闭数据库。

---

## 元数据解析支持

| 工具 | PNG | JPG | WEBP |
|---|:---:|:---:|:---:|
| A1111 WebUI | ✅ | ✅ | ✅ |
| LibLibAI | ✅ | | |
| Civitai 在线生成 | | ✅ | |

说明：

- A1111 家族通过 PNG 文本块 `parameters` 或 JPEG/WEBP 的 EXIF `UserComment` 读取提示词与参数。
- LibLibAI 额外支持 PNG 的 `AIGC` 块。
- Civitai 的存储格式可能随平台更新而变化；解析失败时图片仍会正常入库，只是缺少生成参数。
- ComfyUI 原生只保存工作流，不保存提示词。大型工作流通常无法提取；如需兼容，可使用 [ComfyUI Prompt Reader Node](https://github.com/receyuki/comfyui-prompt-reader-node) 中的 Prompt Saver 节点写出标准 infotext。
- 无法解析生成参数的图片仍会入库，并通过 Pillow 获取分辨率与文件大小。

---

## 环境要求

| 依赖 | 版本 | 说明 |
|---|---|---|
| Python | `>=3.10, <3.13`（推荐 3.12） | 运行后端 |
| Node.js | `>=24.12.0`（推荐 24.12.0） | 仅修改/构建前端时需要 |

后端依赖见 `requirements.txt`：FastAPI、Uvicorn、python-multipart、Pillow、watchdog、zipstream-ng。

---

## 快速开始

```bash
git clone https://github.com/DriveSand/Genesis.git
cd Genesis
```

创建并激活虚拟环境：

**Windows**

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

**Linux / macOS**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

启动后打开 <http://127.0.0.1:5580/>。首次运行会自动创建 `gallery/` 与 `data/`，将图片放入 `gallery/` 即可开始使用。

> 也可以在项目上级目录放置嵌入式 Python，并使用其解释器启动 `main.py`。

默认情况下程序会自动打开浏览器；使用 `--headless_mode` 可禁用该行为（适合服务器或后台运行）。

---

## 启动参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址；设为 `0.0.0.0` 可供局域网访问 |
| `--port` | `5580` | 监听端口 |
| `--read_only` | 关闭 | 只读模式：禁止创建、修改、删除、移动、上传等文件写操作 |
| `--headless_mode` | 关闭 | 启动后不自动打开浏览器 |
| `--log_level` | `warning` | **写入日志文件**的最低等级（debug/info/warning/error/critical）；控制台始终输出全部等级 |
| `--generate_image_cache` | 关闭 | 启动后在后台为图库中全部图片预生成缩略图 |
| `--generate_video_cache` | 关闭 | 预留参数；当前版本尚未实现视频缩略图生成 |

示例：

```bash
python main.py --host 0.0.0.0 --port 8080
```

---

## 运行与停止

- 日志：控制台 + 项目根目录 `genesis.log`；日志中的模块名会显示为完整模块路径，例如 `[database.service]`、`[parsers.a1111]`。
- 停止：在运行终端按 `Ctrl+C`，或关闭终端窗口。程序会依次：
  1. 停止 FastAPI（结束监听与在途请求）；
  2. 等待缩略图队列中的任务全部完成；
  3. 停止目录监控；
  4. 关闭数据库；
  5. 退出进程。
- SSE 等长连接不会阻止退出：收到停止信号后服务端主动结束事件流；FastAPI 停止时最多等待 3 秒，随后强制关闭剩余连接（关闭阶段的取消噪声不会写入日志）。
- Windows 关闭控制台时系统约 5 秒后强制结束进程；程序会在该时限内尽力完成清理。

---

## 数据目录

首次启动后项目结构大致如下：

```text
Genesis/
├─ gallery/                  # 图库根目录（图片与文件夹）
├─ data/
│  ├─ config.json            # 全局设置（首次启动自动生成）
│  ├─ db/
│  │  ├─ genesis.db          # SQLite 数据库
│  │  └─ db_backup/          # 数据库升级前的自动备份
│  └─ cache/
│     ├─ image/              # 缩略图缓存（按 SHA256 分目录）
│     └─ upload/             # 上传临时文件
├─ external_folder.json      # 可选：外部目录挂载配置
├─ version.py                # 程序版本 / 数据库结构版本
├─ main.py                   # 启动入口
├─ scripts/                  # 后端模块
├─ docs/                     # 模块与接口文档
└─ web/                      # 前端源码与构建产物
   ├─ src/
   └─ dist/                  # 已构建前端（随仓库提供）
```

---

## 配置

全局设置在首次启动时写入 `data/config.json`，也可以在 Web 界面的「全局设置」中修改（修改会立即写回文件）。

| 键 | 默认值 | 说明 |
|---|---|---|
| `EnableThumbnail` | `true` | 使用缩略图预览 |
| `ThumbnailShortSideSize` | `512` | 缩略图短边像素长度（64–1024） |
| `GridCellSize` | `240` | 网格单元大小（64–1024） |
| `NewlyAddedImageLevel` | `1` | 新增图片的默认分级（0=G … 4=XXX） |
| `AutoRefresh` | `true` | 目录内容变化时自动刷新 |
| `ShowLevel` | `[0,1,2,3,4]` | 允许显示的图片等级 |
| `SafeBrowsingMode` | `false` | 仅显示 G 级图片 |
| `BlurLevel` | `2` | 开始模糊处理的等级（2=R） |
| `DatabaseIdleCheckpointMinutes` | `10` | 写空闲多少分钟后执行 WAL checkpoint（0=关闭，0–1440）；**重启程序生效** |

---

## 外部目录挂载

在项目根目录创建 `external_folder.json`（可参考 `external_folder.json.example`），将外部目录挂载到图库中的**空目录**（不能是根目录）：

```json
{
  "/example": {
    "physical_path": "D:\\Pictures\\Example",
    "readonly": false,
    "priority": 0
  }
}
```

| 字段 | 说明 |
|---|---|
| `physical_path` | 外部目录的绝对路径 |
| `readonly` | 是否只读（禁止在该挂载点及其子目录内写入文件） |
| `priority` | 物理路径冲突时的优先级，越小越优先 |

该文件仅在**启动时读取一次**；修改后需重启程序。加载时会逐级创建对应的逻辑路径记录：

- 目标逻辑目录不存在：自动创建并挂载；
- 目标逻辑目录已存在且为空：自动转换为挂载点；
- 目标逻辑目录非空：跳过该挂载项并记录错误。

---

## 前端开发

前端使用 HTML + Vue 3（组合式 API）+ TypeScript + Vite，源码位于 `web/`。

```bash
cd web
npm install
npm run build
```

- 开发调试：

```bash
cd web
npm run dev
```

开发服务器默认运行在 <http://127.0.0.1:5173>，并将 `/api` 代理到 `http://127.0.0.1:5580`。

---

## API 与文档

- 交互式接口文档（Swagger UI）：<http://127.0.0.1:5580/api/docs>
- 服务状态：`GET /api/v1/status`（程序版本、数据库版本、只读状态）
- 更详细的模块与接口说明见 `docs/`：
  - [Web API](docs/web_api.md)
  - [数据库](docs/database.md)
  - [配置管理](docs/config_manager.md)
  - [日志](docs/logger.md)
  - [异常定义](docs/exception.md)

---

## 常见问题

**端口被占用？**

使用 `--port` 指定其他端口：

```bash
python main.py --port 8080
```

**启动时数据库正在升级？**

程序会在启动阶段同步完成数据库迁移，迁移期间不会启动文件管理、缩略图或 Web 服务。迁移前会自动备份到 `data/db/db_backup/db_<版本>_<时间>.db`；升级失败会整体回滚。

**图片没有提示词？**

说明图片内没有可解析的生成元数据，或来自不支持的生成工具。图片仍会入库并显示分辨率；可以在查看器中手动「编辑信息」。

**如何只允许查看、不允许修改？**

使用 `--read_only` 启动。此时所有文件写操作（上传、重命名、移动、删除等）都会被拒绝。

**局域网其他设备访问不了？**

使用 `--host 0.0.0.0` 启动，并确认防火墙允许对应端口。请仅在可信网络中使用。

---

## AI 辅助开发

本项目使用 AI 辅助开发：

- 工具：Claude Code
- 模型：Deepseek、Qwen
