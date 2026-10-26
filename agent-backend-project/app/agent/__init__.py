"""Agent 层对外导出。"""

from __future__ import annotations

from typing import Any

from .llm import get_chat_model


def build_stream_chain_context(*args: Any, **kwargs: Any) -> Any:
    """懒加载流式对话上下文构建函数，避免导入 Agent 包时加载重依赖。"""
    from .prompt_layer import build_stream_chain_context as _build_stream_chain_context

    return _build_stream_chain_context(*args, **kwargs)


__all__ = [
    "get_chat_model",
    "build_stream_chain_context",
]
