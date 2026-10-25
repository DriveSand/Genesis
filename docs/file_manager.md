# file_manager 模块接口文档

`scripts/file_manager/` 是程序内**唯一执行文件 I/O 的模块包**，对外门面为 `scripts.file_manager.manager.FileManager`。缩略图生成、上传下载、Web 路由等所有文件操作都必须经过本模块。

模块职责：

- `/gallery` 与 `/cache` 的根前缀分发、文件名与路径段校验；
- 文件/文件夹的创建、重命名、删除、移动、读取、写入；
- 通过 watchdog 监控 `/gallery` 与外部挂载点，将磁盘变化同步到数据库；
- 新文件自动入库（SHA256、分辨率探测、后台元数据解析与模型绑定）；
- 上传临时文件、分块合并后的移动与入库；
- 程序级只读与挂载点级只读保护；
- 挂载点健康检查、离线确认与恢复轮询；
- 程序启动后的磁盘 / 数据库对账。

> 模块边界：本包不执行 SQL（全部通过 `DataBase` 门面）；逻辑路径与物理路径转换委托 `scripts/path_resolver.py`；配置读取委托 `scripts/config_manager.py`；EXIF 解析委托 `scripts/parser_scheduler.py`。

---

## 子模块划分

| 文件 | 职责 |
|---|---|
| `manager.py` | `FileManager` 门面：组装子模块、启动/关闭、对外文件操作与上传接口 |
| `operations.py` | 文件 CRUD、读写、入库流程、元数据解析任务 |
| `upload.py` | `UploadService`：上传临时文件、移动入库、失败清理 |
| `watchdog.py` | `WatchService`：watchdog 注册、事件分发与数据库同步 |
| `mount_manager.py` | `MountManager`：挂载点监控、健康检查、离线检测与恢复 |
| `startup.py` | `StartupReconciler`：启动时磁盘与数据库对账 |
| `readonly.py` | `ReadonlyGuard`：程序级 / 目录级只读检查 |
| `db_helpers.py` | `DbHelper`：路径节点维护、文件记录迁移、元数据写回等组合数据库操作 |
| `disk_utils.py` | 磁盘操作：创建、重命名、递归删除、重名处理、校验复制、写入完成等待 |
| `path_utils.py` | `/gallery`、`/cache` 前缀分发、忽略规则、MIME 推断等纯工具 |
| `whitelist.py` | 自身操作去重白名单，过滤本模块触发的 watchdog 事件 |

包入口 `scripts/file_manager/__init__.py` 只导出：

```python
from scripts.file_manager import FileManager, join_logical_path, split_logical_path
```

其余子模块视为内部实现，调用方不应直接依赖。

---

## 公共 API 一览

| 名称 | 类型 | 说明 |
|---|---|---|
| `FileManager` | 类 | 文件操作唯一入口 |
| `FileManager.create_folder` | 方法 | 创建文件夹 |
| `FileManager.create_file` | 方法 | 创建空文件并入库 |
| `FileManager.rename_file` | 方法 | 重命名文件 |
| `FileManager.rename_folder` | 方法 | 重命名文件夹 |
| `FileManager.delete_file` | 方法 | 删除文件 |
| `FileManager.delete_folder` | 方法 | 删除文件夹（非空需确认） |
| `FileManager.move_entry` | 方法 | 跨目录移动文件/文件夹（仅 `/gallery`） |
| `FileManager.entry_stat` | 方法 | 条目物理信息 |
| `FileManager.folder_stats` | 方法 | 递归统计文件数与字节数 |
| `FileManager.read_file` | 方法 | 打开文件读取流 |
| `FileManager.file_exists` | 方法 | 判断文件是否存在 |
| `FileManager.write_file` | 方法 | 原子写入二进制内容 |
| `FileManager.list_files` | 方法 | 列出目录下文件 |
| `FileManager.compute_sha256` | 方法 | 异步计算 SHA256 |
| `FileManager.reparse_metadata` | 方法 | 重新解析并写回元数据 |
| `FileManager.open_upload_file` | 方法 | 创建上传临时文件 |
| `FileManager.upload_move` | 方法 | 上传临时文件移动入库 |
| `FileManager.discard_upload` | 方法 | 丢弃上传临时文件 |
| `FileManager.shutdown` | 方法 | 关闭监控、线程池并清理临时数据 |
| `FileManager.update_mount_configs` | 方法 | 更新挂载配置 |
| `FileManager.resolve_parent` | 方法 | 解析父目录（子模块内部复用的公共骨架） |
| `join_logical_path` | 函数 | 段列表拼回带 `/gallery` / `/cache` 前缀的逻辑路径 |
| `split_logical_path` | 函数 | 拆分完整逻辑路径为 `(kind, segments)` |

---

## 路径与写入目标约定

### 完整逻辑路径

`FileManager` 的**文件操作接口**以完整逻辑路径为参数，必须以 `/gallery` 或 `/cache` 开头：

| 根 | 说明 | 示例 |
|---|---|---|
| `/gallery` | 数据库路径表管理、受 watchdog 监控、挂载感知 | `/gallery/test/a.png` |
| `/cache` | 直接映射 `data/cache`，不入库、不监控 | `/cache/image/{sha}/{size}.webp` |

