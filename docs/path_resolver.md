# path_resolver 模块接口文档

`scripts/path_resolver.py` 是程序内 **`/gallery` 路径转换的唯一入口**：负责逻辑路径与物理路径的双向转换、路径 UUID 到路径的解析、SHA256 到文件副本的查询，以及物理路径到挂载点的反查。

模块边界（硬性约束）：

- 只处理 `/gallery` 的路径体系；`/gallery` 与 `/cache` 的根前缀分发由 `scripts/file_manager/path_utils.py` 负责，本模块不感知 `/cache`。
- 除通过 `DataBase` 门面做只读查询外，**不做任何磁盘 I/O、不执行任何 SQL**。
- 挂载配置由本类唯一持有；路径表节点的创建由 `config_manager` / `database` 完成，本模块不写数据库。

---

## 路径约定

本模块内部同时存在多种路径表示，混用是最常见的错误来源：

| 名称 | 形式 | 示例 | 使用范围 |
|---|---|---|---|
| 完整逻辑路径（相对 `/gallery`） | 恒以 `/` 开头，**不带 `/gallery` 前缀** | `/`、`/abc/123` | 本模块公开查询 API 的输入/输出；`DataBase.resolve_path` |
| 带前缀逻辑路径 | 以 `/gallery` 开头 | `/gallery/abc/123` | 仅 `walk_segments` 的返回值（供 `FileManager` 内部使用） |
| 段列表 | 目录名组成的 `list[str]` | `["abc", "123"]` | 段级内部方法；不允许 `.` / `..` |
| 挂载 key | 不带 `/gallery`、规范化的逻辑路径 | `/abc` | 挂载配置字典的 key；根 `/` 不允许作为挂载点 |
| 物理路径 | 操作系统绝对路径 | `D:\Pictures\Example\a.png` | 与文件系统交互的场景 |

> **重要**：本模块不会剥离传入路径的 `/gallery` 或 `/cache` 前缀。把 `/gallery/abc` 传给 `logical_to_physical` 会被当作根目录下名为 `gallery` 的子目录来解析，通常得到 `PathNotFoundError`，也可能误命中真实的 `gallery` 目录。`FileManager` 会在调用前自行去掉 `/gallery` 前缀。

---

## 公共 API 一览

| 名称 | 类型 | 说明 |
|---|---|---|
| `GALLERY` | `str` 常量 | 固定为 `"/gallery"` |
| `norm_physical` | 函数 | 物理路径规范化（供比较使用） |
| `validate_name` | 函数 | 校验单段文件/文件夹名称 |
| `match_root` | 函数 | 物理路径的最长前缀匹配 |
| `PathResolver` | 类 | 路径解析器（挂载感知） |

`PathResolver` 方法分为两类：

| 分组 | 方法 |
|---|---|
| 对外查询 API | `sha_to_files`、`uuid_to_logical`、`uuid_to_physical`、`logical_to_physical`、`logical_file_to_physical`、`physical_to_logical`、`physical_to_mount_key` |
| 挂载配置管理与段级方法（供 `FileManager` 复用） | `update_mount_configs`、`iter_mounts`、`mount_config`、`walk_segments`、`mount_key_of`、`segments_to_physical` |

---

## 模块级函数与常量

### GALLERY

```python
GALLERY = "/gallery"
```

`/gallery` 根逻辑前缀。`FileManager` 用它做根分发，`PathResolver` 用它拼接 `walk_segments` 的返回值以及挂载点对应的逻辑根。

### norm_physical

```python
def norm_physical(path: str) -> str
```

