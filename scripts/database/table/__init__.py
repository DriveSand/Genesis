"""scripts/database/table —— 各数据表的 SQL 实现。

本包中的模块只包含以 ``conn`` 为唯一外部依赖的纯函数:
- path.py:  path 表
- info.py:  info 表
- file.py:  file 表
- model.py: checkpoint / lora / embedding / vae 注册表与文件-模型关联

连接、事务与线程调度见 engine.py, 对外门面见 service.py。
"""
import time

# update_path 的哨兵值: 区分「未传」与「显式传 None」
UNSET = object()


def now() -> int:
    """10 位 Unix 时间戳 (秒)。"""
    return int(time.time())