- 两个根都拒绝 `.` / `..` 段，防止路径穿越；单段名称另经 `validate_name` 校验（拒绝空串、`.`、`..`、`/`、`\`、`:`）。
- Web API 使用 gallery 相对路径（如 `/test/a.png`），由路由层 `helpers.gallery_full()` 转成 `/gallery/test/a.png` 后再调用本模块。
- 带 `/gallery` / `/cache` 前缀的路径**不要**传给 `PathResolver`；该前缀的分发由 `path_utils.split_logical_path` 完成。

### destination 参数

写操作（创建、重命名、删除、移动目标、写入、上传）接受 `destination`：

| 值 | 含义 |
|---|---|
| `None`（默认） | 正常挂载感知解析：命中挂载点则落到挂载物理目录 |
| `"mount"` | 明确写入挂载物理目录（仅在挂载点内时与默认一致） |
| `"local"` | 忽略挂载映射，写入 `GALLERY_ROOT` 下的本地影子目录 |

当写目标位于**可写挂载点**内且 `destination is None` 时，抛出 `UserChoiceRequired`（409，`options: ["mount", "local"]`），要求调用方明确选择；只读挂载点则在更早的只读检查中直接抛 `ReadOnlyError`。

> 移动操作的**源**始终固定按 `"mount"` 解析（作用于文件实际所在位置），`destination` 只影响目标目录。

### 只读根

- `--read_only` 启动：拒绝一切 `/gallery` 写操作；`/cache` 不受影响。
- 挂载点 `readonly: true`：该挂载点及其子目录禁止写入（若子路径本身配置了另一个挂载点，以其自身 `readonly` 配置为准）。
- 读取操作不受只读限制。

---

## FileManager

### 构造函数

```python
FileManager(db, gallery_root: str, cache_root: str, upload_folder: str,
            mount_configs: dict, read_only: bool = False) -> None
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `db` | `DataBase` | 是 | — | 已初始化的数据库门面 |
| `gallery_root` | `str` | 是 | — | `GALLERY_ROOT` 物理路径 |
| `cache_root` | `str` | 是 | — | `CACHE_FOLDER` 物理路径 |
| `upload_folder` | `str` | 是 | — | 上传临时目录；传名称时挂到 `cache_root` 下，传绝对路径时直接使用 |
| `mount_configs` | `dict[str, MountConfig]` | 是 | — | 挂载配置，由 `config_manager.load_external_folders()` 产出 |
| `read_only` | `bool` | 否 | `False` | 程序级只读标志 |

构造过程：

1. 物理根转为绝对路径，初始化 `db_lock`（与 `PathResolver` 共享的 `RLock`）。
2. 创建去重白名单、`PathResolver`、`Sha256Service(4)`、只读缓存（遍历路径表预计算）。
3. 尝试导入 `parser_scheduler.parse_metadata`；导入失败时 `parse_metadata = None`，自动入库仍可完成，但 `reparse_metadata()` 会报错。
4. 组装 `DbHelper`、`FileOperations`、`UploadService`、`WatchService`、`MountManager`、`StartupReconciler`。
5. 注册 `/gallery` 根监控与全部挂载点监控，启动 watchdog observer。
6. 将启动对账 `startup.run` 提交到 SHA256 线程池后台执行（不阻塞启动）。

#### 主要公开属性

| 属性 | 类型 | 说明 |
|---|---|---|
| `db` | `DataBase` | 数据库门面 |
| `gallery_root` / `cache_root` / `upload_folder` | `str` | 绝对物理路径 |
| `read_only` | `bool` | 程序级只读标志 |
| `shutting_down` | `bool` | 关闭标志；监控与定时任务据此停止 |
| `db_lock` | `threading.RLock` | 串行化文件层全部数据库访问；与 `PathResolver` 共享 |
| `resolver` | `PathResolver` | 路径解析器（`thumbnail_gen`、Web 辅助层会直接使用） |
| `sha` | `Sha256Service` | 4 线程 SHA256 / 后台任务池 |
| `readonly` | `ReadonlyGuard` | 只读缓存与写前检查 |
| `whitelist` | `DedupeWhitelist` | 自身操作去重白名单 |
| `parse_metadata` | `Callable \| None` | 元数据解析入口（可能因导入失败为 `None`） |
| `db_helper` / `operations` / `upload` / `watch` / `mounts` / `startup` | 子服务 | 内部实现；外部代码不应直接调用 |

### shutdown

```python
def shutdown(self) -> None
```

停止文件管理服务，按以下顺序执行：

1. 置位 `shutting_down`；
2. 停止 watchdog observer（`stop` 后 `join` 最多 5 秒）；
3. 取消挂载点健康检查 / 恢复轮询定时器；
4. 关闭 SHA256 线程池（等待已提交任务完成）；
5. 关闭上传线程池（等待任务完成），删除全部上传临时文件并清空映射表；
6. 清空去重白名单。

**注意**：

- 不关闭数据库；数据库由 `main.py` 在最外层关闭。
- 不回收 `read_file()` 返回的流，调用方必须自行关闭。
- 关闭后不应继续调用文件操作接口。

### update_mount_configs

```python
def update_mount_configs(self, mount_configs: dict) -> None
```