物理路径规范化：`os.path.normcase(os.path.normpath(path))`。统一分隔符；Windows 下同时忽略大小写。仅用于**比较与索引**，不返回可展示给用户的原始路径。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path` | `str` | 是 | 任意物理路径 |

**返回值**：`str` — 规范化后的路径。

### validate_name

```python
def validate_name(name: Any) -> None
```

校验单段文件/文件夹名称。以下情况抛出 `InvalidPathError`：

- 不是 `str`；
- 空字符串；
- `"."` 或 `".."`；
- 包含 `/` 或 `\`（路径分隔符）；
- 包含 `:`（Windows 盘符/备用数据流防护）。

> 本函数只做上述字符级校验，不判断文件是否已存在，也不做大小写或空白裁剪。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | `Any` | 是 | 待校验的单个名称（可含扩展名） |

**返回值**：`None`。校验通过即正常返回。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 名称不满足上述任一规则 |

### match_root

```python
def match_root(physical: str,
               roots: dict[str, tuple[str, str]]) -> tuple[str, str] | None
```

在一组已注册的物理根中做**最长前缀匹配**。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `physical` | `str` | 是 | 待匹配的物理路径 |
| `roots` | `dict[str, tuple[str, str]]` | 是 | `{规范化物理根: (逻辑根, 原始物理根)}`，key 必须已通过 `norm_physical` |

**返回值**：`tuple[str, str] | None` — 命中时为 `(逻辑根, 原始物理根)`；不属于任何根时返回 `None`。

**匹配规则**：

- 前缀边界按 `os.sep` 判定，`D:\a` 不会误匹配 `D:\ab`。
- 多个根同时命中时取规范化 key 最长者（即最深挂载点）。
- 不校验路径是否存在。

> **限制**：若某个物理根本身是文件系统根（如 `D:\` 或 `/`），由于边界判定会拼接出 `D:\\` / `//`，其子路径无法命中该前缀。此类场景应挂载具体子目录，而不是盘符根。

---

## PathResolver

```python
class PathResolver:
    def __init__(self, db, mount_configs: dict, gallery_root: str,
                 db_lock: threading.RLock | None = None) -> None
```

### 构造函数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `db` | `DataBase` | 是 | — | 数据库门面；本类只调用其只读查询方法 |
| `mount_configs` | `dict[str, MountConfig]` | 是 | — | `{挂载逻辑路径: MountConfig}`，key 形如 `/abc`（不带 `/gallery`） |
| `gallery_root` | `str` | 是 | — | `GALLERY_ROOT` 物理路径，内部转为绝对路径 |
| `db_lock` | `threading.RLock \| None` | 否 | `None` | 与 `FileManager` 共享的锁；为 `None` 时自建，仅供独立测试 |

生产环境**必须**传入 `FileManager` 的同一把 `RLock`。`PathResolver` 内部会连续执行多次数据库查询（如 `walk_segments` 逐级查路径表），共享锁保证这些多步查询与文件层的写入不会相互穿插，避免撕裂读。

构造函数末尾会调用 `update_mount_configs(mount_configs)` 建立挂载配置与反向索引；此时也会把 `gallery_root` 加入反向索引（逻辑根为 `/gallery`）。

#### 实例状态

| 属性（内部） | 说明 |
|---|---|
| `_mount_configs` | 规范化的 `{挂载 key: MountConfig}`；物理路径已转为绝对路径 |
| `_config_roots` | `{规范化物理根: (逻辑根, 原始物理根)}`，含 `/gallery` 根与全部挂载点 |
| `_gallery_root` | `GALLERY_ROOT` 的绝对路径 |
| `_db_lock` | 共享 RLock（或测试用自建锁） |

---

## 挂载配置管理

### update_mount_configs

```python
def update_mount_configs(self, mount_configs: dict) -> None
```

