"""
错误处理文件说明：
- 本文件定义统一错误响应模型。
- 重点类：
  - `ErrorResponse`：规范错误响应结构为 `code/message/detail`。
  - `StandardResponse`：通用响应结构为 `code/message/data`。
"""

from typing import Any, Optional

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """统一错误响应结构。"""

    code: int
    message: str
    detail: Any = None


class StandardResponse(BaseModel):
    """通用标准响应结构。"""

    code: int
    message: str
    data: Optional[Any] = None


# 通用错误响应定义，便于各路由统一引用。
common_error_responses = {
    400: {"model": ErrorResponse, "description": "请求参数错误"},
    401: {"model": ErrorResponse, "description": "未授权"},
    404: {"model": ErrorResponse, "description": "资源不存在"},
    409: {"model": ErrorResponse, "description": "资源冲突"},
    422: {"model": ErrorResponse, "description": "请求参数校验失败"},
    429: {"model": ErrorResponse, "description": "请求过于频繁"},
    500: {"model": ErrorResponse, "description": "系统异常"},
}
