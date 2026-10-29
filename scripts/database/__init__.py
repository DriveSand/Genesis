"""Genesis 数据库层 (包入口)。

模块划分:
- creation.py:  连接工厂、建表、根目录记录;
- upgrade.py:   结构版本检查与迁移;
- engine.py:    连接管理、写线程/读线程池、WAL checkpoint、生命周期;
- service.py:   对外门面 DataBase (只负责派发);
- search.py:    文件搜索;
- table/:       path / info / file / model 表的 SQL 实现。

对外接口保持与旧版 scripts/database.py 一致。
"""
from scripts.database.service import DataBase, close, init
from scripts.database.upgrade import DB_VERSION

__all__ = ["DataBase", "DB_VERSION", "init", "close"]
