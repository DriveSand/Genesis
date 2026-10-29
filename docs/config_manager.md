# config_manager 模块接口文档

应用配置（`config.json`）与外部挂载点（`external_folder.json`）的统一管理入口。
`config.json` 在构造时读取一次、运行时修改后立即原子回写硬盘；`external_folder.json` 仅在启动时加载一次，之后本地文件变更不会重新读取。

## 公共 API 一览

| 名称 | 类型 | 说明 |
|---|---|---|
| `ConfigManager` | 类 | 配置管理入口（构造即加载 `config.json`） |
| `MountConfig` | frozen dataclass | 单个挂载点的配置结构（见下文） |
| `DEFAULT_CONFIG` | `dict[str, Any]` | 内置配置默认值（可修改的键集合即以此为准） |
| `CONFIG_FILE_NAME` | `str` 常量 | 配置文件名，固定 `"config.json"` |
| `EXTERNAL_FOLDER_FILE` | `str` 常量 | 挂载配置文件名，固定 `"external_folder.json"` |

---

## ConfigManager

所有方法线程安全（内部锁串行化读取、修改与回写），可从任意业务线程调用。

### ConfigManager() 构造函数

```python
def __init__(self, data_folder: pathlib.Path = FolderPath.DATA_FOLDER) -> None
```

读取 `data_folder/config.json` 完成初始化；文件不存在时以 `DEFAULT_CONFIG` 自动创建，缺失的键自动补全默认值并回写。

#### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `data_folder` | `pathlib.Path` | 否 | `FolderPath.DATA_FOLDER` | `config.json` 所在目录 |

#### 实例属性（公开字段）

| 属性 | 类型 | 说明 |
|---|---|---|
| `config_file` | `pathlib.Path` | 配置文件完整路径 |
| `config` | `dict[str, Any]` | 当前内存中的配置全量快照（只读使用，修改请走 `update_config*`） |
| `external_mounts` | `dict[str, MountConfig]` | 挂载点结果，key 为逻辑路径；`load_external_folders()` 调用前恒为 `{}` |

#### 异常

| 异常（error_code） | 触发条件 | 调用方处理建议 |
|---|---|---|
| `ConfigReadError`（42101） | `config.json` 内容不是合法 JSON，或顶层不是 JSON 对象 | 属数据损坏：提示用户修复/删除该文件后重启；不宜静默忽略 |
| `ConfigWriteError`（42102） | 首次创建或补全默认项时无法原子回写（目录不可写、磁盘故障等） | 记录日志并终止启动（无落盘配置则后续写操作同样会失败） |

### get_config

```python
def get_config(self, key: str) -> Any
```

读取单个配置项。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `key` | `str` | 是 | — | 配置键（须为 `DEFAULT_CONFIG` 已定义的键） |

**返回值**：`Any` — 该键当前值，类型随键而定（`bool` / `int` / `list[int]` 等）。

| 异常（error_code） | 触发条件 | 调用方处理建议 |
|---|---|---|
| `RequestKeyNotFound`（40400） | 键不存在于配置中 | 视为调用方编程错误或前端传参错误：记录日志并返回错误响应，勿重试 |

### get_configs

```python
def get_configs(self, keys: list[str]) -> dict[str, Any]
```

批量读取配置项。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `keys` | `list[str]` | 是 | — | 配置键列表；空列表返回空字典 |

**返回值**：`dict[str, Any]` — `{键: 值}`。任一键不存在时整体抛异常，无部分结果。

| 异常（error_code） | 触发条件 | 调用方处理建议 |
|---|---|---|
| `RequestKeyNotFound`（40400） | `keys` 中任一键不存在 | 同 `get_config` |

### update_config

```python
def update_config(self, key: str, value: Any) -> None
```

更新单个配置项并立即回写硬盘（校验与原子性语义完全复用 `update_configs`）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `key` | `str` | 是 | — | 待更新的配置键（必须已存在，不支持新增键） |
| `value` | `Any` | 是 | — | 新值；不得为 `None`，且须可 JSON 序列化 |

**返回值**：`None`。异常见 `update_configs`。

### update_configs

```python
def update_configs(self, updates: dict[str, Any]) -> None
```

批量更新配置并回写。保证原子性：**先整体校验（任一键非法则一项都不改）**；保存失败时内存状态回滚，与硬盘保持一致。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `updates` | `dict[str, Any]` | 是 | — | `{键: 新值}`；空字典直接返回（不产生任何读写） |

**返回值**：`None`。

**契约注记**：值仅校验"键存在 + 非 None + 可 JSON 序列化"，**不做业务范围校验**（如像素值 64–1024 等上下限由调用方/前端保证）。

| 异常（error_code） | 触发条件 | 调用方处理建议 |
|---|---|---|
| `RequestKeyNotFound`（40400） | 任一 `key` 不存在于配置 | 修正调用参数；整批未生效，可放心重新提交 |
| `RequestContentIncorrect`（40000） | 任一 `value` 为 `None`（空/非法内容） | 提示用户补充有效值；整批未生效 |
| `ConfigWriteError`（42102） | 回写失败（磁盘/权限故障，或值不可 JSON 序列化） | 内存已自动回滚，配置仍为旧值：记录日志、向用户报错；重复提交同一值无意义，需先排除故障 |