更新挂载配置：整体替换 `PathResolver` 的挂载表、重建只读缓存，并为尚未注册监控的挂载点补注册 watchdog + 健康检查。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mount_configs` | `dict[str, MountConfig]` | 是 | 与构造函数同格式；传空字典清空挂载 |

**当前实现注意**：只补注册**新增**挂载点，不会撤销已被移除挂载点的监控与状态。正常流程中 `external_folder.json` 仅在启动时加载一次，本方法主要供内部/测试使用。

### resolve_parent

```python
def resolve_parent(self, kind: str, segments: list[str],
                   destination: Literal["mount", "local"] | None = None,
                   guard: bool = False, must_exist: bool = False,
                   guard_segments: list[str] | None = None
                   ) -> tuple[str, str | None, str | None]
```

子模块（`operations` / `upload` 等）共用的父目录解析骨架，通常不需要外部调用。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `kind` | `"gallery"` \| `"cache"` | 是 | — | 根类型 |
| `segments` | `list[str]` | 是 | — | 父目录段列表 |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | `None` | 写入目标选择 |
| `guard` | `bool` | 否 | `False` | 是否执行只读 / 挂载选择检查（仅 `/gallery`） |
| `must_exist` | `bool` | 否 | `False` | `/cache` 是否要求父目录已存在 |
| `guard_segments` | `list[str] \| None` | 否 | `None` | 只读检查使用的段列表；缺省用 `segments` |

**返回值**：`(父目录物理路径, 目录节点 uuid | None, 带 /gallery 前缀的逻辑路径 | None)`

- `/cache`：返回 `(cache_root/segments, None, None)`；`must_exist=True` 时目录不存在抛 `PathNotFoundError`。
- `/gallery`：可选执行 `ReadonlyGuard.check` → `walk_segments` → `segments_to_physical`。

| 异常 | 触发条件 |
|---|---|
| `PathNotFoundError` | `/gallery` 父目录不在路径表；`/cache` 父目录不存在且 `must_exist=True` |
| `ReadOnlyError` / `UserChoiceRequired` | `guard=True` 且写前检查失败 |

> 本方法不校验路径段与名称；`InvalidPathError` 由上层 `split_logical_path` / `validate_name` 产生。

---

## 文件操作

以下方法的路径参数均为完整逻辑路径（`/gallery/...` 或 `/cache/...`），文件名另行通过 `validate_name` 校验。

### create_folder / create_file

```python
def create_folder(self, parent_path: str, folder_name: str,
                  destination: Literal["mount", "local"] | None = None) -> str

def create_file(self, parent_path: str, filename: str,
                destination: Literal["mount", "local"] | None = None) -> str
```

在 `parent_path` 下创建文件夹或空文件，返回新条目的**完整逻辑路径**。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `parent_path` | `str` | 是 | — | 父目录完整逻辑路径 |
| `folder_name` / `filename` | `str` | 是 | — | 新条目名称（单段，不含分隔符） |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | `None` | 写入目标选择（见上文） |

**行为**：

- `/cache`：父目录必须已存在；只创建磁盘条目，不写数据库。
- `/gallery`：
  1. 解析父目录（路径表必须存在）并执行只读 / 挂载选择检查；
  2. 磁盘已存在同名条目则抛 `NameConflictError`；
  3. 目标写入去重白名单，必要时逐级创建本地影子目录，再创建条目；
  4. 文件夹写入 `path` 表；空文件以空内容 SHA256 复用/创建 `info` 记录，并写入 `file` 记录（`file_size=0`）。
- 新建文件夹会同步更新只读缓存；新建文件不解析元数据。

| 主要异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径前缀非法、含 `.` / `..`、名称为空或含分隔符/`:` |
| `PathNotFoundError` | `/gallery` 父目录不在路径表；`/cache` 父目录不存在 |
| `NameConflictError` | 磁盘上已存在同名条目 |
| `ReadOnlyError` | 程序级只读，或目标目录位于只读挂载点 |
| `UserChoiceRequired` | 目标位于可写挂载点但未指定 `destination` |

### rename_file / rename_folder

```python
def rename_file(self, parent_path: str, old_name: str, new_name: str,
                destination: Literal["mount", "local"] | None = None) -> None

def rename_folder(self, parent_path: str, old_name: str, new_name: str,
                  destination: Literal["mount", "local"] | None = None) -> None
```

在同一目录内重命名。返回 `None`。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 条目所在目录的完整逻辑路径 |
| `old_name` | `str` | 是 | 当前名称 |
| `new_name` | `str` | 是 | 新名称（经 `validate_name`） |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | 写入目标选择 |

**行为**：

- `/cache`：只做磁盘重命名，不涉及数据库。
- `/gallery`：源/目标物理路径取父目录实际位置；源不存在抛 `FileNotFoundError`，目标已存在抛 `NameConflictError`；先写白名单再 `os.replace`，随后更新数据库。
  - 文件：原地更新 `file.filename`，保留 `file.id` 与模型关联。若数据库中没有该文件记录，仅记录 WARNING 并继续磁盘重命名（不会补建记录）。
  - 文件夹：要求路径表存在对应节点，只更新节点 `name`，子树记录不动；完成后整体重建只读缓存。
- 文件夹级只读检查针对**条目自身**（`parent + old_name`）；文件级针对**父目录**。

| 主要异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或新名称非法 |
| `PathNotFoundError` | `/gallery` 父目录或文件夹节点不存在 |
| `FileNotFoundError`（`exceptions.FileNotFoundError`） | 磁盘上源条目不存在 |
| `NameConflictError` | 目标名称已存在 |
| `ReadOnlyError` / `UserChoiceRequired` | 只读或未选择挂载写入目标 |

### delete_file / delete_folder

```python
def delete_file(self, parent_path: str, filename: str,
                destination: Literal["mount", "local"] | None = None) -> None

