"""FastAPI 应用组装: 路由注册、异常映射、前端静态文件服务。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from folder_paths import FolderPath
from scripts.logger import log
from scripts.web_backend.errors import install_exception_handlers
from scripts.web_backend.routers import (
    directories,
    downloads,
    files,
    images,
    search,
    settings,
    uploads,
)
from scripts.web_backend.state import AppState

_DIST_FOLDER = Path(FolderPath.BASE_PATH) / "web" / "dist"


def _mount_frontend(app: FastAPI) -> None:
    """挂载已构建的前端 (web/dist); 文件不存在时返回 index.html (SPA) 。"""
    if not _DIST_FOLDER.is_dir():
        log.warning(f"Frontend build not found, API only: {_DIST_FOLDER}")
        return
    assets = _DIST_FOLDER / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index_file = _DIST_FOLDER / "index.html"

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return FileResponse(index_file)

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_spa(full_path: str) -> FileResponse:
        candidate = (_DIST_FOLDER / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(
                _DIST_FOLDER.resolve()):
            return FileResponse(candidate)
        return FileResponse(index_file)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Genesis", version="1.0.0",
                  docs_url="/api/docs", redoc_url=None)
    app.state.genesis = state
    install_exception_handlers(app)

    for module in (images, directories, search, settings, files, uploads,
                   downloads):
        app.include_router(module.router)

    @app.get("/api/v1/status", tags=["status"])
    def status() -> dict:
        """服务状态 (供前端判断只读模式等) 。"""
        return {"ok": True, "version": state.program_version,
                "database_version": state.db_version,
                "read_only": bool(state.read_only),
                "enable_thumbnail": bool(
                    state.config.get_config("EnableThumbnail"))}

    _mount_frontend(app)
    log.debug("FastAPI application created")
    return app
