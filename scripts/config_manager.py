"""config_manager.py —— 应用配置与外部挂载点配置管理。

两个配置文件的生命周期：
- config.json：构造时读取一次，运行时可修改并立即原子回写硬盘
  （先写同目录临时文件再 os.replace，避免进程中断留下损坏的配置文件）
- external_folder.json：仅在启动时通过 load_external_folders 读取一次，
  加载完成后无论本地文件如何变更都不会重新读取

硬性约束：本模块不执行任何 SQL，路径表写操作全部委托 database 模块。
"""
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from exceptions import (
    ConfigReadError,
    ConfigWriteError,
    RequestKeyNotFound,
    RequestContentIncorrect
)
from scripts.logger import log
from folder_paths import FolderPath

if TYPE_CHECKING:
    from scripts.database import DataBase

CONFIG_FILE_NAME = "config.json"
EXTERNAL_FOLDER_FILE = "external_folder.json"

DEFAULT_CONFIG: dict[str, Any] = {
    # 以下配置会随客户端更改动态修改
    "EnableThumbnail": True,  # 启用预览图预览（开启以后会同步开启预览图生成）
    "ThumbnailShortSideSize": 512,  # 缩略图的短边像素长度（最低64，最高1024）
    "NewlyAddedImageLevel": 1,  # 新增图片的默认分级
    "GridCellSize": 240,  # 网格单元大小（控制图片显示的大小。最低64，最高1024）
    "AutoRefresh": True,  # 前端自动刷新
    "ShowLevel": [0,1,2,3,4], # 想要看见的图片等级
    "SafeBrowsingMode": False,  # 安全浏览模式（只显示G级别的图像）
    "BlurLevel": 2,  # 开始模糊的等级。 0级为G；1级为PG-13；2级为R；3级为X；4级为XXX
    # 数据库写空闲多少分钟后自动执行 WAL checkpoint（0 = 关闭）。重启程序生效
    "DatabaseIdleCheckpointMinutes": 10,
}


@dataclass(frozen=True)
class MountConfig:
    """单个外部挂载点配置。

    readonly 为 True 时该挂载点及其所有子目录禁止写入文件。
    """
    logical_path: str
    physical_path: str
    readonly: bool
    priority: int


# external_folder.json 解析辅助
def _detect_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """json.load 的 object_pairs_hook：检测重复 key。

    发现重复时 log.warning 列出，保留最后一个值（与 json 默认行为一致）。
    """
    seen: dict[str, Any] = {}
    dupes: list[str] = []
    for key, value in pairs:
        if key in seen:
            dupes.append(key)
        seen[key] = value
    if dupes:
        log.warning(f"Duplicate keys in config file, keeping the last value: {dupes}")
    return seen


def _validate_logical_path(logical_path: str) -> str | None:
    """校验逻辑路径，返回错误原因；合法返回 None。"""
    if not logical_path.startswith("/"):
        return "must start with '/'"
    if ".." in logical_path:
        return "must not contain '..'"
    if "//" in logical_path:
        return "must not contain '//'"
    if logical_path == "/":
        return "root path cannot be mounted"
    return None


def _validate_entry(logical_path: str, entry: Any) -> tuple[MountConfig | None, str | None]:
    """校验单个挂载条目；成功返回 (MountConfig, None)，失败返回 (None, 原因)。"""
    reason = _validate_logical_path(logical_path)
    if reason is not None:
        return None, f"Invalid logical path: {reason}"
    if not isinstance(entry, dict):
        return None, "mount entry must be a JSON object"

    physical_path = entry.get("physical_path")
    if not isinstance(physical_path, str) or not physical_path:
        return None, "physical_path is missing or not a string"
    if not os.path.isabs(physical_path):
        return None, f"physical_path is not an absolute path: {physical_path}"
    if not os.path.isdir(physical_path):
        return None, f"physical_path does not exist or is not a directory: {physical_path}"

    readonly = entry.get("readonly")
    if not isinstance(readonly, bool):
        return None, "readonly must be a boolean"

    # bool 是 int 的子类，需显式排除
    priority = entry.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        return None, "priority must be a non-negative integer"

    return MountConfig(logical_path, physical_path, readonly, priority), None


def _norm_physical(physical_path: str) -> str:
    """物理路径规范化（用于判重）：统一分隔符，Windows 下忽略大小写。"""
    return os.path.normcase(os.path.normpath(physical_path))


def _path_len(logical_path: str) -> int:
    """逻辑路径去掉全部 '/' 后的字符长度（冲突消解第二优先级）。"""
    return len(logical_path.replace("/", ""))


def _mount_depth(logical_path: str) -> int:
    """逻辑路径的层级数（路径表节点创建顺序：浅层优先）。"""
    return len([s for s in logical_path.split("/") if s])


