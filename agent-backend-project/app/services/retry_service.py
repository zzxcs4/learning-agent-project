"""轻量自动重试服务。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """自动重试策略。"""

    max_attempts: int = 2


class RetryExecutor:
    """统一处理当前请求内的自动重试。"""

    @staticmethod
    def run(
        *,
        state: dict[str, Any],
        node_name: str,
        operation: Callable[[], T],
        retry_operation: Callable[[], T] | None = None,
        policy: RetryPolicy | None = None,
    ) -> T:
        """执行操作；失败后按策略自动重试。"""
        retry_policy = policy or RetryPolicy()
        max_attempts = max(1, retry_policy.max_attempts)
        current_operation = operation

        for attempt in range(max_attempts):
            try:
                result = current_operation()
                if isinstance(result, dict):
                    RetryExecutor._merge_retry_state(result, state)
                return result
            except Exception:
                if attempt >= max_attempts - 1:
                    raise
                state["auto_retry_count"] = int(state.get("auto_retry_count") or 0) + 1
                state["last_auto_retry_node"] = node_name
                current_operation = retry_operation or operation

        return current_operation()

    @staticmethod
    def _merge_retry_state(result: dict[str, Any], state: dict[str, Any]) -> None:
        """把自动重试元信息带回业务状态。"""
        for key in ("auto_retry_count", "last_auto_retry_node"):
            if key in state:
                result[key] = state[key]