整体替换挂载配置，并重建正/反向索引。传 `None` 或空字典表示清空全部挂载，仅保留 `/gallery` 根。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mount_configs` | `dict[str, MountConfig]` | 是 | `{挂载逻辑路径: MountConfig}` |

**处理规则**：

- 非 `str` 的 key 直接忽略。
- key 统一规范化为 `"/" + "/".join(非空段)`：去除重复斜杠、尾随斜杠；规范化后为 `/` 的条目（根挂载）被忽略。
- `MountConfig.physical_path` 经 `os.path.abspath` 转为绝对路径并写回配置副本（使用 `dataclasses.replace`，不修改调用方对象）。
- 反向索引由 `/gallery` 根加上全部挂载点构成；同一规范化物理路径重复出现时，后写入者覆盖反向索引条目（`config_manager` 在加载阶段已消解物理路径冲突，正常不会发生）。
- 不检查物理路径是否存在——挂载目录的合法性由 `config_manager.load_external_folders` 负责。

**返回值**：`None`。

**并发语义**：每次更新都新建整表并**替换引用**，已发布的字典不会被就地修改，因此正在遍历 `iter_mounts()` 的调用方仍持有稳定快照。`_mount_configs` 与 `_config_roots` 是两次独立赋值，方法本身未加锁；调用方应串行化更新（实际仅在启动期与 `FileManager.update_mount_configs` 中调用）。

### iter_mounts

```python
def iter_mounts(self) -> Iterator[tuple[str, Any]]
```

按当前配置顺序遍历挂载点。

**返回值**：迭代器，元素为 `(挂载 key, MountConfig)`；key 已规范化，`MountConfig.physical_path` 为绝对路径。

由于配置更新是整表替换，遍历期间即使发生更新，迭代器仍绑定旧字典，可安全使用。

### mount_config

```python
def mount_config(self, mount_key: str) -> MountConfig | None
```

按挂载 key 精确查找配置。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `mount_key` | `str` | 是 | **规范化**的挂载 key（如 `/abc`），不带 `/gallery`、无尾随斜杠 |

**返回值**：`MountConfig | None` — 未命中返回 `None`。

> 本方法不做 key 规范化：传入 `/abc/`、`abc` 或 `/gallery/abc` 通常查不到（除非恰好配置了同名 key）。

---

## 对外查询 API

以下方法的逻辑路径参数/返回值均采用**相对 `/gallery`、恒以 `/` 开头的完整逻辑路径**（根为 `/`），不带 `/gallery` 前缀。

### sha_to_files

```python
def sha_to_files(self, sha256: str) -> dict[str, list[dict[str, str]]]
```

按 SHA256 查询该内容在图库中的全部现有副本。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `sha256` | `str` | 是 | 64 位十六进制 SHA256，大小写不敏感（内部经 `normalize_sha256` 规范化） |

**返回值**：

```python
{
    "content": [
        {"logical_path": "/abc/123", "file_name": "a.png"},
        ...
    ]
}
```

- `logical_path` 为文件所在目录的完整逻辑路径；根目录文件为 `"/"`。
- 内容不存在、SHA256 格式非法、或没有任何文件记录时返回 `{"content": []}`。
- 路径节点已被删除的孤儿文件记录会被跳过，并记录 WARNING 日志。

| 异常 | 触发条件 |
|---|---|
| 无（输入类） | 所有非法/未知 SHA256 均以空结果返回；底层数据库故障仍会向上传播 |

**线程安全**：内部取共享 `db_lock`。供缩略图模块按 SHA 选取一个可读副本。

### uuid_to_logical

```python
def uuid_to_logical(self, path_uuid: str) -> str
```

路径表节点 UUID → 完整逻辑路径。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path_uuid` | `str` | 是 | `path` 表节点的 uuid |

**返回值**：`str` — 恒以 `/` 开头；根节点返回 `"/"`，其余形如 `"/abc/123"`。

| 异常 | 触发条件 |
|---|---|
| `PathNotFoundError` | uuid 在 `path` 表中不存在 |

**线程安全**：内部取共享 `db_lock`。

### uuid_to_physical

```python
def uuid_to_physical(self, path_uuid: str) -> str
```

目录节点 UUID → 物理绝对路径（挂载感知）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `path_uuid` | `str` | 是 | `path` 表中的目录节点 uuid |

**返回值**：`str` — 按 UUID 解析出的逻辑路径再做挂载感知的物理拼接。

| 异常 | 触发条件 |
|---|---|
| `PathNotFoundError` | uuid 不存在（来自 `uuid_to_logical`） |