def _ensure_mount_path(db: "DataBase", logical_path: str) -> str | None:
    """确保逻辑路径在路径表中存在，逐级创建缺失节点，返回挂载目标节点 uuid。

    - 新建中间节点 type='inbuilt'，最终挂载目标节点 type='mount'
    - 已存在的空内置目录会转换为挂载点；非空目录则跳过
    - status='normal'、conflict_state=NULL 由 database 模块保证
    - 途中发现同名节点已是挂载点（非最终层）时记录错误并跳过
    """
    segments = [s for s in logical_path.split("/") if s]
    if not segments:
        log.error(f"Empty mount path segments: {logical_path}")
        return None

    root = db.get_root_path()
    if root is None:
        log.error("Path table has no root node, cannot create mount paths")
        return None

    current = root["uuid"]
    for i, seg in enumerate(segments):
        is_final = i == len(segments) - 1
        # 每层只查询一次子节点，构建 name -> node 索引
        children = {c["name"]: c for c in db.list_paths(current)}
        existing = children.get(seg)
        if existing is not None:
            if existing["type"] == "mount":
                if is_final:
                    # 目标节点已是挂载点：视为重复挂载，幂等复用
                    return existing["uuid"]
                log.error(
                    f"Failed to create mount path '{logical_path}': "
                    f"intermediate node '{seg}' is already a mount point"
                )
                return None
            # type == 'inbuilt'
            if is_final:
                # 空目录可以作为挂载点: 转换为 mount 类型
                if (db.list_paths(existing["uuid"])
                        or db.list_files_by_path(existing["uuid"])):
                    log.error(
                        f"Failed to create mount path '{logical_path}': "
                        f"target directory '{seg}' is not empty")
                    return None
                try:
                    db.update_path(existing["uuid"], path_type="mount")
                except Exception as e:  # noqa: BLE001
                    log.error(
                        f"Failed to convert empty directory to mount point "
                        f"'{logical_path}': {e}")
                    return None
                log.info(f"Empty directory converted to mount point: "
                         f"{logical_path}")
                return existing["uuid"]
            current = existing["uuid"]
        else:
            node_type = "mount" if is_final else "inbuilt"
            try:
                current = db.add_path(seg, path_type=node_type, parent_uuid=current)
            except Exception as e:
                log.error(
                    f"Failed to create node '{seg}' for mount path "
                    f"'{logical_path}': {e}"
                )
                return None
    return current


