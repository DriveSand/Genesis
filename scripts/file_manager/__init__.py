"""scripts/file_manager —— 文件操作模块包。

模块划分:
- manager.py:        FileManager 门面 (组装、启动/关闭、对外服务);
- operations.py:     文件 CRUD 与入库/元数据解析;
- upload.py:         上传流程 (临时文件、分块合并、移动入库);
- watchdog.py:       文件系统监控与事件同步;
- mount_manager.py:  挂载点监控、离线检测与恢复;
- startup.py:        启动对账;
- readonly.py:       只读保护;
- db_helpers.py:     数据库交互辅助;
- disk_utils.py:     磁盘操作辅助;
- path_utils.py:     逻辑/物理路径工具;
- whitelist.py:      自身操作去重白名单。
"""
from scripts.file_manager.manager import FileManager
from scripts.file_manager.path_utils import join_logical_path, split_logical_path

__all__ = ["FileManager", "join_logical_path", "split_logical_path"]
