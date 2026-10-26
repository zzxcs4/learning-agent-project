"""
- 本文件注册 FastAPI 全局异常处理器。
- 配合 `AppException` 与 `raise_error`，统一错误返回。
"""

from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.core.exceptions import AppException
from app.core.response_models import ErrorResponse
from app.core.logging_config import main_logger # 导入日志器

VALIDATION_MESSAGE_MAP = {
    "missing": "该字段为必填项",
    "string_too_short": "长度太短",
    "string_too_long": "长度太长",
    "int_parsing": "必须是整数",
    "greater_than": "必须大于限定值",
    "greater_than_equal": "必须大于等于限定值",
}


def _build_detail(request: Request, detail: Any) -> dict[str, Any]:
    # 将基础请求信息注入 detail，便于前端快速定位出错接口。
    base_detail: dict[str, Any] = {
        "path": request.url.path,
        "method": request.method,
    }
    if detail is None:
        return base_detail
    if isinstance(detail, dict):
        merged = detail.copy()
        merged.update(base_detail)
        return merged
    base_detail["reason"] = detail
    return base_detail


def _json_error_response(*, code: int, message: str, detail: Any) -> JSONResponse:
    # 统一错误响应出口，避免各处理器重复构造 JSONResponse 逻辑。
    payload = ErrorResponse(code=code, message=message, detail=detail)
    return JSONResponse(status_code=code, content=payload.model_dump())


def _build_validation_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    # 将 FastAPI/Pydantic 校验错误转换为前端友好的中文结构。
    errors: list[dict[str, Any]] = []
    for error in exc.errors():
        error_type = str(error.get("type", "validation_error"))
        location = [str(item) for item in error.get("loc", []) if item != "body"]
        errors.append(
            {
                "field": ".".join(location) if location else "unknown",
                "message": VALIDATION_MESSAGE_MAP.get(error_type, "参数格式不正确"),
                "type": error_type,
            }
        )
    return errors


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器。"""

    @app.exception_handler(AppException)
    async def handle_app_exception(request: Request, exc: AppException) -> JSONResponse:
        # 统一处理业务层通过 raise_error 抛出的异常。
        main_logger.error("捕获到业务异常: 错误信息: {}, 详情: {}", exc.message, exc.detail, exc_info=True) # 记录异常
        detail = _build_detail(request, exc.detail)
        return _json_error_response(
            code=exc.code,
            message=exc.message,
            detail=detail,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_exception(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 处理请求参数校验失败：统一错误码并输出中文字段错误信息。
        main_logger.error("捕获到请求参数校验异常: 错误详情: {}", exc.errors(), exc_info=True) # 记录异常
        detail = _build_detail(request, {"errors": _build_validation_errors(exc)})
        return _json_error_response(
            code=422,
            message="请求参数校验失败",
            detail=detail,
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        # 兜底处理 FastAPI 原生 HTTPException，保证响应结构与业务异常一致。
        main_logger.error("捕获到HTTP异常: 错误详情: {}", exc.detail, exc_info=True) # 记录异常
        detail = _build_detail(request, exc.detail)
        return _json_error_response(
            code=exc.status_code,
            message=str(exc.detail) if exc.detail else "请求处理失败",
            detail=detail,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        # 最终兜底：处理未捕获异常，避免向前端暴露内部错误细节。
        main_logger.critical("捕获到未预期的异常: {}", exc, exc_info=True) # 记录严重异常
        detail = _build_detail(request, {"exception_type": exc.__class__.__name__})
        return _json_error_response(
            code=500,
            message="系统开小差了，请稍后再试",
            detail=detail,
        )