class ConfigManager:
    """应用配置（config.json）与外部挂载点（external_folder.json）管理。"""

    def __init__(self, data_folder: Path = FolderPath.DATA_FOLDER):
        self.config_file = Path(data_folder) / CONFIG_FILE_NAME
        # 外部挂载点（启动时由 load_external_folders 一次性加载）
        self.external_mounts: dict[str, MountConfig] = {}
        # 串行化运行时的配置读取与回写
        self._lock = threading.RLock()
        self.config: dict[str, Any] = self._load_config()

    # ---------------------------------------------------------------- #
    # config.json 读写
    # ---------------------------------------------------------------- #

    def _save_config(self) -> None:
        """原子回写配置文件：先写同目录临时文件，成功后 os.replace 覆盖，
        避免进程中断留下损坏或半写入的 config.json。"""
        tmp_file = self.config_file.parent / (self.config_file.name + ".tmp")
        try:
            # 配置目录缺失时自愈（正常时序下由 FolderPath.create_folder 保证）
            self.config_file.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_file, "w", encoding="utf-8") as file:
                json.dump(self.config, file, indent=1, ensure_ascii=False)
            os.replace(tmp_file, self.config_file)
        except (OSError, TypeError, ValueError) as e:
            # TypeError/ValueError：配置值不可 JSON 序列化
            try:
                tmp_file.unlink(missing_ok=True)
            except OSError:
                pass
            log.error(f"Failed to save config: {e}")
            raise ConfigWriteError(f"Failed to save config file: {e}") from e

    def _load_config(self) -> dict[str, Any]:
        """读取配置文件并补全缺失的默认项；文件不存在时以默认值创建。"""
        if not self.config_file.exists():  # 配置文件不存在时自动创建
            log.info("Config file not found, creating with default settings")
            self.config = dict(DEFAULT_CONFIG)
            self._save_config()
            return self.config

        try:
            with open(self.config_file, "r", encoding="utf-8") as data:
                loaded = json.load(data)
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"Failed to load config: {e}")
            raise ConfigReadError(f"Failed to read config file: {e}") from e

        if not isinstance(loaded, dict):
            raise ConfigReadError(
                "Config file top-level must be a JSON object, got "
                f"{type(loaded).__name__}"
            )

        # 识别本地配置中缺失的默认项，并自动补全
        missing = [key for key in DEFAULT_CONFIG if key not in loaded]
        if missing:
            for key in missing:
                loaded[key] = DEFAULT_CONFIG[key]
            log.info(f"Missing config keys filled: {missing}")
            self.config = loaded
            self._save_config()

        return loaded

    def get_config(self, key: str) -> Any:
        # 获取配置
        with self._lock:
            if key in self.config:
                return self.config[key]
            raise RequestKeyNotFound(f"Request key({key}) not found")

    def get_configs(self, keys: list[str]) -> dict[str, Any]:
        # 批量获取配置
        return {key: self.get_config(key) for key in keys}

    def update_config(self, key: str, value: Any) -> None:
        # 更新单个配置项（校验与回写逻辑复用批量更新）
        self.update_configs({key: value})

    def update_configs(self, updates: dict[str, Any]) -> None:
        # 批量更新配置文件；任一键非法则整体不生效，保存失败时回滚内存状态
        if not updates:
            return
        with self._lock:
            for key, value in updates.items():
                if key not in self.config:
                    raise RequestKeyNotFound(f"Request key({key}) not found")
                if value is None:
                    raise RequestContentIncorrect(
                        f"Requested value for key({key}) is empty or incorrect"
                    )
            backup = {key: self.config[key] for key in updates}
            self.config.update(updates)
            try:
                self._save_config()
            except Exception:
                # 回滚内存状态，保持与硬盘内容一致
                self.config.update(backup)
                raise

    # ---------------------------------------------------------------- #
    # external_folder.json 外部挂载点加载（仅启动时调用一次）
    # ---------------------------------------------------------------- #

    def load_external_folders(self, db: "DataBase") -> tuple[dict[str, MountConfig], list[str]]:
        """从项目根目录读取 external_folder.json 并挂载。

        仅启动时调用一次；加载完成后本地文件变更不会触发重新读取。

        流程：字段校验 → 物理路径冲突消解 → 路径表节点逐级创建。
        路径表写操作全部委托 database 模块，本模块不执行任何 SQL。

        :param db: DataBase 实例（由调用方创建并负责关闭）
        :return: (成功挂载的 dict[逻辑路径, MountConfig], 被跳过的逻辑路径列表)
        """
        skipped: list[str] = []

        def abort(reason: str) -> tuple[dict[str, MountConfig], list[str]]:
            # 读取阶段的整体兜底：不挂载任何外部目录，不向调用方抛异常
            log.error(reason)
            self.external_mounts = {}
            return {}, []

        ext_file = FolderPath.BASE_PATH / EXTERNAL_FOLDER_FILE
        if not ext_file.exists():
            self.external_mounts = {}
            return {}, []  # 无此文件则静默跳过

        log.info(f"Loading external folder config: {ext_file}")
        try:
            with open(ext_file, "r", encoding="utf-8") as f:
                data = json.load(f, object_pairs_hook=_detect_duplicate_keys)
        except (json.JSONDecodeError, OSError) as e:
            return abort(f"Failed to load external folder config: {e}")
        if not isinstance(data, dict):
            return abort("External folder config top-level must be a JSON object")

        # 1) 逐条校验
        valid: dict[str, MountConfig] = {}
        for logical_path, entry in data.items():
            mc, reason = _validate_entry(logical_path, entry)
            if mc is None:
                log.error(f"Invalid mount entry, skipped '{logical_path}': {reason}")
                skipped.append(logical_path)
            else:
                valid[logical_path] = mc

        # 2) 物理路径冲突消解（规范化后相同视为同一物理路径）
        groups: dict[str, list[MountConfig]] = {}
        for mc in valid.values():
            groups.setdefault(_norm_physical(mc.physical_path), []).append(mc)

        winners: dict[str, MountConfig] = {}
        for norm_path, group in groups.items():
            # 1. priority 小者胜 → 2. 去 "/" 后路径短者胜 → 3. 逻辑路径默认排序靠前者胜
            ordered = sorted(
                group,
                key=lambda c: (c.priority, _path_len(c.logical_path), c.logical_path),
            )
            winner = ordered[0]
            winners[winner.logical_path] = winner
            winner_len = _path_len(winner.logical_path)
            for loser in ordered[1:]:
                log.warning(
                    "Physical path conflict on '%s': mount '%s' is selected "
                    "(priority=%d, path_len=%d), mount '%s' is not mounted "
                    "(priority=%d, path_len=%d).",
                    norm_path,
                    winner.logical_path, winner.priority, winner_len,
                    loser.logical_path, loser.priority, _path_len(loser.logical_path),
                )
                skipped.append(loser.logical_path)

        # 3) 为胜者逐级创建路径表节点（浅层优先，避免父节点缺失或误判）
        mounted: dict[str, MountConfig] = {}
        for logical_path in sorted(winners, key=lambda lp: (_mount_depth(lp), lp)):
            target_uuid = _ensure_mount_path(db, logical_path)
            if target_uuid is None:
                skipped.append(logical_path)
            else:
                mounted[logical_path] = winners[logical_path]

        self.external_mounts = mounted
        log.info(
            f"External folders loaded: {len(mounted)} mounted, {len(skipped)} skipped"
        )
        return mounted, skipped
