"""database/service.py —— 数据库对外门面 (DataBase)。

本模块只负责把门面方法派发到对应模块, 不直接执行 SQL:
- engine.py:   连接管理、写线程/读线程池、WAL checkpoint、生命周期;
- table/path.py:  path 表;
- table/info.py:  info 表;
- table/file.py:  file 表;
- table/model.py: checkpoint / lora / embedding / vae 与文件-模型关联;
- search.py:      文件搜索。

建表逻辑见 creation.py, 版本检查与迁移见 upgrade.py。
"""
from pathlib import Path
from typing import Any

from folder_paths import FolderPath
from scripts.database.creation import DB_FILE_NAME
from scripts.database.engine import (
    CHECKPOINT_IDLE_MINUTES,
    close,
    init,
    submit_read,
    submit_write,
)
from scripts.database.search import search_files as _search_files
from scripts.database.table import file as file_table
from scripts.database.table import info as info_table
from scripts.database.table import model as model_table
from scripts.database.table import path as path_table
from scripts.database.upgrade import DB_VERSION
from scripts.logger import log


class DataBase:
    """Genesis 数据库封装: 写操作经单写线程串行事务化执行,
    读操作经读线程池并发执行。生命周期亦可用模块级 init()/close() 管理。
    """

    def __init__(self, db_folder: Path = FolderPath.DATABASE_FOLDER,
                 db_version: int = DB_VERSION,
                 idle_checkpoint_minutes: float = CHECKPOINT_IDLE_MINUTES):
        self.db_file = Path(db_folder) / DB_FILE_NAME
        self.db_file.parent.mkdir(parents=True, exist_ok=True)
        init(self.db_file, expected_version=db_version,
             idle_checkpoint_minutes=idle_checkpoint_minutes)
        log.debug(f"Database ready: {self.db_file} (schema v{db_version}, "
                  f"idle checkpoint={idle_checkpoint_minutes} min)")

    def close(self) -> None:
        """关闭数据库 (程序退出时必须调用) 。等待写队列清空与进行中任务完成。"""
        close()  # 模块级 close()

    def __enter__(self) -> "DataBase":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # path 表
    def get_root_path(self) -> dict[str, Any] | None:
        """获取根目录记录。"""
        return submit_read(path_table.get_root_path)

    def add_path(self, name: str, path_type: str = "mount",
                 parent_uuid: str | None = None,
                 path_uuid: str | None = None) -> str:
        """新建路径节点, 返回新节点 uuid。parent_uuid 缺省挂在根目录下。

        path_uuid 缺省时自动生成; 调用方 (如 file_manager) 可自行生成后传入。
        """
        return submit_write(path_table.add_path, name, path_type,
                            parent_uuid, path_uuid)

    def get_path(self, path_uuid: str) -> dict[str, Any] | None:
        return submit_read(path_table.get_path, path_uuid)

    def list_paths(self, parent_uuid: str | None = None) -> list[dict[str, Any]]:
        """列出路径节点; 给定 parent_uuid 时只返回其直接子节点。"""
        return submit_read(path_table.list_paths, parent_uuid)

    def update_path(self, path_uuid: str, name: Any = path_table.UNSET,
                    status: Any = path_table.UNSET,
                    conflict_state: Any = path_table.UNSET,
                    last_checked: Any = path_table.UNSET,
                    path_type: Any = path_table.UNSET) -> bool:
        """更新路径节点字段。未传入的参数保持不变 (显式传 None 表示置空)

        path_type 仅允许在 'inbuilt' / 'mount' 间切换; 把非空目录转换为
        'mount' 会被拒绝。
        """
        return submit_write(path_table.update_path, path_uuid, name, status,
                            conflict_state, last_checked, path_type)

    def move_path(self, path_uuid: str, parent_uuid: str, name: str) -> bool:
        """移动/重命名目录节点 (保留节点 uuid, 子树与文件记录不变)。"""
        return submit_write(path_table.move_path, path_uuid, parent_uuid, name)

    def delete_path(self, path_uuid: str) -> bool:
        """删除空路径节点 (存在子节点或关联文件时禁止删除) 。"""
        return submit_write(path_table.delete_path, path_uuid)

    def resolve_path(self, path_uuid: str) -> str | None:
        """解析路径节点的完整逻辑路径 (自根目录起, '/' 连接, 不含 ROOT)。

        恒以 "/" 开头: 根节点返回 "/", 其余形如 "/abc/123"；节点不存在返回 None。
        """
        return submit_read(path_table.resolve_path, path_uuid)

    # info 表
    def create_info(
        self,
        sha256: str,
        *,
        seed: int = 0, # 应该没几个人用 0 做随机种吧
        steps: int = 0,
        cfg: float = 0.0,
        sampler: str = "",
        scheduler: str = "",
        tool: str = "",
        rating: int = 1,
        positive_prompt: str = "",
        negative_prompt: str = "",
        note: str = "",
        files: list[dict[str, Any]] | None = None,
        checkpoints: list[dict[str, Any] | str] | None = None,
        loras: list[dict[str, Any] | str] | None = None,
        embeddings: list[dict[str, Any] | str] | None = None,
        vaes: list[dict[str, Any] | str] | None = None,
        upscales: list[dict[str, Any] | str] | None = None,
    ) -> int:
        """新建 info 记录, 可在同一事务内附带文件与模型记录, 返回 info_id。

        files 元素: {"path_uuid", "filename", "mime_type",
        "width"?, "height"?, "file_size"?} (尺寸/大小属于单个文件)
        checkpoints / embeddings / vaes 元素: {"name", "version"?, "url"?} 或纯名称字符串
        loras 元素: {"name", "weight", "model_type"?(0=LoRA,1=LyCORIS), "version"?, "url"?}
        模型会绑定到 files 中的每个文件; 未提供文件时不能附带模型。
        """
        return submit_write(
            info_table.create_info, sha256, seed, steps,
            cfg, sampler, scheduler, tool, rating, positive_prompt,
            negative_prompt, note, files, checkpoints, loras, embeddings,
            vaes, upscales,
        )

    def get_info(self, info_id: int) -> dict[str, Any] | None:
        return submit_read(info_table.get_info, info_id)

    def get_info_by_sha256(self, sha256: str) -> dict[str, Any] | None:
        return submit_read(info_table.get_info_by_sha256, sha256)

    def list_info(self, rating: int | None = None, start_time: int | None = None,
                  end_time: int | None = None, limit: int = 50,
                  offset: int = 0) -> list[dict[str, Any]]:
        """按条件分页列出 info 记录 (按创建时间倒序)。"""
        return submit_read(info_table.list_info, rating, start_time, end_time,
                           limit, offset)

    def count_info(self, rating: int | None = None, start_time: int | None = None,
                   end_time: int | None = None) -> int:
        """与 list_info 相同条件下的记录总数 (供分页使用)。"""
        return submit_read(info_table.count_info, rating, start_time, end_time)

    def update_info(self, info_id: int, **fields: Any) -> bool:
        """更新 info 字段 (白名单内的列), 自动刷新 updated_at。"""
        return submit_write(info_table.update_info, info_id, dict(fields))

    def delete_info(self, info_id: int) -> bool:
        """删除整条 info 记录 (其文件与文件-模型关联按 CASCADE 联动清理)。"""
        return submit_write(info_table.delete_info, info_id)

    # file 表
    def add_file(self, info_id: int, path_uuid: str, filename: str,
                 mime_type: str, width: int = 0, height: int = 0,
                 file_size: int = 0) -> None:
        """为 info 追加一条文件记录 (可附带单个文件的尺寸/大小)。"""
        self.add_files(info_id, [{
            "path_uuid": path_uuid,
            "filename": filename,
            "mime_type": mime_type,
            "width": width,
            "height": height,
            "file_size": file_size,
        }])

    def add_files(self, info_id: int, files: list[dict[str, Any]]) -> int:
        """批量追加文件记录 (同一事务) , 返回写入条数。

        元素格式: {"path_uuid", "filename", "mime_type",
        "width"?, "height"?, "file_size"?}
        """
        return submit_write(file_table.add_files, info_id, files)

    def update_file(self, path_uuid: str, filename: str, **fields: Any) -> bool:
        """更新单个文件记录的字段 (白名单: mime_type/width/height/file_size)。"""
        return submit_write(file_table.update_file, path_uuid, filename,
                            dict(fields))

    def rename_file(self, path_uuid: str, old_name: str,
                    new_name: str) -> bool:
        """原地重命名文件记录 (保留 file.id 与模型关联)。"""
        return submit_write(file_table.rename_file, path_uuid, old_name,
                            new_name)

    def relocate_file(self, src_path_uuid: str, old_name: str,
                      dst_path_uuid: str, new_name: str) -> bool:
        """原地移动文件记录到另一目录 (保留 file.id 与模型关联)。"""
        return submit_write(file_table.relocate_file, src_path_uuid, old_name,
                            dst_path_uuid, new_name)

    # 文件关联的模型记录
    def update_file_models(self, path_uuid: str, filename: str,
                           checkpoints: list[dict[str, Any] | str] | None = None,
                           loras: list[dict[str, Any] | str] | None = None,
                           embeddings: list[dict[str, Any] | str] | None = None,
                           vaes: list[dict[str, Any] | str] | None = None,
                           upscales: list[dict[str, Any] | str] | None = None
                           ) -> None:
        """整体修改某个文件使用的模型 (同一事务)

        参数为 None 表示该类模型保持不变; 传空列表表示清空该类。
        模型唯一键为 name + version + url (LoRA 另加 model_type),
        已存在则复用模型注册表记录, 否则新建。
        """
        submit_write(model_table.update_file_models, path_uuid, filename,
                     checkpoints, loras, embeddings, vaes, upscales)

    def get_file_models(self, path_uuid: str,
                        filename: str) -> dict[str, list[dict[str, Any]]]:
        """获取某个文件关联的全部模型, 按种类分组 (LoRA 含 weight)。"""
        return submit_read(model_table.get_file_models, path_uuid, filename)

    def get_files_by_info(self, info_id: int) -> list[dict[str, Any]]:
        """获取 info 的全部文件记录, 附带解析后的目录路径 (directory)。"""
        return submit_read(file_table.get_files_by_info, info_id)

    def list_files_by_path(self, path_uuid: str) -> list[dict[str, Any]]:
        """列出某路径节点下的全部文件记录。"""
        return submit_read(file_table.list_files_by_path, path_uuid)

    def search_files(self, spec: dict[str, Any], limit: int = 50,
                     after: dict[str, Any] | None = None
                     ) -> list[dict[str, Any]]:
        """按结构化条件搜索文件记录 (基本/高级搜索共用, 供 Web 层分页)。

        spec 支持键 (均缺省为空): content, paths, path_include,
        path_exclude, name_include, name_exclude, prompt_include,
        prompt_exclude, neg_prompt_include, neg_prompt_exclude,
        file_types, sha256, tool, sampler, scheduler, seed, rating, note,
        size_ranges, width_ranges, height_ranges, time_ranges, ckpt, lora,
        embed。同一数组内多元素取 OR, 不同条件间取 AND。

        after: 上一页最后一个对象 {name, sha256, path}。
        返回项: name/sha256/rating/file_size/add_time/path/info_id/path_uuid。
        """
        return submit_read(_search_files, spec, max(1, min(int(limit), 200)),
                           after)

    def delete_file(self, path_uuid: str, filename: str) -> bool:
        """删除单条文件记录 (仅删 file 表, 不触碰 info 与模型记录)。"""
        return submit_write(file_table.delete_file, path_uuid, filename)

    def delete_files_by_info(self, info_id: int) -> int:
        """删除 info 名下全部文件记录 (仅删 file 表), 返回删除条数。"""
        return submit_write(file_table.delete_files_by_info, info_id)

    # 模型查询 (去重与反查)
    def list_model_names(self, kind: str) -> list[dict[str, Any]]:
        """列出某种模型的去重名称及关联文件数量。

        kind: checkpoint / lora / embedding / vae
        返回: [{"name", "file_count"}, ...]
        """
        return submit_read(model_table.list_model_names, kind)

    def find_infos_by_model(self, name: str) -> list[dict[str, Any]]:
        """按模型名称反查: 经 file_* 中间表关联 info 与 file。

        返回 list, 每项为 info 字段 + matched_models (命中的模型记录)
        + files(完整路径在 directory 字段). 注意: files 可能为空列表,
        (info 存在但无关联文件) 由应用层自行处理。
        """
        return submit_read(model_table.find_infos_by_model, name)

    # 主表孤儿记录清理
    def count_orphan_info(self) -> int:
        """统计 file 表中已无任何关联文件的 info 记录数量 (供用户确认)。"""
        return submit_read(info_table.count_orphan_info)

    def cleanup_orphan_info(self) -> int:
        """删除已无关联文件的 info 记录 (模型表经 CASCADE 联动清理), 返回删除数。"""
        return submit_write(info_table.cleanup_orphan_info)