> 本方法假定 uuid 属于目录节点；不额外校验物理路径是否真实存在。

### logical_to_physical

```python
def logical_to_physical(self, logical_path: str) -> str
```

完整逻辑路径 → 物理绝对路径，并校验路径表的存在性。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `logical_path` | `str` | 是 | 相对 `/gallery` 的完整逻辑路径（`"/"` 或 `"/abc/123"`） |

**返回值**：`str` — 挂载感知的物理绝对路径。

**处理流程**：解析并校验逻辑路径格式 → `walk_segments` 逐级确认路径表中每一级都存在 → `segments_to_physical` 计算物理落点。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 不是 `str`、不以 `/` 开头、或包含 `.` / `..` 段 |
| `PathNotFoundError` | 路径表中任一级目录不存在（含根节点缺失） |

> 只校验**路径表**，不校验磁盘上的物理目录是否存在。输入必须不带 `/gallery` 前缀。

### logical_file_to_physical

```python
def logical_file_to_physical(self, logical_path: str, filename: str) -> str
```

目录逻辑路径 + 文件名 → 物理绝对路径。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `logical_path` | `str` | 是 | 目录的完整逻辑路径 |
| `filename` | `str` | 是 | 单段文件名（经 `validate_name` 校验） |

**返回值**：`str` — `logical_to_physical(logical_path)` 与 `filename` 的拼接结果。

| 异常 | 触发条件 |
|---|---|
| `InvalidPathError` | 文件名非法（见 `validate_name`），或目录路径格式非法 |
| `PathNotFoundError` | 目录路径在路径表中不存在 |

> 不校验文件本身是否存在。

### physical_to_logical

```python
def physical_to_logical(self, physical: str) -> str | None
```

物理路径 → 完整逻辑路径（反向查询）。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `physical` | `str` | 是 | 绝对物理路径 |

**返回值**：`str | None` — 恒以 `/` 开头；`/gallery` 根或挂载根本身返回其逻辑路径（如 `"/"`、`"/abc"`）；不属于任何已配置根时返回 `None`。

**匹配范围**：基于**配置全集**（`/gallery` 根 + 全部挂载，**包含离线挂载**）做最长前缀匹配，与运行时在线状态无关。路径可以是文件或目录，不要求存在于磁盘。

### physical_to_mount_key

```python
def physical_to_mount_key(self, physical: str) -> str | None
```

物理路径 → 所属挂载点 key。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `physical` | `str` | 是 | 绝对物理路径 |

**返回值**：`str | None` —— 命中最深挂载点时返回挂载 key（不带 `/gallery`，如 `"/example"`）；命中 `/gallery` 根本身或不属于任何根时返回 `None`。

**匹配范围**：与 `physical_to_logical` 相同，使用配置全集（含离线挂载）。`FileManager` 用它将 watchdog 事件路由到对应挂载点。

---

## 段级方法（供 FileManager 复用）

这几个方法接收**已拆分的目录段列表**，不做完整的逻辑路径格式校验；调用方必须保证段中不含 `.`、`..` 或路径分隔符。

### walk_segments

```python
def walk_segments(self, segments: list[str]) -> tuple[str, str]
```

沿路径表逐级解析 `/gallery` 段列表。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `segments` | `list[str]` | 是 | 目录段列表；空列表表示 `/gallery` 根 |

**返回值**：`tuple[str, str]` — `(目标节点 uuid, 带 /gallery 前缀的逻辑路径)`。

- 空段列表 → `(根节点 uuid, "/gallery")`。
- 其余 → `(节点 uuid, "/gallery/abc/123")`。

| 异常 | 触发条件 |
|---|---|
| `PathNotFoundError` | 路径表缺少根节点，或任一级段不存在 |

**注意事项**：

- 返回值中的逻辑路径**带 `/gallery` 前缀**，与模块其他公开方法的约定不同。
- 每级使用精确名称匹配（区分大小写）；不会做挂载判断或磁盘访问。
- 本方法不校验段内容；防路径穿越由 `FileManager.split_logical_path` 在调用前完成。

