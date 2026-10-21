"""异常 -> HTTP 响应映射。"""
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from exceptions import (
    APPBaseError,
    ConfigError,
    CrossRootError,
    DatabaseError,
    DirectoryNotEmptyError,
    FileManagerError,
    FileNotFoundError as FMFileNotFound,
    InputDataError,
    InvalidPathError,
    MetadataParserError,
    NameConflictError,
    PathNotFoundError,
    ReadOnlyError,
    RequestContentIncorrect,
    RequestKeyNotFound,
    SHA256Error,
    UserChoiceRequired,
)
from scripts.logger import log

# 文件管理异常没有内置错误码, 由 Web 层分配 (便于前端区分)
_FM_CODES: dict[type, int] = {
    InvalidPathError: 40002,
    CrossRootError: 40003,
    PathNotFoundError: 40410,
    FMFileNotFound: 40411,
    NameConflictError: 40901,
    DirectoryNotEmptyError: 40903,
    UserChoiceRequired: 40902,
    ReadOnlyError: 40301,
    SHA256Error: 42204,
}


def _http_status(exc: Exception) -> int:
    if isinstance(exc, (RequestKeyNotFound, PathNotFoundError, FMFileNotFound)):
        return 404
    if isinstance(exc, (InputDataError, RequestContentIncorrect,
                        InvalidPathError, CrossRootError)):
        return 400
    if isinstance(exc, (NameConflictError, DirectoryNotEmptyError,
                        UserChoiceRequired)):
        return 409
    if isinstance(exc, ReadOnlyError):
        return 403
    if isinstance(exc, SHA256Error):
        return 422
    if isinstance(exc, MetadataParserError):
        return 422
    if isinstance(exc, (ConfigError, DatabaseError)):
        return 500
    return 500


def _error_code(exc: Exception) -> int:
    if isinstance(exc, APPBaseError):
        return exc.error_code
    code = _FM_CODES.get(type(exc))
    if code is not None:
        return code
    if isinstance(exc, MetadataParserError):
        return 42200
    return 50001


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(APPBaseError)
    async def _app_error_handler(_request: Request, exc: APPBaseError):
        if _http_status(exc) >= 500:
            log.error(f"API error: [{exc.error_code}] {exc.message}")
        return JSONResponse(
            status_code=_http_status(exc),
            content={"error": {"code": exc.error_code, "message": exc.message}},
        )

    @app.exception_handler(FileManagerError)
    async def _fm_error_handler(_request: Request, exc: FileManagerError):
        payload: dict = {"code": _error_code(exc), "message": str(exc)}
        if isinstance(exc, UserChoiceRequired):
            payload["options"] = list(getattr(exc, "options", []))
            payload["logical_path"] = getattr(exc, "logical_path", "")
        log.warning(f"API file error: {exc}")
        return JSONResponse(status_code=_http_status(exc),
                            content={"error": payload})

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(_request: Request,
                                        exc: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(p) for p in err.get('loc', []))}: {err.get('msg')}"
            for err in exc.errors()[:5])
        return JSONResponse(
            status_code=400,
            content={"error": {"code": 40000,
                               "message": f"Invalid request: {details}"}},
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(_request: Request, exc: Exception):
        log.error(f"Unhandled API exception: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": 50000,
                               "message": "Internal server error"}},
        )
