"""文件搜索: 结构化条件 -> SQL WHERE, 以及游标分页查询。

搜索只读取 file / info / path 与模型中间表, 不修改任何数据。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from scripts.database.table.model import MODEL_JUNCTIONS, MODEL_TABLES


def like_param(value: Any) -> str:
    """LIKE 参数转义: % 与 _ 按字面匹配 (配合 ESCAPE '\\')。"""
    escaped = (str(value).replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"


def path_like(pattern: str) -> str:
    """目录路径模式 -> LIKE 参数: '*' 为通配符, 其余 % _ \\ 按字面匹配。"""
    out: list[str] = []
    for ch in pattern:
        if ch == "*":
            out.append("%")
        elif ch in ("\\", "%", "_"):
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def path_pattern_clauses(patterns: list[str]) -> list[tuple[str, list[str]]]:
    """基本搜索目录模式 -> OR 条件组 [(SQL, 参数), ...]。

    语义:
    - ``/123``   根目录下的 123 目录 (不含子目录)
    - ``*/123``  任意目录下的 123 目录 (不含子目录, '*' 可跨层级)
    - ``/123/*`` 根目录下的 123 目录及其全部子目录
    - ``123``    旧行为: 目录路径模糊包含
    """
    clauses: list[tuple[str, list[str]]] = []
    for raw in patterns:
        pattern = str(raw).strip()
        if not pattern:
            continue
        if pattern.endswith("/*"):
            base = pattern[:-2].rstrip("/") or "/"
            if base == "/":  # /* 覆盖整个图库
                clauses.append(("1 = 1", []))
                continue
            like = path_like(base)
            clauses.append((
                "(d.full LIKE ? ESCAPE '\\' OR d.full LIKE ? ESCAPE '\\')",
                [like, like + "/%"],
            ))
            continue
        pattern = pattern.rstrip("/") or "/"
        like = path_like(pattern)
        if pattern.startswith("/") or pattern.startswith("*"):
            clauses.append(("d.full LIKE ? ESCAPE '\\'", [like]))
        else:
            # 未以 '/' 或 '*' 开头: 保持目录名模糊匹配
            clauses.append(("d.full LIKE ? ESCAPE '\\'", [f"%{like}%"]))
    return clauses


def search_where(spec: dict[str, Any]) -> tuple[str, list[Any]]:
    """结构化搜索条件 -> WHERE 子句与参数 (供 search_files 使用)。

    语义约定: 同一条件数组内多个元素取 OR, 不同条件之间取 AND;
    include 表示"必须包含", exclusion 表示"不得包含"。
    """
    clauses: list[str] = []
    params: list[Any] = []

    def add_any_like(expr: str, values: list[Any]) -> None:
        """OR 组: 任一值被 expr 包含。"""
        if not values:
            return
        clauses.append(
            "(" + " OR ".join(f"{expr} LIKE ? ESCAPE '\\'" for _ in values) + ")")
        params.extend(like_param(v) for v in values)

    def add_all_not_like(expr: str, values: list[Any]) -> None:
        """排除组: 每个值都不得被 expr 包含。"""
        for v in values:
            clauses.append(f"{expr} NOT LIKE ? ESCAPE '\\'")
            params.append(like_param(v))

    def add_ranges(expr: str, ranges: list[tuple[int, int]]) -> None:
        """数值区间 OR 组: (lo, hi); 0 表示该侧不设限。"""
        if not ranges:
            return
        parts: list[str] = []
        for lo, hi in ranges:
            sub: list[str] = []
            if lo > 0:
                sub.append(f"{expr} > ?")
                params.append(lo)
            if hi > 0:
                sub.append(f"{expr} < ?")
                params.append(hi)
            parts.append("(" + " AND ".join(sub) + ")" if sub else "1 = 1")
        clauses.append("(" + " OR ".join(parts) + ")")

    # 基本搜索: content 匹配"正向提示词或文件名", paths 匹配所在目录
    content = spec.get("content", [])
    if content:
        parts: list[str] = []
        for v in content:
            parts.append("(i.positive_prompt LIKE ? ESCAPE '\\'"
                         " OR f.filename LIKE ? ESCAPE '\\')")
            params.extend((like_param(v), like_param(v)))
        clauses.append("(" + " OR ".join(parts) + ")")
    # 基本搜索目录模式 ($p: 后的引号路径): 同组内取 OR
    path_clauses = path_pattern_clauses(spec.get("paths", []))
    if path_clauses:
        clauses.append("(" + " OR ".join(sql for sql, _ in path_clauses) + ")")
        for _sql, values in path_clauses:
            params.extend(values)

    add_any_like("d.full", spec.get("path_include", []))
    add_all_not_like("d.full", spec.get("path_exclude", []))
    add_any_like("f.filename", spec.get("name_include", []))
    add_all_not_like("f.filename", spec.get("name_exclude", []))
    add_any_like("i.positive_prompt", spec.get("prompt_include", []))
    add_all_not_like("i.positive_prompt", spec.get("prompt_exclude", []))
    add_any_like("i.negative_prompt", spec.get("neg_prompt_include", []))
    add_all_not_like("i.negative_prompt", spec.get("neg_prompt_exclude", []))

    # 文件类型 (扩展名, 小写不带点)
    extensions = [str(e).lower() for e in spec.get("file_types", [])]
    if extensions:
        clauses.append("(" + " OR ".join(
            "LOWER(f.filename) LIKE ?" for _ in extensions) + ")")
        params.extend(f"%.{e}" for e in extensions)

    # 精确匹配 OR 组
    for expr, key in (("i.sha256", "sha256"), ("i.tool", "tool"),
                      ("i.sampler", "sampler"), ("i.scheduler", "scheduler"),
                      ("i.seed", "seed"), ("i.rating", "rating")):
        values = spec.get(key, [])
        if values:
            clauses.append(f"{expr} IN ({', '.join('?' for _ in values)})")
            params.extend(values)

    add_any_like("i.note", spec.get("note", []))
    add_ranges("f.file_size", spec.get("size_ranges", []))
    add_ranges("f.width", spec.get("width_ranges", []))
    add_ranges("f.height", spec.get("height_ranges", []))
    add_ranges("f.added_at", spec.get("time_ranges", []))

    # 模型匹配 (文件级关联): 同名即命中, ver 为空表示该名称下的全部版本
    for kind, key in (("checkpoint", "ckpt"), ("lora", "lora"),
                      ("embedding", "embed")):
        items = spec.get(key, [])
        if not items:
            continue
        table = MODEL_TABLES[kind]
        junction, column = MODEL_JUNCTIONS[kind]
        exists: list[str] = []
        for name, version in items:
            if version:
                exists.append(
                    f"EXISTS (SELECT 1 FROM {junction} j "
                    f"JOIN {table} m ON m.id = j.{column} "
                    "WHERE j.file_id = f.id AND LOWER(m.name) = LOWER(?) "
                    "AND m.version = ?)")
                params.extend((name, version))
            else:
                exists.append(
                    f"EXISTS (SELECT 1 FROM {junction} j "
                    f"JOIN {table} m ON m.id = j.{column} "
                    "WHERE j.file_id = f.id AND LOWER(m.name) = LOWER(?))")
                params.append(name)
        clauses.append("(" + " OR ".join(exists) + ")")

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def search_files(conn: sqlite3.Connection, spec: dict[str, Any],
                 limit: int, after: dict[str, Any] | None
                 ) -> list[dict[str, Any]]:
    """按结构化条件搜索文件记录 (文件 + info + 完整目录路径)。

    排序: added_at 倒序, 文件名升序, 目录升序 (游标恢复所需的稳定次序) 。
    after: 上一页最后一个对象 {name, sha256, path}; 从其后继续。
    """
    where, params = search_where(spec)
    rows = conn.execute(
        f"""
        WITH RECURSIVE dirs(uuid, full) AS (
            SELECT uuid, '/' FROM path WHERE parent_uuid IS NULL
            UNION ALL
            SELECT p.uuid,
                   CASE WHEN d.full = '/' THEN '/' || p.name
                        ELSE d.full || '/' || p.name END
            FROM path p JOIN dirs d ON p.parent_uuid = d.uuid
        )
        SELECT f.filename AS name, i.sha256 AS sha256, i.rating AS rating,
               f.file_size AS file_size, f.added_at AS add_time,
               d.full AS path, f.info_id AS info_id,
               f.path_uuid AS path_uuid
        FROM file f
        JOIN info i ON i.id = f.info_id
        JOIN dirs d ON d.uuid = f.path_uuid
        {where}
        ORDER BY f.added_at DESC, f.filename ASC, d.full ASC
        """,
        params,
    )
    result: list[dict[str, Any]] = []
    skipping = after is not None
    for row in rows:
        item = dict(row)
        if skipping:
            if (item["name"] == after.get("name")
                    and item["sha256"] == after.get("sha256")
                    and item["path"] == after.get("path")):
                skipping = False
            continue
        result.append(item)
        if len(result) >= limit:
            break
    return result
