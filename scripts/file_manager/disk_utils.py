"""磁盘操作辅助: 创建、重命名、删除、复制、重名处理与写入完成等待。"""
from __future__ import annotations

import os
import time
from typing import Any

from exceptions import FileNotFoundError as FMFileNotFound
from exceptions import NameConflictError, SHA256Error
from scripts.file_manager.path_utils import split_name_ext
from scripts.sha256 import CHUNK_SIZE
from scripts.logger import log

UPLOAD_MAX_TRIALS = 100   # (n) 后缀最多尝试次数
COPY_MAX_RETRY = 3        # 跨文件系统复制后哈希校验失败的最大重复制次数

# 入库前等待文件写入完成: 轮询间隔 / 最长等待 / 空文件判定
READY_POLL_INTERVAL = 0.1
READY_TIMEOUT = 10.0
READY_EMPTY_TIMEOUT = 2.0


def touch(physical: str, is_dir: bool = False) -> None:
    """在磁盘上创建空文件夹或空文件。"""
    if is_dir:
        os.makedirs(physical)
    else:
        with open(physical, "wb"):
            pass


def rename_on_disk(parent_phys: str, old_name: str, new_name: str,
                   lp: str) -> None:
    src = os.path.join(parent_phys, old_name)
    dst = os.path.join(parent_phys, new_name)
    if not os.path.exists(src):
        raise FMFileNotFound(f"Target not found: {lp}/{old_name}")
    if os.path.exists(dst):
        raise NameConflictError(f"Name already taken: {lp}/{new_name}")
    os.replace(src, dst)


def rmtree(physical: str) -> None:
    """递归删除目录树 (仅用 os, 不引入额外库)。"""
    for entry in os.listdir(physical):
        p = os.path.join(physical, entry)
        if os.path.isdir(p):
            rmtree(p)
        else:
            os.remove(p)
    os.rmdir(physical)


def unique_name(target_dir: str, filename: str) -> str:
    """重名时自动添加 (n) 后缀: file.png -> file(1).png; 保留完整扩展名。

    使用 os.path.exists() 检查, 不依赖数据库; 最多尝试 100 次。
    """
    stem, ext = split_name_ext(filename)
    for n in range(UPLOAD_MAX_TRIALS + 1):
        candidate = filename if n == 0 else f"{stem}({n}){ext}"
        if not os.path.exists(os.path.join(target_dir, candidate)):
            if n > 0:
                log.info(f"Duplicate name, auto-appended (n) suffix: "
                         f"{filename} -> {candidate}")
            return candidate
    raise NameConflictError(
        f"Failed to find an available filename after "
        f"{UPLOAD_MAX_TRIALS} attempts: {filename}")


def copy_file(src: str, dst: str) -> None:
    """分块复制文件; 失败时清理半成品。"""
    try:
        with open(src, "rb") as fs, open(dst, "wb") as fd:
            while True:
                chunk = fs.read(CHUNK_SIZE)
                if not chunk:
                    break
                fd.write(chunk)
    except OSError:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise


def copy_verified(src: str, dst: str, expected_sha: str, sha: Any) -> str:
    """跨文件系统校验复制: 复制后二次比对哈希, 不一致重新复制。

    最多尝试 COPY_MAX_RETRY 次, 通过校验后返回 SHA256;
    仍失败则清理目标半成品并抛 SHA256Error。
    """
    for attempt in range(1, COPY_MAX_RETRY + 1):
        copy_file(src, dst)
        digest, hash_err = sha.compute(dst)
        if digest is not None and digest == expected_sha:
            if attempt > 1:
                log.warning(
                    f"Cross-filesystem copy verified on retry: {dst} "
                    f"(attempt {attempt}/{COPY_MAX_RETRY})")
            return digest
        log.warning(
            f"Cross-filesystem copy SHA256 mismatch, re-copying: {dst} "
            f"(attempt {attempt}/{COPY_MAX_RETRY}"
            + (f", hash error: {hash_err}" if hash_err else "") + ")")
    try:
        os.remove(dst)
    except OSError:
        pass
    raise SHA256Error(
        f"Cross-filesystem copy SHA256 mismatch after "
        f"{COPY_MAX_RETRY} attempts: {dst}")


def wait_file_ready(physical: str,
                    timeout: float = READY_TIMEOUT) -> bool:
    """等待新文件写入完成 (大小、mtime 连续一次轮询不变且非空)。

    文件创建事件通常先于写入完成触发; 若立即计算哈希/探测分辨率,
    会把半成品 (甚至 0 字节) 当作最终内容入库, 导致空哈希记录、
    无分辨率、元数据解析失败。写入超过 timeout 仍为空视为空文件。
    """
    start = time.monotonic()
    deadline = start + timeout
    last: tuple[int, int] | None = None
    while True:
        try:
            st = os.stat(physical)
        except OSError:
            return False
        current = (int(st.st_size), int(st.st_mtime_ns))
        if st.st_size > 0 and current == last:
            return True
        if st.st_size == 0 and time.monotonic() - start >= READY_EMPTY_TIMEOUT:
            return False  # 持续为空: 视为空文件 (空文件不参与入库)
        if time.monotonic() >= deadline:
            return st.st_size > 0
        last = current
        time.sleep(READY_POLL_INTERVAL)


__all__ = [
    "COPY_MAX_RETRY",
    "READY_EMPTY_TIMEOUT",
    "READY_POLL_INTERVAL",
    "READY_TIMEOUT",
    "UPLOAD_MAX_TRIALS",
    "copy_file",
    "copy_verified",
    "rename_on_disk",
    "rmtree",
    "touch",
    "unique_name",
    "wait_file_ready",
]
