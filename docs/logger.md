# logger 模块接口文档

日志模块：为整个程序提供统一的日志入口 `log` 与一次性初始化函数 `setup_logging()`。
业务代码只导入 `log` 直接打日志，模块自动识别真实调用方模块名，无需手动传递。

## 公共 API 一览

| 名称 | 类型 | 说明 |
|---|---|---|
| `log` | `logging.Logger` 实例（模块级全局） | 全程序唯一的日志入口 |
| `setup_logging` | 函数 | 启动时初始化日志（文件 + 控制台双通道），调用一次即可，可重复调用 |
| `LOG_FILE_NAME` | 模块级常量 `str` | 日志文件名，固定为 `"genesis.log"`（位于 `FolderPath.BASE_PATH` 下） |

---

## setup_logging

```python
def setup_logging(log_level: str = "warn") -> None
```

初始化日志通道：在 `FolderPath.BASE_PATH / LOG_FILE_NAME` 建立文件日志，并挂载控制台日志。应在 `main.py` 启动时调用一次。

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `log_level` | `str` | 否 | `"warn"` | **文件日志**的最低记录等级。有效值：`debug` / `info` / `warning` / `error` / `critical`，大小写与首尾空格不敏感。其他任意取值（含默认值 `"warn"`、`None`、空串）回退为 `WARNING`。**控制台不受此参数限制，始终输出所有等级（DEBUG 起）** |

### 返回值

| 类型 | 说明 |
|---|---|
| `None` | 无返回值；初始化结果体现在 `log` 的输出通道上 |

### 行为契约

- 每次调用都会**清空并重建**日志文件（先以写模式截断，再挂载 handler）。
- 支持重复调用：旧的 handler 会被移除并关闭后重建，不会产生重复输出。
- 文件行格式：`[yyyy/mm/dd HH:MM:SS] [等级] [模块] 内容`（UTF-8 编码）。
- 控制台行格式：`[等级] [模块] 内容`，等级带 ANSI 颜色（Windows 终端自动启用虚拟转义处理）。

### 异常

| 异常 | 触发条件 | 调用方处理建议 |
|---|---|---|
| `OSError` | 日志文件无法创建/写入（如 `BASE_PATH` 目录不存在且不可创建、磁盘只读、权限不足） | 启动阶段捕获后记录到 stderr 并终止启动，或确保数据目录可写后重试；不应在未初始化的状态下长期运行 |

---

## log

模块级全局 Logger 实例（内部为 `logging.Logger` 的业务子类，名为 `"genesis"`），等级恒为 `DEBUG`，不向 root 传播。提供标准的五个等级方法：

```python
log.debug(msg, *args, **kwargs)
log.info(msg, *args, **kwargs)
log.warning(msg, *args, **kwargs)
log.error(msg, *args, **kwargs)
log.critical(msg, *args, **kwargs)
```

### 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `msg` | `str` | 是 | — | 日志内容；含占位符时配合 `*args` 做 `%` 格式化（如 `log.info("count=%d", n)`），避免提前拼接 |
| `*args` | `tuple` | 否 | 空 | 与 `msg` 中占位符对应的格式化实参 |
| `exc_info` | `bool \| Exception` | 否 | `False` | 关键字参数；为 `True` 或异常对象时附带 traceback（控制台与文件均完整展开）。记录异常必带 |
| `stack_info` | `bool` | 否 | `False` | 关键字参数；附带调用栈信息 |
| `extra` | `dict` | 否 | `None` | 关键字参数；向日志记录注入附加字段（本模块格式化器不消费自定义字段，一般无需使用） |

各方法参数含义完全一致，仅等级不同：`DEBUG` 调试细节、`INFO` 关键步骤、`WARNING` 可恢复异常、`ERROR` 操作失败（异常处应带 `exc_info=True`）、`CRITICAL` 服务不可用级故障（如数据库繁忙、缩略图模块过载时打印此级）。

### 返回值

| 类型 | 说明 |
|---|---|
| `None` | 五个方法均无返回值 |

### 异常

- 正常情况下**不向调用方抛出任何异常**；输出通道自身失败（如文件句柄失效）由 logging 内部捕获并报告到 stderr，不影响业务流程。日志调用不应被 try/except 包裹。

### 行为契约

- 行内 `[模块]` 字段自动识别为**真实调用方**的限定模块名（logger 模块自身被跳过），调用方无需传递：
  - `scripts/database/service.py` → `[database.service]`
  - `scripts/parsers/a1111.py` → `[parsers.a1111]`
  - `main.py` / 根目录模块 → `[main]` / `[config_manager]`
  - 项目外的第三方模块回退为文件名（不含扩展名）。
- 模块名通过挂载在 handler 上的 Filter 改写 `LogRecord.module` 实现，业务代码无需修改。
- 业务代码统一 `from scripts.logger import log`，禁止另行创建 Logger 或使用 `print`。
- 在 `setup_logging()` 之前调用：`WARNING` 及以上经系统兜底 handler 输出到 stderr，`DEBUG` / `INFO` 被丢弃——业务模块应保证启动初始化先行。

---

## 典型用法

```python
from scripts.logger import setup_logging
setup_logging(args.log_level)          # main.py 启动时调用一次

from scripts.logger import log
log.info("Gallery scan started")       # 关键步骤
log.warning(f"Mount offline: {key}")   # 可恢复异常
log.error(f"Read failed: {path}", exc_info=True)  # 异常处必带 exc_info
```
