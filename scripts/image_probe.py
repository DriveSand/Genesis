"""image_probe.py — 图片尺寸探测 (与元数据解析解耦)。

只读取图片头部获取宽高, 不做任何元数据解析, 也不访问数据库。
用于保证即使图片没有可解析的生成元数据, 分辨率仍然可以入库。
"""
from PIL import Image

from scripts.logger import log


def probe_image_size(physical: str) -> tuple[int, int]:
    """返回图片 (宽度, 高度); 非图片或无法打开时返回 (0, 0)。"""
    try:
        with Image.open(physical) as image:
            width, height = image.size
        return int(width), int(height)
    except Exception as exc:  # noqa: BLE001 非图片/损坏文件按未知尺寸处理
        log.debug(f"Image size probe failed: {physical} ({exc})")
        return 0, 0