**线程安全**：内部取共享 `db_lock`；`db.list_paths` 在锁内逐级查询。

### mount_key_of

```python
def mount_key_of(self, segments: list[str]) -> str | None
```

返回段列表命中的**最深**挂载点 key。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `segments` | `list[str]` | 是 | 相对 `/gallery` 的目录段列表 |

**返回值**：`str | None` — 例如挂载 `/abc` 存在时，`["abc", "sub"]` 返回 `"/abc"`；无命中或段列表为空时返回 `None`。

**行为**：从完整路径开始逐级向上查找配置，第一个命中的挂载 key 即结果。仅查配置，不访问数据库与磁盘，不判断挂载点是否在线或可访问。

### segments_to_physical

```python
def segments_to_physical(
        self, segments: list[str],
        destination: Literal["mount", "local"] | None = None) -> str
```

`/gallery` 段列表 → 物理绝对路径（挂载感知）。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `segments` | `list[str]` | 是 | — | 相对 `/gallery` 的目录段列表 |
| `destination` | `"mount" \| "local" \| None` | 否 | `None` | `"local"` 时忽略挂载映射，落到本地影子目录；其余值（含 `None`）按挂载感知处理 |

**解析规则**：

1. `destination == "local"`：直接返回 `GALLERY_ROOT / segments...`，即使该路径位于挂载点内。
2. 否则调用 `mount_key_of(segments)`：
   - 无挂载命中：返回 `GALLERY_ROOT / segments...`；
   - 命中挂载 `/abc`：返回 `挂载物理路径 / segments 中挂载点之后的部分`。

**示例**（`GALLERY_ROOT = D:\gallery`，挂载 `/example → D:\Pictures\Example`）：

| 调用 | 结果 |
|---|---|
| `segments_to_physical([])` | `D:\gallery` |
| `segments_to_physical(["other"])` | `D:\gallery\other` |
| `segments_to_physical(["example", "2024"])` | `D:\Pictures\Example\2024` |
| `segments_to_physical(["example", "2024"], "local")` | `D:\gallery\example\2024` |

**返回值**：`str` — 物理绝对路径。

**注意事项**：

- 不做存在性检查，返回路径可能并不存在（如新建目录前、挂载离线时）。
- **不做任何段内容清洗**：调用方必须保证段合法，否则可能拼出意外路径。
- `destination="local"` 仅用于“在读写挂载内写入时选择落到本地影子目录”的场景（见 `FileManager._write_guard` / `UserChoiceRequired`）。

---

## 线程模型与并发

| 方法 | 是否访问数据库 | 是否取共享 `db_lock` |
|---|---|---|
| `sha_to_files` | 是 | 是 |
| `uuid_to_logical` | 是 | 是 |
| `uuid_to_physical` | 是（经 `uuid_to_logical`） | 是 |
| `logical_to_physical` | 是（经 `walk_segments`） | 是 |
| `logical_file_to_physical` | 是（经 `logical_to_physical`） | 是 |
| `walk_segments` | 是 | 是（可重入） |
| `update_mount_configs` / `iter_mounts` / `mount_config` / `mount_key_of` / `segments_to_physical` / `physical_to_logical` / `physical_to_mount_key` | 否 | 否 |

- 共享的 `RLock` 与 `FileManager._db_lock` 是同一把锁，可重入；多步查询期间不会被文件层的写操作打断。
- 配置只读方法无锁：挂载配置以“整表新建 + 引用替换”的方式发布，单个索引的读取始终基于完整快照；`_mount_configs` 与 `_config_roots` 之间的一致性由调用方串行化更新保证。
- **生产代码不要传入 `db_lock=None`**（自建锁只能防本实例内部竞争，无法与 `FileManager` 的数据库操作互斥）。

---

## 调用关系

