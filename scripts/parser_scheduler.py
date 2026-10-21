"""parser_scheduler.py —— EXIF/元数据解析的唯一对外模块 (调度器) 。

工作流程: 其他模块 -> (调用) -> 本调度器 -> (读取容器元数据, 
选择合适的解析器, 发送 RawMetadata) -> scripts/parsers 内的解析器。

对外接口: 
- read_raw_metadata(file_path)  读取文件原始元数据块 -> RawMetadata (异常显式抛出) 
- parse_raw_metadata(raw)       对已有 RawMetadata 做解析调度 -> dict | None
- parse_metadata(file_path)     file_manager 使用的总入口 -> dict | None (永不抛异常) 

禁令与边界 (模块级) : 
- 本模块是 EXIF 解析的唯一模块, 其他模块禁止自行解析 EXIF; 
- scripts/parsers 包仅本模块可导入, 其他模块禁止直接调用解析器; 
- 本模块及解析器不写 SQL、不调用 DataBase; 
- 对文件只做字节级只读访问 (Image.open / os.path.getsize / os.path.isfile) : 
  file_manager.read_file 是逻辑路径接口, 回调场景只有物理路径, 且回调
  file_manager 会形成重入死锁风险, 故解析入口直接只读打开物理文件——
  不写文件、不建目录、不列目录、不解析逻辑路径。

线程安全: 无全局可变状态, 注册表在 import 时一次性构建, 
可被 file_manager 的线程池并发调用。
"""
from __future__ import annotations

import os
from typing import Any

from PIL import Image, UnidentifiedImageError

from exceptions import (MetadataCorruptError, MetadataParserError,
                        UnsupportedFormatError)
from scripts.logger import log
from scripts.parsers import RawMetadata, iter_parsers

_SUPPORTED_FORMATS = {"PNG", "JPEG", "WEBP"}
_EXIF_IFD = 0x8769         # Exif IFD 指针 tag
_TAG_USER_COMMENT = 37510  # ExifIFD.UserComment

# UserComment 字符集前缀 (EXIF 规范, 8 字节) 
_UC_UNICODE = b"UNICODE\x00"
_UC_ASCII = b"ASCII\x00\x00\x00"
_UC_JIS = b"JIS\x00\x00\x00\x00\x00"


def parse_metadata(file_path: str) -> dict[str, Any] | None:
    """调度总入口 (file_manager 使用) 。任何失败都返回 None, 永不抛异常。"""
    try:
        raw = read_raw_metadata(file_path)
    except UnsupportedFormatError as e:
        log.debug(str(e))    # 非目标格式是常态 (普通照片) , 不告警
        return None
    except MetadataParserError as e:
        log.warning(str(e))  # 声称是图片却读不开: 值得告警
        return None
    try:
        parsed = parse_raw_metadata(raw)
    except Exception as e:  # noqa: BLE001  最后一道兜底, 入库流程绝不因解析崩溃
        log.warning(f"Metadata parsing failed: {file_path} ({e})")
        return None
    if parsed:
        log.info(f"Metadata parsed successfully: {file_path} "
                 f"(tool={parsed.get('tool')})")
    return parsed


def read_raw_metadata(file_path: str) -> RawMetadata:
    """读取图片格式/尺寸/全部候选元数据块。

    :raises UnsupportedFormatError: 文件不存在、非图片、或不在支持格式内
    :raises MetadataCorruptError:   文件损坏/读取失败
    """
    if not os.path.isfile(file_path):
        raise UnsupportedFormatError(f"File not found: {file_path}")
    try:
        file_size = os.path.getsize(file_path)
        with Image.open(file_path) as im:
            fmt = (im.format or "").upper()
            if fmt not in _SUPPORTED_FORMATS:
                raise UnsupportedFormatError(
                    f"Unsupported format: {fmt or 'unknown'} ({file_path})")
            width, height = im.size
            text: dict[str, str] = {}
            user_comment: str | None = None
            if fmt == "PNG":
                im.load()  # 确保 IDAT 之后的文本 chunk 也被读入
                text = {str(k): str(v)
                        for k, v in (getattr(im, "text", None) or {}).items()}
            else:  # JPEG / WEBP: 取 EXIF UserComment
                user_comment = _read_user_comment(im)
            return RawMetadata(file_path=file_path, format=fmt, width=width,
                               height=height, file_size=file_size,
                               text=text, user_comment=user_comment)
    except MetadataParserError:
        raise
    except UnidentifiedImageError as e:
        raise UnsupportedFormatError(f"Unrecognized image: {file_path}") from e
    except (OSError, ValueError, SyntaxError) as e:
        raise MetadataCorruptError(
            f"Failed to read image: {file_path} ({e})") from e


def parse_raw_metadata(raw: RawMetadata) -> dict[str, Any] | None:
    """按注册顺序选择第一个 can_parse()==True 的解析器进行解析。"""
    for parser in iter_parsers():
        tool = getattr(parser, "TOOL_NAME", "?")
        try:
            if not parser.can_parse(raw):
                continue
        except Exception as e:  # noqa: BLE001
            log.warning(
                f"Parser {tool} can_parse failed: {raw.file_path} ({e})")
            continue
        try:
            parsed = parser.parse(raw)
        except Exception as e:  # noqa: BLE001
            log.warning(f"Parser {tool} failed: {raw.file_path} ({e})")
            return None    # 已匹配的解析器即权威: 失败不再级联给其他解析器
        if not parsed:
            return None
        parsed.setdefault("tool", tool)
        parsed.setdefault("width", raw.width)
        parsed.setdefault("height", raw.height)
        parsed.setdefault("file_size", raw.file_size)
        return parsed
    log.debug(f"No matching parser: {raw.file_path}")
    return None


def _read_user_comment(im: Image.Image) -> str | None:
    """读取并解码 EXIF UserComment; 结构损坏返回 None, 不阻断。"""
    try:
        exif = im.getexif()
        uc = exif.get_ifd(_EXIF_IFD).get(_TAG_USER_COMMENT) if exif else None
    except Exception as e:  # noqa: BLE001  EXIF 结构损坏不应阻断
        log.debug(f"EXIF read failed: {im.filename} ({e})")
        return None
    if uc is None:
        return None
    if isinstance(uc, str):                # 个别容器/版本下 PIL 已给 str
        return uc
    if not isinstance(uc, (bytes, bytearray)):
        return None
    data = bytes(uc)
    if data.startswith(_UC_UNICODE):
        payload = data[8:]
        # 实测 civitai 为 UTF-16BE; 个别写端用 LE, 故 BE 优先、LE 兜底
        for enc in ("utf-16-be", "utf-16-le"):
            try:
                return payload.decode(enc)
            except UnicodeDecodeError:
                continue
        log.debug(f"Failed to decode UNICODE UserComment: {im.filename}")
        return None
    if data.startswith(_UC_ASCII):
        return data[8:].decode("ascii", errors="replace")
    if data.startswith(_UC_JIS):
        log.debug(f"JIS-encoded UserComment is not supported: {im.filename}")
        return None
    # 全零前缀 (规范未定义) 或无已知前缀: 按 UTF-8 -> latin-1 兜底
    payload = data[8:] if data[:8] == b"\x00" * 8 else data
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("latin-1", errors="replace")
