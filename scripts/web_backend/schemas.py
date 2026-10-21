"""Web 后端请求体模型 (Pydantic v2) 。"""
from typing import Any, Literal

from pydantic import BaseModel, Field

EntryType = Literal["folder", "file"]


# ---------------------------------------------------------------------- #
# 文件操作
# ---------------------------------------------------------------------- #

class DeleteItem(BaseModel):
    type: EntryType
    name: str
    path: str = "/"


class DeleteRequest(BaseModel):
    content: list[DeleteItem] = Field(min_length=1)


class MoveItem(BaseModel):
    type: EntryType
    name: str
    source_path: str
    target_path: str
    destination: Literal["mount", "local"] | None = None


class MoveRequest(BaseModel):
    content: list[MoveItem] = Field(min_length=1)


class RenameRequest(BaseModel):
    type: EntryType
    name: str
    path: str = "/"
    new_name: str


class CreateFolderRequest(BaseModel):
    path: str = "/"
    name: str
    destination: Literal["mount", "local"] | None = None
    auto_rename: bool = False  # 同名时自动追加 " (N)" 并返回实际名称


# ---------------------------------------------------------------------- #
# 搜索
# ---------------------------------------------------------------------- #

class IncludeExclude(BaseModel):
    include: list[str] = []
    exclusion: list[str] = []


class RangeRule(BaseModel):
    bigger_than: int = 0
    smaller_than: int = 0


class TimeRule(BaseModel):
    after: int = 0
    before: int = 0


class ModelRule(BaseModel):
    name: str
    ver: str = ""


class BasicSearchRequest(BaseModel):
    content: list[str] = []
    path: list[str] = []
    max_quantity: int = 50
    cursor: str | None = None


class AdvancedSearchRequest(BaseModel):
    path: IncludeExclude = IncludeExclude()
    file_name: IncludeExclude = IncludeExclude()
    file_type: list[str] = []
    file_size: list[RangeRule] = []
    width: list[RangeRule] = []
    height: list[RangeRule] = []
    sha256: list[str] = []
    prompt: IncludeExclude = IncludeExclude()
    neg_prompt: IncludeExclude = IncludeExclude()
    ckpt: list[ModelRule] = []
    lora: list[ModelRule] = []
    embed: list[ModelRule] = []
    tool: list[str] = []
    seed: list[int] = []
    sampler: list[str] = []
    scheduler: list[str] = []
    add_time: list[TimeRule] = []
    rating: list[int] = []
    note: list[str] = []
    max_quantity: int = 50
    cursor: str | None = None


# ---------------------------------------------------------------------- #
# 元数据修改
# ---------------------------------------------------------------------- #

class TargetRef(BaseModel):
    name: str
    path: str = "/"


class ModelEntry(BaseModel):
    model_name: str
    version: str = ""
    model_url: str | None = None
    weight: float | None = None
    type: int = 0


class MetadataChange(BaseModel):
    positive_prompt: str | None = None
    negative_prompt: str | None = None
    tool: str | None = None
    seed: int | None = None
    steps: int | None = None
    cfg: float | None = None
    sampler: str | None = None
    scheduler: str | None = None
    rating: int | None = None
    note: str | None = None
    checkpoint: list[ModelEntry] | None = None
    lora: list[ModelEntry] | None = None
    embedding: list[ModelEntry] | None = None
    vae: list[ModelEntry] | None = None


class MetadataUpdateRequest(BaseModel):
    target: TargetRef
    change: MetadataChange


# ---------------------------------------------------------------------- #
# 上传
# ---------------------------------------------------------------------- #

class UploadFileItem(BaseModel):
    name: str
    mime: str = ""
    sha256: str
    size: int = 0


class PrepareUploadRequest(BaseModel):
    target: str = "/"
    files: list[UploadFileItem] = Field(min_length=1)
    destination: Literal["mount", "local"] | None = None


class FolderCheckRequest(BaseModel):
    target: str = "/"
    paths: list[str] = []


# ---------------------------------------------------------------------- #
# 配置
# ---------------------------------------------------------------------- #

class SettingsUpdateRequest(BaseModel):
    change: dict[str, Any] = Field(min_length=1)


# ---------------------------------------------------------------------- #
# 下载
# ---------------------------------------------------------------------- #

class DownloadFolder(BaseModel):
    name: str
    path: str = "/"


class DownloadFile(BaseModel):
    name: str
    path: str = "/"


class DownloadBatchRequest(BaseModel):
    folders: list[DownloadFolder] = []
    files: list[DownloadFile] = []
