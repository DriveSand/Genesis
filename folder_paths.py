from pathlib import Path
from dataclasses import dataclass


@dataclass(frozen=True)
class FolderPath:
    BASE_PATH = Path(__file__).resolve().parent
    DATA_FOLDER = BASE_PATH / "data"
    GALLERY_ROOT = BASE_PATH / "gallery"
    DATABASE_FOLDER = DATA_FOLDER / "db"
    DATABASE_BACKUP_FOLDER = DATABASE_FOLDER / "db_backup"
    CACHE_FOLDER = DATA_FOLDER / "cache"
    UPLOAD_FOLDER = CACHE_FOLDER / "upload"
    IMAGE_CACHE_FOLDER = CACHE_FOLDER / "image"
    VIDEO_CACHE_FOLDER = CACHE_FOLDER / "video"

    def create_folder(self):
        # 自动创建对应目录
        self.DATA_FOLDER.mkdir(parents=True, exist_ok=True)
        self.GALLERY_ROOT.mkdir(parents=True, exist_ok=True)
        self.DATABASE_FOLDER.mkdir(parents=True, exist_ok=True)
        self.DATABASE_BACKUP_FOLDER.mkdir(parents=True, exist_ok=True)
        self.CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
        self.UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        self.IMAGE_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)
        self.VIDEO_CACHE_FOLDER.mkdir(parents=True, exist_ok=True)