def delete_folder(self, parent_path: str, folder_name: str,
                  confirm: bool = False,
                  destination: Literal["mount", "local"] | None = None) -> None
```

删除文件或文件夹。文件夹递归删除，非空文件夹必须显式 `confirm=True`。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `parent_path` | `str` | 是 | — | 条目所在目录的完整逻辑路径 |
| `filename` / `folder_name` | `str` | 是 | — | 待删除条目名称 |
| `confirm` | `bool` | 否 | `False` | 非空文件夹删除确认 |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | `None` | 写入目标选择 |

**行为**：

- `/cache`：直接递归删除 / 删除文件，无数据库操作。
- `/gallery`：
  1. 前置检查条目存在、文件夹非空确认、只读 / 挂载选择；
  2. 文件夹要求路径表节点存在；将整棵子树写入删除白名单；
  3. 磁盘删除（`rmtree` 或 `os.remove`）；
  4. 数据库删除：文件夹自底向上先删文件记录再删路径节点；文件删除单条 `file` 记录；
  5. 文件夹删除后整体重建只读缓存。
- 没有回收站：磁盘内容立即删除。
- 对**挂载点节点本身**执行删除/重命名时，目标路径由“父目录物理路径 + 名称”拼接，实际作用于本地影子目录，不会删除或重命名外部挂载的物理目录；挂载点应通过配置管理。

| 主要异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |
| `PathNotFoundError` | `/gallery` 父目录或文件夹节点不存在 |
| `FileNotFoundError`（`exceptions.FileNotFoundError`） | 磁盘上条目不存在 |
| `DirectoryNotEmptyError` | 非空文件夹且 `confirm=False` |
| `ReadOnlyError` / `UserChoiceRequired` | 只读或未选择挂载写入目标 |

### move_entry

```python
def move_entry(self, source_parent: str, name: str, target_parent: str,
               is_dir: bool = False,
               destination: Literal["mount", "local"] | None = None) -> str
```

跨目录移动文件/文件夹，返回新条目的完整逻辑路径。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `source_parent` | `str` | 是 | — | 源目录完整逻辑路径（仅 `/gallery`） |
| `name` | `str` | 是 | — | 条目名称 |
| `target_parent` | `str` | 是 | — | 目标目录完整逻辑路径（仅 `/gallery`） |
| `is_dir` | `bool` | 否 | `False` | 是否为文件夹 |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | `None` | **目标侧**写入选择 |

**行为**：

1. 校验两个路径都位于 `/gallery`；源与目标目录相同、或文件夹移入自身子树 → `InvalidPathError`。
2. 源侧按实际位置解析并检查只读；目标侧执行只读 / 挂载选择检查。
3. 源不存在 / 目标同名 → `FileNotFoundError` / `NameConflictError`。
4. 整棵子树写入重命名白名单，`shutil.move`（跨文件系统时自动复制后删除源），确保目标父目录存在。
5. 数据库迁移：
   - 文件夹：更新目录节点 `parent_uuid`/`name`，子树与文件记录保持不变；
   - 文件：原地更新 `path_uuid`/`filename`，保留 `file.id` 与模型关联；
   - 若源侧缺少数据库记录：文件夹按新增重新登记整棵子树，文件按新增入库。
6. 文件夹移动后整体重建只读缓存。

| 主要异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 非 `/gallery`、源/目标相同、移入自身子树、名称非法 |
| `CrossRootError` | 源或目标不在 `/gallery` |
| `PathNotFoundError` | 父目录不在路径表 |
| `FileNotFoundError` / `NameConflictError` | 源不存在 / 目标重名 |
| `ReadOnlyError` / `UserChoiceRequired` | 源/目标只读或未选择写入目标 |

---

## 查询与读写

### entry_stat

```python
def entry_stat(self, parent_path: str, name: str) -> dict[str, Any] | None
```

返回条目的物理信息；不存在或无法 stat 时返回 `None`。

**返回值**：

```python
{
    "size": int,           # 字节数
    "created_time": int,   # st_ctime（Windows 为创建时间；POSIX 为 inode 变更时间）
    "edit_time": int,      # st_mtime
    "is_dir": bool,
}
```

**行为**：

- 名称经 `validate_name`；只拼接物理路径，不校验路径表。
- `/gallery` 条目按挂载感知解析：若条目自身是挂载点，则 stat 挂载物理根，而不是本地影子目录。
- `/cache` 条目直接映射缓存目录。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |

### folder_stats

```python
def folder_stats(self, parent_path: str) -> dict[str, int]
```

递归统计逻辑目录下的文件数与总字节数，返回 `{"files": int, "bytes": int}`。

- `/gallery`：父目录必须在路径表中存在。
- `/cache`：目录不存在时返回 `{"files": 0, "bytes": 0}`。
- 单个文件读取大小失败会被跳过，不影响整体统计。
- `os.walk` 不跟随目录符号链接。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径非法 |
| `PathNotFoundError` | `/gallery` 父目录不在路径表 |

### read_file

```python
def read_file(self, parent_path: str, filename: str) -> io.BufferedReader
```

以二进制只读模式打开文件并返回流。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 文件所在目录的完整逻辑路径 |
| `filename` | `str` | 是 | 文件名（经 `validate_name`） |

**返回值**：`io.BufferedReader` — **调用方负责关闭**；本模块不跟踪回收。

- 不检查只读；`/gallery` 与 `/cache` 均可读。
- `/gallery` 父目录必须在路径表中存在；文件不存在抛 `FileNotFoundError`。
- 传输过程中文件被删除 / 移动导致的读取失败由调用方处理。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |
| `PathNotFoundError` | `/gallery` 父目录不在路径表 |
| `FileNotFoundError`（`exceptions.FileNotFoundError`） | 目标不是文件或不存在 |

### file_exists

```python
def file_exists(self, parent_path: str, filename: str) -> bool
```

判断文件是否存在。

- `/gallery` 父目录不在路径表时抛 `PathNotFoundError`，而不是返回 `False`。
- `/cache` 父目录不存在时返回 `False`。
- 只检查 `os.path.isfile`，目录返回 `False`。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |
| `PathNotFoundError` | `/gallery` 父目录不在路径表 |

### write_file

```python
def write_file(self, parent_path: str, filename: str, data: bytes) -> None
```

将二进制内容原子写入文件。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 目标目录完整逻辑路径 |
| `filename` | `str` | 是 | 文件名（经 `validate_name`） |
| `data` | `bytes` | 是 | 完整文件内容 |

**行为**：

- 先在同目录写 `.{uuid}.tmp`，成功后 `os.replace` 原子替换；失败清理临时文件后重新抛 `OSError`。
- `/cache`：父目录必须存在；无数据库操作。
- `/gallery`：执行只读 / 挂载选择检查；**不写去重白名单**，由 watchdog 按外部变更规则处理入库。若已有同名记录且文件大小未变化，入库流程可能跳过重新解析（自动入库以大小作为快速判断）。
- 会覆盖已存在的同名文件。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |
| `PathNotFoundError` | `/cache` 父目录不存在；`/gallery` 父目录不在路径表 |
| `ReadOnlyError` / `UserChoiceRequired` | 只读或未选择挂载写入目标 |
| `OSError` | 磁盘写入 / 替换失败 |

### list_files

```python
def list_files(self, parent_path: str) -> list[dict[str, Any]]
```

列出逻辑目录下的文件。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 目录完整逻辑路径 |

**返回值**：

- `/gallery`：数据库 `file` 记录，附带经 `info` 关联得到的 `sha256`；每项包含 `id`、`info_id`、`path_uuid`、`filename`、`mime_type`、`added_at`、`width`、`height`、`file_size`、`directory`、`sha256`。
- `/cache`：直接枚举磁盘文件（按名称排序），仅返回 `{"filename", "sha256": None, "directory"}`，不包含子目录。

`/cache` 父目录必须存在；`/gallery` 父目录必须在路径表中存在。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径非法 |
| `PathNotFoundError` | 父目录不存在 |

---

## SHA256 与元数据

### compute_sha256

```python
def compute_sha256(self, parent_path: str, filename: str,
                   callback: Callable[[str | None, str | None], None]) -> None
