"""
错误处理文件说明：
- 一个通用异常类与一个统一抛错函数。
"""

from typing import Any


class AppException(Exception):
    """统一应用异常，承载标准错误响应所需字段。"""

    def __init__(self, *, code: int, message: str, detail: Any = None) -> None:
        # code 直接使用 HTTP 状态码，全局处理器会据此返回响应。
        self.code = code
        self.message = message
        self.detail = detail
        super().__init__(message)


def raise_error(
    *,
    code: int,
    message: str,
    detail: Any = None,
) -> None:
    """业务层统一抛错入口：code 直接使用 HTTP 状态码，文案由外部显式传入。"""
    if code < 100 or code > 599:
        raise ValueError("code 必须是合法的 HTTP 状态码(100-599)")
    raise AppException(code=code, message=message, detail=detail)