| 调用方 | 使用的方法 | 用途 |
|---|---|---|
| `FileManager` | `walk_segments` | 将逻辑目录解析为路径表节点 uuid |
| `FileManager` | `segments_to_physical` | 计算挂载感知的写入/读取物理路径 |
| `FileManager` | `mount_key_of` | 判断写入目标是否位于挂载点、推断只读继承 |
| `FileManager` | `physical_to_mount_key` | watchdog 事件 → 挂载点路由（离线确认） |
| `ThumbnailGenerator` | `sha_to_files` | 按 SHA256 选取一个可读副本作为缩略图源 |
| `web_backend.helpers` | `walk_segments`（经 `fm.resolver`） | 将 API 的目录路径解析为 uuid |
| `config_manager` | 产出 `MountConfig` | 挂载加载；路径表节点创建后交给 `FileManager` 构造 `PathResolver` |

本模块**不修改**路径表，也不负责挂载点的创建/校验——这些由 `config_manager.load_external_folders` 与 `database` 完成。

---

## 异常一览

本模块只抛出 `FileManagerError` 的子类（定义见 `exceptions.py`）。这些异常**没有内置 `error_code`**，由 Web 层 `scripts/web_backend/errors.py` 统一映射：

| 异常 | Web 错误码 / HTTP | 触发方法 |
|---|---|---|
| `InvalidPathError` | `40002` / `400` | `validate_name`、`logical_to_physical`、`logical_file_to_physical` |
| `PathNotFoundError` | `40410` / `404` | `walk_segments`、`uuid_to_logical`、`logical_to_physical`、`logical_file_to_physical` |
| 无路径类异常（返回 `None` / 空结果） | — | `physical_to_logical`、`physical_to_mount_key`、`sha_to_files`、`mount_config`、`mount_key_of` |

---

## 典型用法

```python
from folder_paths import FolderPath
from scripts.path_resolver import PathResolver

# FileManager 构造时：共享同一把 RLock 与挂载配置
resolver = PathResolver(db, mount_configs, str(FolderPath.GALLERY_ROOT), db_lock)

# 逻辑路径 -> 路径表节点 + 带 /gallery 前缀的逻辑路径
dir_uuid, gallery_logical = resolver.walk_segments(["example", "2024"])

# 物理落点（挂载感知）
physical = resolver.segments_to_physical(["example", "2024"])
local_shadow = resolver.segments_to_physical(["example", "2024"], "local")

# 完整逻辑路径 -> 物理路径（会校验路径表存在性）
physical = resolver.logical_to_physical("/example/2024")

# 物理路径反查
logical = resolver.physical_to_logical(r"D:\Pictures\Example\2024\a.png")  # "/example/2024/a.png"
mount_key = resolver.physical_to_mount_key(r"D:\Pictures\Example\2024\a.png")  # "/example"

# SHA256 -> 全部副本
copies = resolver.sha_to_files(sha256)["content"]
```

---

## 注意事项总结

1. **两种逻辑路径前缀不要混用**：公开 API 使用 `/abc`；`walk_segments` 返回 `/gallery/abc`。
2. **`PathResolver` 不认识 `/gallery` 与 `/cache` 前缀**，不要直接传入带前缀的路径。
3. **路径表存在性 ≠ 磁盘存在性**：`logical_to_physical` 只查数据库；`segments_to_physical` 与 `physical_to_*` 完全不检查磁盘。
4. **段级方法不做输入清洗**，调用方必须先完成名称/路径段校验（`validate_name`、`split_logical_path`）。
5. **`mount_key_of` / `mount_config` 依赖规范化 key**：无尾随斜杠、无 `/gallery`、无重复斜杠。
6. **反向查询包含离线挂载**，且基于配置而非在线状态；物理路径重复时反向索引以后写入者为准（正常由 `config_manager` 预先消解冲突）。
7. **物理根为文件系统根时最长前缀匹配会失效**，应挂载具体子目录。
8. **生产环境必须共享 `FileManager` 的 `db_lock`**，否则多步查询与写操作之间可能出现撕裂读。