```

异步计算文件 SHA256。立即返回，结果通过回调交付。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 文件所在目录完整逻辑路径 |
| `filename` | `str` | 是 | 文件名 |
| `callback` | `Callable` | 是 | `callback(digest, error)`；二者恰有一个非 `None` |

**行为**：

- 路径 / 文件不存在时不抛异常，而是回调 `(None, "file_not_found")`；该早退回调在**调用线程同步执行**。
- 实际计算提交到 4 线程 `Sha256Service`；成功 / 计算失败的回调在**工作线程中执行**，实现必须线程安全。
- 计算逻辑见 `scripts/sha256.py`：≤8 MB 一次性读入，>8 MB 按 8 MB 分块；mtime 变化时丢弃重算。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |

### reparse_metadata

```python
def reparse_metadata(self, parent_path: str, filename: str) -> dict[str, Any]
```

同步重新读取图片元数据并写回数据库（Web “重新读取元数据”）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `parent_path` | `str` | 是 | 文件所在目录完整逻辑路径（仅 `/gallery`） |
| `filename` | `str` | 是 | 文件名 |

**返回值**：解析出的元数据字典；图片没有可解析的生成参数时返回 `{}`。

**行为**：

1. 校验解析器可用、路径位于 `/gallery`、磁盘文件存在、数据库 `file` 记录存在；
2. 调用 `parser_scheduler.parse_metadata()`（永不抛异常）解析；
3. **无论是否解析到生成参数**，都重新探测宽高与文件大小并刷新 `file` 表；
4. 有元数据时按白名单写回 `info` 字段，并通过 `models` 字段整体替换文件的 checkpoint / lora / embedding / vae 绑定。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 路径或名称非法 |
| `CrossRootError` | 目标不在 `/gallery` |
| `PathNotFoundError` | 父目录不在路径表 |
| `FileNotFoundError`（`exceptions.FileNotFoundError`） | 磁盘文件或数据库记录不存在 |
| `FileManagerError` | 元数据解析器不可用（导入失败） |

### 自动入库流程

外部新增文件由 watchdog / 启动对账提交到 SHA256 线程池，执行 `ingest_file_task`：

1. `wait_file_ready` 等待文件稳定：大小与 mtime 连续一次轮询不变，且大小非空；持续为空 2 秒视为空文件，最长等待 10 秒。
2. 计算 SHA256（mtime 变化自动重算，最多 3 次）；哈希期间文件大小再次变化则重试，最多 3 轮；拒绝空内容 SHA256。
3. 若已有同名 `file` 记录：
   - 大小相同 → 视为重复事件，直接返回；
   - 大小不同 → 删除旧记录后重新入库。
4. 用 Pillow 独立探测宽高（失败为 `0×0`），按 SHA256 复用/创建 `info`（新记录为空元数据、`rating=1`），写入 `file` 记录。
5. 提交后台 `parse_metadata_task`：解析成功后经 `DbHelper.apply_metadata` 写回 `info` / `file` / 模型绑定；失败仅告警。

**注意**：自动入库只按**文件大小**判断已有记录是否为同一内容；大小相同但内容被替换的文件不会触发重新入库。显式创建的空文件（`create_file`）会直接写入 `file_size=0` 的记录，而 watchdog 自动入库会跳过空文件。

---

## 上传

上传采用“临时文件 → 移动入库”两阶段模型，Web 层负责分块接收与合并，`FileManager` 负责临时文件与最终落盘。

### open_upload_file

```python
def open_upload_file(self, original_filename: str,
                     target_parent: str) -> tuple[str, io.BufferedWriter]
