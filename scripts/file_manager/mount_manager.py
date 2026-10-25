"""挂载点管理: 监控注册、健康检查、离线确认与恢复轮询。"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Callable

from exceptions import PathNotFoundError
from scripts.file_manager.path_utils import is_accessible
from scripts.logger import log
from scripts.path_resolver import GALLERY

if TYPE_CHECKING:
    from scripts.file_manager.manager import FileManager

OFFLINE_CONFIRM_DELAY = 5.0    # 离线确认期 (秒)
HEALTH_CHECK_INTERVAL = 30.0   # 挂载点健康检查间隔 (秒)
RECOVERY_POLL_INTERVAL = 30.0  # 离线恢复轮询间隔 (秒)


class MountManager:
    """维护挂载点在线状态, 并驱动 watchdog 注册/注销与恢复轮询。"""

    def __init__(self, manager: "FileManager") -> None:
        self._mgr = manager
        self.state: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.Lock()
        self._timers: set[threading.Timer] = set()
        self._timers_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def shutdown(self) -> None:
        with self._timers_lock:
            timers = list(self._timers)
        for timer in timers:
            timer.cancel()

    def update_mount_configs(self, mount_configs: dict) -> None:
        """挂载配置变更时更新内存配置、只读缓存并补注册新挂载点监控。"""
        mgr = self._mgr
        mgr.resolver.update_mount_configs(mount_configs)
        mgr.readonly.build()
        for mount_key, mc in mgr.resolver.iter_mounts():
            if mount_key not in self.state:
                self.init_mount_watch(mount_key, mc)

    # ------------------------------------------------------------------ #
    # 监控注册
    # ------------------------------------------------------------------ #

    def init_mount_watch(self, mount_key: str, mc: Any) -> None:
        """挂载点: 路径表中已有记录才注册监控, 并启动健康检查。"""
        mgr = self._mgr
        segments = [s for s in mount_key.split("/") if s]
        logical = GALLERY + mount_key
        state: dict[str, Any] = {
            "physical": mc.physical_path,
            "logical": logical,
            "segments": segments,
            "online": True,
            "confirming": False,
            "watch": None,
        }
        self.state[mount_key] = state
        try:
            with mgr.db_lock:
                mgr.resolver.walk_segments(segments)  # 路径表记录存在才允许监控
        except PathNotFoundError:
            log.warning(f"Mount has no path-table record, watch deferred: "
                        f"{mount_key}")
            state["online"] = False
            self.spawn_timer(RECOVERY_POLL_INTERVAL,
                             self.recovery_poll, mount_key)
            return
        watch = mgr.watch.start_watch(mc.physical_path, logical)
        if watch is None:
            state["online"] = False
            self.spawn_timer(RECOVERY_POLL_INTERVAL,
                             self.recovery_poll, mount_key)
            return
        state["watch"] = watch
        # 上次运行遗留的 offline 状态在本次挂载成功时复位
        self.set_mount_status(state, "normal")
        self.spawn_timer(HEALTH_CHECK_INTERVAL, self.health_check, mount_key)

    def unschedule_mount(self, mount_key: str) -> None:
        mgr = self._mgr
        state = self.state.get(mount_key)
        if state is None:
            return
        watch = state.get("watch")
        if watch is not None:
            try:
                mgr.watch.unschedule(watch)
                log.info(f"Mount watch stopped: {state['logical']}")
            except Exception as e:  # noqa: BLE001
                log.error(f"Failed to remove mount watch: {state['logical']} ({e})")
            state["watch"] = None
        mgr.watch.remove_root(state["physical"])

    # ------------------------------------------------------------------ #
    # 定时器与健康检查
    # ------------------------------------------------------------------ #

    def spawn_timer(self, delay: float, fn: Callable, *args: Any) -> None:
        def _wrapper() -> None:
            try:
                if not self._mgr.shutting_down:
                    fn(*args)
            finally:
                with self._timers_lock:
                    self._timers.discard(timer)

        timer = threading.Timer(delay, _wrapper)
        timer.daemon = True
        with self._timers_lock:
            self._timers.add(timer)
        timer.start()

    def health_check(self, mount_key: str) -> None:
        """在线挂载点定时健康检查 (30 秒链式)。"""
        state = self.state.get(mount_key)
        if state is None or self._mgr.shutting_down:
            return
        if not state["online"]:
            return  # 离线期间本链结束, 恢复在线后由恢复轮询重新启动
        if not is_accessible(state["physical"]):
            self.begin_offline_confirmation(mount_key)
        self.spawn_timer(HEALTH_CHECK_INTERVAL, self.health_check, mount_key)

    def begin_offline_confirmation(self, mount_key: str) -> None:
        with self._state_lock:
            state = self.state.get(mount_key)
            if state is None or state["confirming"] or not state["online"]:
                return
            state["confirming"] = True
        log.warning(f"Mount '{state['logical']}' seems offline, "
                    f"entering {OFFLINE_CONFIRM_DELAY:.0f}s confirmation")
        self.spawn_timer(OFFLINE_CONFIRM_DELAY,
                         self.confirm_offline, mount_key)

    def confirm_offline(self, mount_key: str) -> None:
        state = self.state.get(mount_key)
        if state is None:
            return
        with self._state_lock:
            state["confirming"] = False
        if is_accessible(state["physical"]):
            log.info(f"Mount '{state['logical']}' recovered during confirmation, "
                     f"offline cancelled")
            return

        log.error(f"Mount '{state['logical']}' confirmed offline")
        self.set_mount_status(state, "offline")
        self.unschedule_mount(mount_key)
        with self._state_lock:
            state["online"] = False
        self.spawn_timer(RECOVERY_POLL_INTERVAL,
                         self.recovery_poll, mount_key)

    def set_mount_status(self, state: dict[str, Any], status: str) -> bool:
        """写挂载点路径表状态 (offline/normal), 失败记日志返回 False。"""
        mgr = self._mgr
        try:
            with mgr.db_lock:
                dir_uuid, _ = mgr.resolver.walk_segments(state["segments"])
                mgr.db.update_path(dir_uuid, status=status)
            return True
        except Exception as e:  # noqa: BLE001
            log.error(f"Failed to set mount status '{status}': "
                      f"{state['logical']} ({e})")
            return False

    def recovery_poll(self, mount_key: str) -> None:
        """离线挂载点恢复轮询: 链式轮询, 恢复后重新注册监控。"""
        mgr = self._mgr
        state = self.state.get(mount_key)
        if state is None or mgr.shutting_down:
            return
        log.debug(f"Polling mount '{state['logical']}' ...")
        if not is_accessible(state["physical"]):
            self.spawn_timer(RECOVERY_POLL_INTERVAL,
                             self.recovery_poll, mount_key)
            return
        try:
            with mgr.db_lock:
                mgr.resolver.walk_segments(state["segments"])
        except PathNotFoundError:
            log.warning(f"Mount '{state['logical']}' still missing in path table, "
                        f"retry later")
            self.spawn_timer(RECOVERY_POLL_INTERVAL,
                             self.recovery_poll, mount_key)
            return

        log.info(f"Mount '{state['logical']}' is back online")
        self.set_mount_status(state, "normal")
        watch = mgr.watch.start_watch(state["physical"], state["logical"])
        if watch is None:
            self.spawn_timer(RECOVERY_POLL_INTERVAL,
                             self.recovery_poll, mount_key)
            return
        state["watch"] = watch
        with self._state_lock:
            state["online"] = True
        self.spawn_timer(HEALTH_CHECK_INTERVAL, self.health_check, mount_key)


__all__ = ["MountManager"]
