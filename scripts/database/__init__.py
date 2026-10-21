"""Genesis 数据库层 (包入口)。

模块划分:
- creation.py: 连接工厂、建表、根目录记录;
- upgrade.py:  结构版本检查与迁移;
- service.py:  对外服务 (全部查询语句、读写引擎与 DataBase 门面)。

对外接口保持与旧版 scripts/database.py 一致。
"""
from scripts.database.service import DataBase, close, init
from scripts.database.upgrade import DB_VERSION

__all__ = ["DataBase", "DB_VERSION", "init", "close"]