```

开始一次上传。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `original_filename` | `str` | 是 | 客户端原始文件名；不得为空或含 `/`、`\` |
| `target_parent` | `str` | 是 | 目标目录完整逻辑路径（仅 `/gallery`） |

**返回值**：`(temp_name, stream)`

- `temp_name`：UUID 临时文件名（不含路径），作为后续 `upload_move` / `discard_upload` 的标识；
- `stream`：以 `wb` 打开的写入流，调用方负责写入并关闭。

**行为**：

- 目标必须是 `/gallery`，否则抛 `CrossRootError`。
- 在 `upload_folder`（默认 `data/cache/upload`）中创建临时文件，并登记 `{temp_name: {original_name, target_parent}}`。
- 本方法**不检查只读**；真正的写入检查在 `upload_move` 中执行。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 原始文件名为空或含分隔符 |
| `CrossRootError` | 目标不在 `/gallery` |

### upload_move

```python
def upload_move(self, source_filename: str, target_parent: str, sha256: str,
                destination: Literal["mount", "local"] | None = None,
                callback: Callable[[str | None, str | None], None] | None = None
                ) -> None
```

将上传临时文件移动到目标目录并入库。**同步完成校验，异步执行移动**。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `source_filename` | `str` | 是 | — | `open_upload_file` 返回的临时文件名 |
| `target_parent` | `str` | 是 | — | 目标目录完整逻辑路径（仅 `/gallery`） |
| `sha256` | `str` | 是 | — | 上传内容的预期 SHA256（64 位十六进制，大小写不敏感） |
| `destination` | `"mount"` \| `"local"` \| `None` | 否 | `None` | 写入目标选择 |
| `callback` | `Callable \| None` | 否 | `None` | `callback(new_logical_path, error)`；在工作线程执行 |

**调用线程内同步执行**：

1. 校验 SHA256 格式（非法 → `InputDataError`）与目标根（必须 `/gallery` → `CrossRootError`）；
2. 只读 / 挂载选择检查（`ReadOnlyError` / `UserChoiceRequired`）；
3. 计算临时文件 SHA256，计算失败或不匹配 → `SHA256Error`（临时文件保留，可重试或丢弃）。

**上传线程池（2 线程）中异步执行**：

1. 查找临时文件映射；不存在 → 回调错误；
2. 解析目标物理目录（挂载感知）并创建；
3. 重名自动追加 `(n)` 后缀（最多 100 次，保留完整扩展名）；
4. 写去重白名单后尝试 `os.replace` 原子移动；跨文件系统失败时改为“复制 + 二次 SHA256 校验”，最多 3 次；
5. Pillow 探测宽高，按 SHA 复用/创建 `info`，写入 `file` 记录；
6. 异步触发元数据解析；
7. 移除映射并回调成功（参数为带 `/gallery` 前缀的新逻辑路径）；失败仅告警并回调错误，**保留临时文件与映射**供调用方重试或清理。

> 回调在工作线程中执行；不要依赖调用线程的上下文。未提供回调时，成功与失败只记录日志。

| 异常（同步阶段） | 触发条件 |
|---|---|
| `InputDataError` | SHA256 格式非法 |
| `CrossRootError` | 目标不在 `/gallery` |
| `ReadOnlyError` / `UserChoiceRequired` | 只读或未选择挂载写入目标 |
| `SHA256Error` | 临时文件哈希计算失败或与预期不一致 |

### discard_upload

```python
def discard_upload(self, source_filename: str) -> None
```

丢弃一个上传临时文件：从映射表移除并删除磁盘文件（删除失败忽略）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `source_filename` | `str` | 是 | 临时文件名；不得为空、含 `/`、`\` 或 `..` |

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 临时文件名非法 |

### 上传生命周期（与 Web 层配合）

1. `POST /api/v1/uploads/prepare`：登记任务；对已存在的 SHA256 可直接“秒传”，否则返回文件 token。
2. 客户端按 10 MiB 分块调用 `POST /api/v1/uploads/execute`；每块写入独立的 `/cache/upload` 临时文件。
3. 最后一块到达后，Web 层合并分块到新的临时文件，调用 `upload_move()` 校验并异步落盘。
4. 失败 / 超时由调用方 `discard_upload()` 清理；`AppState` 另有 24 小时过期任务清理。
5. `FileManager.shutdown()` 会等待上传线程池并删除剩余临时文件。

---

## 只读保护

`ReadonlyGuard` 维护 `{完整逻辑路径: 是否只读}` 缓存：

- 从 `/gallery` 根开始深度优先遍历路径表；根默认可写；
- 每个节点若恰好是挂载点，取其 `MountConfig.readonly`；否则继承父目录状态；
- 程序级 `read_only` 在检查时优先判断。

写前检查 `check(segments, destination)` 的顺序：

1. `read_only=True` → `ReadOnlyError`（程序级只读）；
2. 目标完整逻辑路径在缓存中为只读 → `ReadOnlyError`（目录级只读，向下继承）；
3. 目标位于某个挂载点内且未提供 `destination` → `UserChoiceRequired`（可选 `mount` / `local`）。

只读仅约束 `/gallery`；`/cache`（缩略图、上传临时文件）始终可写。

缓存更新时机：创建文件夹 / 文件时继承父目录状态；文件夹重命名、删除、移动与挂载配置更新后整体重建；watchdog 自动创建目录时按父目录继承。

---

## 文件系统监控

`WatchService` 在同一个 watchdog `Observer` 上为 `/gallery` 根与每个挂载根注册**递归**监控。

### 事件过滤

- 忽略临时/中间文件：扩展名 `.tmp`、`.temp`、`.bak`、`.swp`、`.swo`、`.partial`、`.crdownload`、`.download`；名称 `.DS_Store`、`Thumbs.db`、`desktop.ini`；以 `~$` 开头的 Office 临时文件。
- 经本模块执行的写操作会提前写入 `DedupeWhitelist`，事件到达时按“路径 + 操作类型 + 10 秒窗口”跳过，避免自身操作被重复同步。
- `moved` 事件若源是临时文件而目标不是，按新增处理（原子写入 `tmp → 目标` 的典型表现）。

### 事件同步

| 事件 | 处理 |
|---|---|
| 新建目录 | `ensure_dir_records` 逐级确保路径表节点存在 |
| 新建文件 | 确保父目录记录，提交 `ingest_file_task` 到 SHA256 线程池 |
| 删除目录 | 自底向上删除文件记录与路径节点，重建只读缓存 |
| 删除文件 | 删除 `file` 记录 |
| 重命名/移动 | 双方都在监控范围内：迁移目录/文件记录；一方在范围外：按新增或删除处理 |

事件处理中的 `OSError` / `PermissionError` 会触发对应挂载点的离线确认；其他异常只记录日志，不会终止监控线程。

### 去重白名单

`DedupeWhitelist` 结构为 `{规范化物理路径: (OpType, 时间戳)}`：

- 匹配窗口 10 秒，条目 30 秒后过期（写入时惰性清理）；
- 操作类型：`CREATE_FILE`、`CREATE_DIR`、`RENAME_FILE`、`RENAME_DIR`、`DELETE_FILE`、`DELETE_DIR`；
- 线程安全；`shutdown` 时清空。

---

## 挂载管理

`MountManager` 维护每个挂载点的运行时状态：

```python
{
    "physical": str,        # 挂载物理路径
    "logical": str,         # /gallery 前缀逻辑路径
    "segments": list[str],  # 逻辑段列表
    "online": bool,
    "confirming": bool,
    "watch": watch 句柄,
}
```

### 启动注册

- 只有路径表中存在挂载节点的记录才注册 watchdog；
- 注册成功后将路径表状态置为 `normal`（复位上次运行遗留的 `offline`），并启动 30 秒链式健康检查；
- 路径表缺失或监控注册失败时将运行时状态标记为离线（不写 `path.status`），并每 30 秒轮询恢复。

### 离线检测与恢复

1. 在线挂载点每 30 秒检查物理目录是否可访问；
2. 不可访问 → 进入 5 秒确认期；确认期结束仍不可访问，则：
   - 路径表状态写为 `offline`；
   - 注销 watchdog；
   - `online=False`；
   - 每 30 秒轮询恢复。
3. 恢复可访问且路径表记录存在 → 状态写回 `normal`，重新注册 watchdog，恢复健康检查链。

> 挂载恢复后不会立即执行启动对账；离线期间的文件变化在下一次程序启动对账或 watchdog 事件触发时同步。

### update_mount_configs

调用 `PathResolver.update_mount_configs`、重建只读缓存，并为 `state` 中不存在的挂载 key 注册监控。当前实现不撤销已移除挂载的监控。

---

## 启动对账

`StartupReconciler` 在启动后**异步**运行（SHA256 线程池），对 `/gallery` 根与全部挂载点逐目录比对磁盘和数据库：

- 磁盘存在、数据库缺失的目录：逐级 `ensure_dir_records`；
- 磁盘存在、数据库缺失的文件：提交 `ingest_file_task`；
- 数据库存在、磁盘已删除的目录/文件：移除记录（删除前再次确认，避免与并发新增竞态）；
- 已存在但**大小变化**的文件：视为内容被替换，先删记录再重新入库；
- 大小相同的文件直接跳过；
- 挂载点及其本地影子目录不参与本地根的对账，避免误删外部挂载中的数据；
- 不可访问的挂载点整体跳过并记录 WARNING。

因程序关闭期间 watchdog 无法感知变化，本流程保证下次启动后数据库与磁盘一致。

---

## 并发与线程模型

| 执行体 | 数量 | 用途 |
|---|---|---|
| FastAPI 处理线程 | 按请求 | 调用 `FileManager` 同步方法 |
| watchdog Observer | 1 | 接收文件系统事件 |
| `Sha256Service` 线程池 | 4 | 异步 SHA256、新文件入库、元数据解析、启动对账 |
| 上传线程池 | 2 | 上传临时文件最终移动入库 |
| `threading.Timer` | 动态 | 挂载健康检查 / 离线确认 / 恢复轮询 |

- `FileManager.db_lock`（`RLock`）串行化文件层的所有数据库访问，并与 `PathResolver` 共享；`DbHelper` 的多步查询/写入通常在调用方持有该锁时执行，部分方法（如 `ensure_dir_records`）内部自行加锁，锁可重入。
- `DataBase` 自身仍是“单写线程 + 4 读线程”模型；`db_lock` 是文件层之上的额外串行化。
- 回调（`compute_sha256`、`upload_move`）通常在**工作线程**执行，必须线程安全；`compute_sha256` 的 `file_not_found` 早退回调是调用线程同步执行。
- `read_file` 返回的流由调用方关闭；本模块不跟踪回收。
- `shutting_down` 置位后，watchdog 事件与挂载定时器停止处理；关闭顺序见 `shutdown()`。

---

## 异常一览

本模块抛出的异常均为 `FileManagerError` 子类（`InputDataError` 除外），本身没有内置错误码，由 Web 层 `scripts/web_backend/errors.py` 统一映射：

| 异常 | Web 错误码 / HTTP | 典型场景 |
|---|---|---|
| `InvalidPathError` | `40002` / `400` | 路径前缀非法、含 `.` / `..`、名称含分隔符或 `:` |
| `CrossRootError` | `40003` / `400` | 跨 `/gallery` / `/cache` 的不允许操作（上传目标、移动源/目标） |
| `PathNotFoundError` | `40410` / `404` | 逻辑路径不在路径表、挂载节点缺失 |
| `exceptions.FileNotFoundError` | `40411` / `404` | 磁盘条目或数据库文件记录不存在 |
| `NameConflictError` | `40901` / `409` | 同名文件/文件夹已存在 |
| `UserChoiceRequired` | `40902` / `409` | 写目标位于可写挂载点但未指定 `destination`；携带 `options` 与 `logical_path` |
| `DirectoryNotEmptyError` | `40903` / `409` | 非空文件夹删除未确认 |
| `ReadOnlyError` | `40301` / `403` | 程序级只读或目录级只读 |
| `SHA256Error` | `42204` / `422` | 上传哈希计算失败 / 不一致、跨文件系统复制校验失败 |
| `InputDataError` | `40001` / `400` | SHA256 格式非法等输入错误 |
| `FileManagerError`（基类） | `50001` / `500` | 其他文件管理错误 |
| `OSError` 等其他异常 | `50000` / `500` | 磁盘故障、线程池已关闭等未映射异常 |

`UserChoiceRequired` 额外携带：

```python
exc.options       # ["mount", "local"]
exc.logical_path  # 目标完整逻辑路径
```

---

## 典型用法

```python
from scripts.file_manager import FileManager

