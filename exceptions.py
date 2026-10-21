class APPBaseError(Exception):
    def __init__(self, error_code: int, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(self.message)
    
    def __str__(self):
        return f"[Error: {self.error_code}] {self.message}"


# 数据输入输出异常
class DataIOError(APPBaseError):
    """ 数据输入输出异常基类 """
    pass

class InputDataError(DataIOError):
    def __init__(self, message: str):
        super().__init__(40001, message)


# 请求数据异常
class RequestDataError(APPBaseError):
    pass

class RequestKeyNotFound(RequestDataError):
    def __init__(self, message):
        super().__init__(40400, message)

class RequestContentIncorrect(RequestDataError):
    def __init__(self, message):
        super().__init__(40000, message)


# 文件管理异常
class FileManagerError(Exception):
    """文件管理异常基类"""
    pass


class InvalidPathError(FileManagerError):
    """路径不以 /gallery 或 /cache 开头"""
    pass


class PathNotFoundError(FileManagerError):
    """逻辑路径在路径表中不存在"""
    pass


class FileNotFoundError(FileManagerError):  # noqa: A001 规范命名，有意覆盖内置异常
    """目标文件/文件夹不存在"""
    pass


class NameConflictError(FileManagerError):
    """同名冲突"""
    pass


class ReadOnlyError(FileManagerError):
    """只读保护拒绝"""
    pass


class DirectoryNotEmptyError(FileManagerError):
    """非空文件夹删除未确认"""
    pass


class UserChoiceRequired(FileManagerError):
    """需要用户选择写入目标"""

    def __init__(self, message: str, options: list, logical_path: str):
        super().__init__(message)
        self.options = options
        self.logical_path = logical_path


class CrossRootError(FileManagerError):
    """非法跨根操作"""
    pass


class SHA256Error(FileManagerError):
    """SHA256 计算失败"""
    pass


# 元数据解析异常
class MetadataParserError(APPBaseError):
    """元数据解析异常基类"""
    pass


class UnsupportedFormatError(MetadataParserError):
    """文件不存在 / 非图片 / 非支持的图片格式"""

    def __init__(self, message: str):
        super().__init__(42201, message)


class MetadataCorruptError(MetadataParserError):
    """图片损坏 / 元数据读取失败"""

    def __init__(self, message: str):
        super().__init__(42202, message)


class ParseFailedError(MetadataParserError):
    """解析器内部结构性错误（预留）"""

    def __init__(self, message: str):
        super().__init__(42203, message)


# 配置管理异常
class ConfigError(APPBaseError):
    """配置管理异常基类"""
    pass


class ConfigReadError(ConfigError):
    """config.json 读取 / 解析失败"""

    def __init__(self, message: str):
        super().__init__(42101, message)


class ConfigWriteError(ConfigError):
    """config.json 回写失败"""

    def __init__(self, message: str):
        super().__init__(42102, message)


# 数据库异常
class DatabaseError(APPBaseError):
    """数据库模块异常基类"""
    pass


class DatabaseClosedError(DatabaseError):
    """数据库未初始化或已关闭后仍被访问"""

    def __init__(self, message: str):
        super().__init__(42111, message)


class SchemaInitError(DatabaseError):
    """数据库结构初始化失败"""

    def __init__(self, message: str):
        super().__init__(42112, message)


class DatabaseVersionError(DatabaseError):
    """数据库版本高于程序支持版本, 拒绝启动"""

    def __init__(self, message: str):
        super().__init__(42113, message)