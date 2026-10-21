# 请求数据异常

---

> RequestKeyNotFound
错误码：40400
程序内部使用

---

> RequestContentIncorrect
错误码：40000
程序内部使用

---

> InputDataError
错误码：40001

---

# 元数据解析异常

> MetadataParserError
元数据解析异常基类

---

> UnsupportedFormatError
错误码：42201
文件不存在 / 非图片 / 非支持的图片格式
程序内部使用（调度器捕获后降级为 debug 日志）

---

> MetadataCorruptError
错误码：42202
图片损坏 / 元数据读取失败
程序内部使用（调度器捕获后降级为 warning 日志）

---

> ParseFailedError
错误码：42203
解析器内部结构性错误（预留）
程序内部使用

---

# 配置管理异常

> ConfigError
配置管理异常基类

---

> ConfigReadError
错误码：42101
config.json 读取 / 解析失败（JSON 损坏、顶层非对象、文件不可读）
程序内部使用

---

> ConfigWriteError
错误码：42102
config.json 回写失败（磁盘 IO 失败 / 配置值不可 JSON 序列化）
程序内部使用

---

# 数据库异常

> DatabaseError
数据库模块异常基类

---

> DatabaseClosedError
错误码：42111
数据库未初始化或已关闭后仍被访问
程序内部使用

---

> SchemaInitError
错误码：42112
数据库结构初始化失败（建表 / 根目录记录创建失败）
程序内部使用

---

> DatabaseVersionError
错误码：42113
数据库文件版本高于程序记录版本，或缺少升级路径（拒绝启动）
程序内部使用