# 构造（由 main.py 完成）
fm = FileManager(db, gallery_root, cache_root, "upload", mounts,
                 read_only=False)

# 新建文件夹并读取文件
new_path = fm.create_folder("/gallery/test", "2024")
stream = fm.read_file("/gallery/test", "a.png")
try:
    data = stream.read()
finally:
    stream.close()

# 移动
fm.move_entry("/gallery/test", "a.png", "/gallery/test/2024", is_dir=False)

# 重新解析元数据
meta = fm.reparse_metadata("/gallery/test/2024", "a.png")

# 上传：写入临时文件 -> 校验并异步移动
tmp, out = fm.open_upload_file("a.png", "/gallery/test/2024")
try:
    out.write(data)
finally:
    out.close()
fm.upload_move(tmp, "/gallery/test/2024", sha256,
               callback=lambda path, err: print(path, err))
```

---

## 注意事项

1. **所有文件 I/O 必须经过 `FileManager`**；Web 路由、缩略图模块等不得直接读写磁盘。
2. **路径前缀**：公开方法使用 `/gallery/...` / `/cache/...`；`PathResolver` 使用 gallery 相对路径。
3. **`/cache` 不入库、不监控**；缩略图和上传临时文件的数据库一致性由调用方自行维护。
4. **可写挂载点内写入必须提供 `destination`**，否则 `UserChoiceRequired`；只读挂载点会先被 `ReadOnlyError` 拒绝。
5. **移动只在 `/gallery` 内**；源侧始终作用于实际挂载位置，`destination` 仅影响目标。
6. **启动对账与自动入库只按文件大小判断内容是否变化**：大小相同的内容替换不会被识别。
7. **离线挂载恢复后不会立即对账**；其数据在下一次启动或 watchdog 事件时同步。
8. **`update_mount_configs` 只补注册新增挂载**，不会移除已删除挂载的监控。
9. **上传失败保留临时文件与映射**，调用方必须调用 `discard_upload()` 或重试；`shutdown()` 会统一清理。
10. **`read_file` 流由调用方关闭**；不要把流长期保存在内存中。
