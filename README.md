
![Genesis](https://img.shields.io/badge/Genesis-源纪-6C5CE7?style=flat-square)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Vue](https://img.shields.io/badge/Vue%203-TypeScript-4FC08D?style=flat-square&logo=vue.js&logoColor=white)
![License](https://img.shields.io/badge/License-GPLv3-lightgrey?style=flat-square)

# Genesis · 源纪

> 面向 AI 绘画作品的自托管图片画廊

**Genesis** 支持自动读取图片内嵌的生成参数，提供按目录浏览、全文检索、元数据编辑等能力，并支持缩略图缓存、外部目录挂载与只读模式


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
- [许可](#许可)

---

## 主要特性

### 浏览与展示

- **现代 Web 界面**：FastAPI + Vue 3
- **缩略图预览**：缩略图自动生成并缓存，短边尺寸与网格单元大小均可调（范围: 64–1024 px）
- **专业图片查看器**：支持滚轮缩放、拖动、旋转、上一张 / 下一张
- **分级体系（G / PG-13 / R / X / XXX）**：按等级过滤、对敏感图片模糊处理，并可开启「仅显示 G 级」的安全浏览模式
- **一体化网格**：文件夹与图片同网格顺排；空白处右键即可新建文件夹或上传文件

### 管理与检索

- **实时同步**：目录浏览、游标分页，目录变更时自动刷新
- **强大的搜索**：基本 + 高级搜索，可按提示词、文件名、路径、类型、大小 / 宽高 / 时间、分级、工具、采样器、调度器、Checkpoint / LoRA / Embedding 等条件组合筛选
- **目录模式搜索（仅基本搜索有效）**：`$p:"/123"`（根目录下）、`$p:"*/123"`（任意层级）、`$p:"/123/*"`（含子目录）；可连写多个引号模式取 OR
- **灵活上传**：支持拖拽、文件 / 文件夹选择，文件夹可按扁平或树状上传，同名目录可合并或自动新建
- **完整文件操作**：重命名、移动、删除，多选批量下载
- **元数据编辑**：编辑提示词、生成参数、分级、备注及模型信息（Checkpoint / LoRA / LyCORIS / Embedding / VAE，LoRA 可单独设置权重）

### 系统与数据

- **统一文件管理**：目录变更实时同步至数据库
- **只读模式**：`--read_only` 启动后拒绝一切文件写入，有效防止意外修改
- **外部目录挂载**：类似Linux mount的功能，可将外部目录挂载到图库中的空目录，可设为只读

---

## 元数据解析支持

| 工具 | PNG | JPG | WEBP |
|---|:---:|:---:|:---:|
| A1111 WebUI | ✅ | ✅ | ✅ |
| LibLibAI | ✅ | | |
| Civitai 在线生成 | | ✅ | |
| ComfyUI | ✅ | | |

> - **A1111 家族**：通过 PNG 文本块 `parameters` 或 JPEG / WEBP 的 EXIF `UserComment` 读取提示词与参数
> - **LibLibAI**：额外支持 PNG 的 `AIGC` 块
> - **Civitai**：存储格式可能随平台更新而变化；解析失败时图片仍会正常入库，仅缺少生成参数
> - **ComfyUI**：读取 PNG 内置的 `prompt` / `workflow` 工作流，从保存节点回溯采样器，提取提示词、采样参数、Checkpoint、VAE 与 LoRA；支持 easy-use（`easy promptConcat` / `easy loraStack`）、LoraManager （`Lora Loader (LoraManager)`）节点。图片同时带有 A1111 infotext 时按 **ComfyUI** 解析，缺失字段再由 infotext 补齐；过于复杂或包含未知自定义节点的工作流可能只能提取部分参数
> - 无法解析生成参数的图片仍会入库，但只能获取分辨率与文件大小

---

## 环境要求

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | `>=3.10, <3.13`（推荐 3.12） | 运行后端 |
| Node.js | `>=24.12`（推荐 24.12） | 构建前端（必需） |

---

## 快速开始

```bash
git clone https://github.com/DriveSand/Genesis.git
cd Genesis
```

**1. 构建前端**（仓库不含 `dist`，首次使用必须先构建）：

```bash
cd web
npm install
npm run build
cd ..
```

**2. 安装后端依赖并启动。**

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

启动后访问 <http://127.0.0.1:5580/>。首次运行会自动创建 `gallery/` 与 `data/`

> 默认会自动打开浏览器；如需在服务器 / 后台运行，使用 `--headless_mode` 禁用该行为

---

## 启动参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 监听地址；设为 `0.0.0.0` 可从其他设备访问 |
| `--port` | `5580` | 监听端口 |
| `--read_only` | 关闭 | 只读模式：禁止创建、修改、删除、移动、上传等写操作 |
| `--headless_mode` | 关闭 | 启动后不自动打开浏览器 |
| `--log_level` | `warning` | 写入日志文件的最低等级（debug / info / warning / error / critical）；控制台始终输出全部等级 |
| `--generate_image_cache` | 关闭 | 启动后在后台为全部图片预生成缩略图 |
| `--generate_video_cache` | 关闭 | 预留参数；当前版本尚未实现 |

示例：

```bash
python main.py --host 0.0.0.0 --port 8080
```

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
   └─ dist/                  # 构建产物（需自行 npm run build 生成，不随仓库提供）
```

---

## 配置

全局设置在首次启动时写入 `data/config.json`，也可在 Web 界面「全局设置」中修改

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
| `DatabaseIdleCheckpointMinutes` | `10` | 写空闲多少分钟后执行 WAL checkpoint（0=关闭，范围: 0–1440）；重启生效 |

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
| `readonly` | 是否只读 |
| `priority` | 逻辑路径冲突时的优先级，值越小越优先 |

> 该文件仅在**启动时读取一次**，修改后需重启。加载时逐级创建对应逻辑路径：目标目录**不存在**则自动创建并挂载；**已存在且为空**则转换为挂载点；**非空**则跳过并记录错误

---

## 前端开发

前端使用 HTML + Vue 3（组合式 API）+ TypeScript + Vite，源码位于 `web/`

**构建生产版本：**

```bash
cd web
npm install
npm run build
```

**本地开发调试：**

```bash
cd web
npm run dev
```

开发服务器默认运行在 <http://127.0.0.1:5173>，并将 `/api` 代理到 `http://127.0.0.1:5580`

---

## API 与文档

- **交互式接口文档（Swagger UI）**：<http://127.0.0.1:5580/api/docs>
- **服务状态**：`GET /api/v1/status`（程序版本、数据库版本、只读状态）
- **更详细说明**见 `docs/`：[Web API](https://github.com/DriveSand/Genesis/blob/main/docs/web_api.md)、[数据库](https://github.com/DriveSand/Genesis/blob/main/docs/database.md)、[配置管理](https://github.com/DriveSand/Genesis/blob/main/docs/config_manager.md)、[日志](https://github.com/DriveSand/Genesis/blob/main/docs/logger.md)、[异常定义](https://github.com/DriveSand/Genesis/blob/main/docs/exception.md)

---

## 常见问题

**Q：端口被占用？** 使用 `--port` 指定其他端口，如 `python main.py --port 8080`

**Q：启动时数据库正在升级？** 程序会在启动阶段同步完成迁移，期间不启动文件管理 / 缩略图 / Web 服务。迁移前自动备份到 `data/db/db_backup/`，失败会整体回滚

**Q：图片没有提示词？** 图片内没有可解析的生成元数据，或来自不支持的工具。图片仍会入库并显示分辨率，可在查看器中手动「编辑信息」

**Q：如何只允许查看、不允许修改？** 使用 `--read_only` 启动，所有文件写操作都会被拒绝

---

## AI 辅助开发

本项目使用 AI 辅助开发：

- 工具：Claude Code
- 模型：Deepseek、Qwen

---
## 许可

本项目采用 GPLv3 协议开放源代码，使用前请阅读并遵守相应条款

本项目还使用了其他库的 代码/图标 （详见: [THIRD_PARTY_LICENSES](https://github.com/DriveSand/Genesis/tree/main/THIRD_PARTY_LICENSES) 里的 [README.md](https://github.com/DriveSand/Genesis/blob/main/THIRD_PARTY_LICENSES/README.md)）,使用前请前往对应仓库阅读并遵守其相应条款
