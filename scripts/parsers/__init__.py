"""scripts/parsers —— EXIF/元数据解析器包: 契约定义与注册表。

约束 (硬性) : 本包只允许被 scripts/parser_scheduler.py 导入, 
其他任何模块禁止直接导入本包内的解析器。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class RawMetadata:
    """容器读取层输出的中间结构, 是解析器的唯一输入。

    解析器拿到它之后不得再发生任何文件 I/O。
    """

    file_path: str                                      # 物理路径 (仅供日志/溯源) 
    format: str                                         # PIL 格式名: "PNG" / "JPEG" / "WEBP"
    width: int                                          # 图像实际像素宽
    height: int                                         # 图像实际像素高
    file_size: int                                      # 文件字节数
    text: dict[str, str] = field(default_factory=dict)  # PNG tEXt/iTXt/zTXt 键值
    user_comment: str | None = None                     # EXIF UserComment 解码后的文本


@runtime_checkable
class ParserModule(Protocol):
    """解析器模块契约 (模块级鸭子类型) 。

    每个解析器模块必须暴露: 
    - TOOL_NAME: str            工具名, 调度器写入结果的 tool 字段
    - can_parse(raw) -> bool    判定是否属于该工具格式; 不得抛异常
    - parse(raw) -> dict        返回结构化解析结果; 失败可抛异常, 由调度器兜底
    """

    TOOL_NAME: str

    def can_parse(self, raw: RawMetadata) -> bool: ...
    def parse(self, raw: RawMetadata) -> dict[str, Any]: ...


def iter_parsers() -> tuple[ModuleType, ...]:
    """按优先级返回解析器注册表快照 (只读) 。"""
    return tuple(_PARSERS)


# ---- 注册表 (扩展点) ----
# 新增解析器三步: 1) 新建 <tool>.py 实现契约  2) 在此 import  3) 按优先级插入列表。
# 匹配规则: 顺序遍历, 第一个 can_parse()==True 的解析器独占解析权 (首个匹配即权威) 。
from scripts.parsers import a1111  # noqa: E402  必须放在 RawMetadata 定义之后

_PARSERS: list[ModuleType] = [a1111]
# 日后 (示例) : 
# from scripts.parsers import comfyui, fooocus
# _PARSERS = [comfyui, a1111, fooocus]
