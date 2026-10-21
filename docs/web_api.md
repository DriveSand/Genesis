# Web 后端接口说明

FastAPI 实现，启动入口 `main.py`。接口契约以 `construction/前端API.md` 为准，本文补充实现约定与补充端点。前端构建产物位于 `web/dist`，由后端直接托管（访问 `/`）。

## 通用约定

- API 前缀：`/api/v1`；交互式文档：`/api/docs`。
- 逻辑路径：图片与目录参数均为 **/gallery 相对逻辑路径**（根为 `/`），如 `/test/a.png`；不接受 `/gallery` 前缀。
- 错误响应：`{"error": {"code": <int>, "message": "<英文描述>"}}`。
  HTTP 状态映射：`404` 不存在（40400/40410/40411）、`400` 参数非法（40000/40001/40002/40003）、`403` 只读拒绝（40301）、`409` 冲突（40901/40902/40903）、`422` 哈希/元数据失败（422xx）、`500` 服务内部错误（421xx/50xxx）。
- 分页：`max_quantity` 最小按 10 计算，上限 200（目录内容 500）；`cursor` 为后端 `current_cursor` 原样回传，不透明。
- `--read_only` 启动时，一切文件写操作返回 403。

## 端点一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/status` | 服务状态（`read_only`、`enable_thumbnail`） |
| GET | `/api/v1/images/models` | 图片使用的模型（checkpoint/lora/embedding/vae） |
| GET | `/api/v1/images/metadata` | 图片生成信息 |
| POST | `/api/v1/images/metadata/update` | 修改元数据（仅改数据库） |
| POST | `/api/v1/images/metadata/refresh?image_path=` | 重新解析图片元数据并写回数据库；分辨率/文件大小总刷新，响应含 `has_metadata`（补充端点） |
| GET | `/api/v1/images/model-names?kind=` | 某种模型的去重名称（编辑联想，补充端点） |
| GET | `/api/v1/images/thumbnail?image_sha256=` | 缩略图（缺失时按需生成并等待） |
| POST | `/api/v1/images/thumbnail/regenerate?image_sha256=` | 重新生成单张缩略图（补充端点） |
| GET | `/api/v1/images/original?image_path=` | 原图 |
| GET | `/api/v1/directories/contents` | 目录内容（folders 先于 files，游标分页） |
| GET | `/api/v1/directories/scan` | 目录变更 SSE（`event: change` / `data: true`） |
| POST | `/api/v1/search/basic` | 基本搜索（`$p:` 后接引号包裹的目录模式） |
| POST | `/api/v1/search/advanced` | 高级搜索 |
| GET | `/api/v1/downloads/single` | 单文件下载 |
| POST | `/api/v1/downloads/batch` | 多文件/目录流式 ZIP（仅存储，重名追加 `(N)`） |
| POST | `/api/v1/files/delete` | 批量删除（文件夹递归删除） |
| POST | `/api/v1/files/move` | 批量跨目录移动 |
| POST | `/api/v1/files/rename` | 重命名 |
| POST | `/api/v1/files/folder` | 新建文件夹（`auto_rename: true` 时同名自动追加 `(N)`，补充端点） |
| GET | `/api/v1/settings` | 获取配置 |
| POST | `/api/v1/settings/update` | 更新配置（只发送变化字段） |
| GET | `/api/v1/settings/thumbnail-cache` | 缩略图缓存占用（补充端点） |
| POST | `/api/v1/settings/thumbnail-cache/clear` | 清空缩略图缓存（补充端点） |
| POST | `/api/v1/settings/regenerate-thumbnails` | 全库重新生成缩略图（后台执行，补充端点） |
| POST | `/api/v1/uploads/check-folders` | 检查目标目录下已存在的相对文件夹路径（树状上传冲突处理，补充端点） |
| POST | `/api/v1/uploads/prepare` | 上传准备：秒传判定 + 分块大小 + 任务/文件 token |
| POST | `/api/v1/uploads/execute` | 分块上传（Form-Data：task_id/token/chunk_index/total_chunks/file） |

## 行为说明

- **目录内容**：响应额外携带当前目录 `uuid`（供 SSE 使用）；`scanned_path` 表示目录物理位置可访问。
- **目录扫描 (SSE)**：服务端每 2 秒比对子目录数/文件数/时间戳，变化时推送 `change`；前端在 `AutoRefresh` 开启时自动订阅。程序退出时无需客户端断开：收到停止信号后事件流会主动结束（0.2 秒内响应）；若仍有长连接未结束，FastAPI 的优雅关闭最多等待 3 秒，随后强制关闭。
- **搜索**：同一条件数组内多元素取 OR，条件之间取 AND；include 为"包含"、exclusion 为"不得包含"；模型名称大小写不敏感，版本留空匹配全部版本；文件大小按字节、区间为开区间，`0` 表示该侧不设限。
- **基本搜索目录模式**：前端把 `$p:` 后连续的引号字符串解析为 `path` 数组（`$p:"/123" "*/test"` 表示组内 OR）；目录名含空格时必须写在引号内。各模式语义：
  - `/123`：仅根目录下 `123` 目录内的文件（不含子目录）；
  - `*/123`：任意层级下 `123` 目录内的文件（不含子目录，`*` 可跨层级）；
  - `/123/*`：根目录下 `123` 目录及其全部子目录内的文件；
  - `123`：未以 `/` 或 `*` 开头时保持旧的目录路径模糊匹配。
  直接调用 API 时 `path` 传入去除引号后的模式字符串即可。
- **启动对账**：程序启动后在后台比对 `/gallery` 与各在线挂载点的磁盘内容与数据库记录，补齐程序关闭期间新增的文件/目录，移除已删除的记录；已存在但大小变化的文件视为内容被替换并重新入库。
- **入库竞态**：新文件在写入完成后才计算 SHA256、探测分辨率并解析元数据；空文件不会入库，避免把 0 字节快照写成正式记录。
- **文件夹上传**：前端按"扁平 / 树状"结构选择后进入等待上传阶段（树状模式展示目录层级与各文件大小），由用户确认后开始；树状模式逐级创建目录（冲突时选择合并或新建 `(N)`），并按目标目录分组调用 `prepare/execute`。
- **分辨率**：入库时用 Pillow 独立探测图片宽高（与元数据解析无关），无生成元数据的图片也能得到分辨率；重新读取元数据时同样始终刷新分辨率与文件大小，解析不到生成参数时 `has_metadata` 为 `false`。
- **秒传**：`prepare` 时若同 SHA256 已有可读副本，后端直接复制并返回 `duplicate: true`（失败自动回退为真实上传）。
- **分块上传**：按 `chunk_size`（10 MiB）切片；最后一块到达后合并、校验 SHA256、移动入库；返回 `status: success | fail`（失败附 `error` 字段），`saved_path` 为 `/gallery` 相对路径。
- **配置**：键集合取自 `config_manager.DEFAULT_CONFIG`；尺寸类 64–1024、分级 0–4、`ShowLevel` 为 0–4 列表，非法值返回 400。
- **数据库空闲合并**：`DatabaseIdleCheckpointMinutes` 为 0–1440 的整数（0=关闭），写入配置文件后需重启程序才会应用到数据库后台任务。
- **模型归属**：模型按**文件**关联（file N—N checkpoint/lora/embedding/vae）。`images/models` 返回指定文件绑定的模型；`images/metadata/update` 中的模型数组只修改该文件；`images/model-names` 返回仍被文件引用的去重名称及 `file_count`。
- **移动**：仅支持 `/gallery` 内；源侧操作固定作用于挂载实际位置；目标同名冲突返回 409；文件夹不得移入自身子树。
