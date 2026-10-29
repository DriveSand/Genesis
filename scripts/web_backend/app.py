"""FastAPI 应用组装: 路由注册、异常映射、前端静态文件服务。"""
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from folder_paths import FolderPath
from scripts.database.engine import interactive_priority
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

# 前端重新构建后必须立即拿到新的 index.html (其中引用带哈希的 JS/CSS);
# 禁止浏览器缓存入口页面, 否则需要手动 Shift+F5 才能刷新。
_INDEX_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


class _DbPriorityMiddleware:
    """把 HTTP 请求作用域内的数据库任务标记为交互优先。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with interactive_priority():
            await self.app(scope, receive, send)


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
        return FileResponse(index_file, headers=_INDEX_HEADERS)

    @app.get("/{full_path:path}", include_in_schema=False)
    def frontend_spa(full_path: str) -> FileResponse:
        candidate = (_DIST_FOLDER / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(
                _DIST_FOLDER.resolve()):
            # 直接访问 /index.html 时同样禁止缓存
            headers = (_INDEX_HEADERS
                       if candidate == index_file.resolve() else None)
            return FileResponse(candidate, headers=headers)
        return FileResponse(index_file, headers=_INDEX_HEADERS)


def create_app(state: AppState) -> FastAPI:
    app = FastAPI(title="Genesis", version="1.0.0",
                  docs_url="/api/docs", redoc_url=None)
    app.state.genesis = state
    app.add_middleware(_DbPriorityMiddleware)
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
