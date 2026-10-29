"""version.py —— 程序与数据库结构版本 (唯一来源)。

修改版本号时直接编辑本文件; main.py 与数据库模块从这里读取。
"""

PROGRAM_VERSION = "1.3.0"

# SQLite 结构版本 (PRAGMA user_version) ; 升级逻辑见 scripts/database/upgrade.py
DATABASE_VERSION = 3