### load_external_folders

```python
def load_external_folders(self, db: DataBase) -> tuple[dict[str, MountConfig], list[str]]
```

从项目根目录（`FolderPath.BASE_PATH`）读取 `external_folder.json`，校验、消解物理路径冲突、经 `db` 在路径表逐级创建挂载节点。**仅启动时调用一次**；重复调用会重新加载并整体替换 `external_mounts`。**本方法不向调用方抛出任何异常**——单条失败仅告警并跳过，整体读取失败按空配置处理。

挂载目标节点已存在时：若为**空的内置目录**（路径表下无子目录、无文件记录），会将其转换为 `type='mount'` 的挂载点；若目录非空则记录错误并跳过。目标节点不存在时按需逐级创建。

输入文件的结构契约：顶层为 `{"<逻辑路径>": {"physical_path": str, "readonly": bool, "priority": int}}`（其他未识别字段会被忽略，便于兼容旧配置）。逻辑路径须以 `/` 开头、不含 `..` 与 `//`、不得为根 `/`；`physical_path` 须为存在目录的绝对路径；`priority` 须为非负整数；同一物理路径的多个逻辑挂载按 `priority 小者 → 去 '/' 后路径短者 → 逻辑路径字典序前者` 胜出，落选者与校验失败者进入 skipped。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `db` | `DataBase` | 是 | — | 已 `init()` 的数据库门面，用于路径表节点创建（本模块不直接执行 SQL） |

**返回值**：`tuple[mounted, skipped]`

| 字段 | 类型 | 说明 |
|---|---|---|
| `mounted` | `dict[str, MountConfig]` | 成功挂载的条目，key 为逻辑路径；同时存入 `self.external_mounts` |
| `skipped` | `list[str]` | 被跳过的逻辑路径（校验失败、冲突落选、路径表创建失败三类合并） |

文件不存在或整体不可读/格式非法时返回 `({}, [])`，仅记 ERROR 日志。

**调用方处理建议**：启动流程中先 `database.init()` 再调用本方法，将 `mounted` 传给 `FileManager.update_mount_configs`；如需向用户反馈部分挂载失效，检查 `skipped` 非空即可，无需异常捕获。

---

## MountConfig

```python
@dataclass(frozen=True)
MountConfig(logical_path: str, physical_path: str, readonly: bool, priority: int)
```

不可变的单挂载点配置；由 `load_external_folders` 产出，实例可直接作为 dict key / 入 `set`。

| 字段 | 类型 | 说明 |
|---|---|---|
| `logical_path` | `str` | 挂载点逻辑路径（`/gallery` 下，如 `/abc`），以 `/` 开头 |
| `physical_path` | `str` | 挂载的物理目录绝对路径 |
| `readonly` | `bool` | 该挂载点及其全部子目录禁止写入 |
| `priority` | `int` | 物理路径冲突消解优先级，非负，越小越优先 |

---

## DEFAULT_CONFIG

`config.json` 的内置默认键值；运行时可更新（不可新增键）。

| 键 | 类型 | 默认值 | 含义 |
|---|---|---|---|
| `EnableThumbnail` | `bool` | `True` | 启用预览图预览（开启后同步开启预览图生成） |
| `ThumbnailShortSideSize` | `int` | `512` | 缩略图短边像素长度（范围 64–1024，范围由调用方保证） |
| `NewlyAddedImageLevel` | `int` | `1` | 新增图片的默认分级 |
| `GridCellSize` | `int` | `240` | 网格单元大小（控制图片显示的大小，范围 64–1024） |
| `AutoRefresh` | `bool` | `True` | 前端自动刷新 |
| `ShowLevel` | `list[int]` | `[0,1,2,3,4]` | 允许显示的图片等级集合 |
| `SafeBrowsingMode` | `bool` | `False` | 安全浏览模式（只显示 G 级图像） |
| `BlurLevel` | `int` | `2` | 开始模糊的等级（-1=None 不模糊，0=G，1=PG-13，2=R，3=X，4=XXX） |
| `DatabaseIdleCheckpointMinutes` | `int` | `10` | 数据库写空闲多少分钟后执行 WAL checkpoint（0=关闭，范围 0–1440）；**修改后重启程序生效** |

---

## 异常类型说明

本模块抛出的异常均继承 `APPBaseError`，携带 `error_code`（int）与 `message`（str）属性，定义见 `exceptions.py`：

| 异常 | error_code | 来源 |
|---|---|---|
| `RequestKeyNotFound` | 40400 | 请求了不存在的配置键 |
| `RequestContentIncorrect` | 40000 | 提交的值为空（None） |
| `ConfigReadError` | 42101 | `config.json` 读取/解析失败 |
| `ConfigWriteError` | 42102 | `config.json` 原子回写失败 |
