"""Agent 原子能力工具包（tool，不是 worker）。

划分标准（升级规划 §7.1）：**有角色人格的保留为 Agent，没有角色的原子能力才做成 tool。**

| 在这里（tool：函数、无提示词、无状态） | 在 `app/agent/workers.py`（worker：有提示词、有角色身份） |
|---|---|
| `search_material` 搜知识库 | 出题 |
| `get_mastery` 查掌握度 | 评测 |
| `get_weak_points` 查薄弱点 | 难点讲解 |
| `list_practice_history` 查错题/练习 | 笔记归档 |
| `save_note` 存笔记（唯一写权限工具） | |

`DEFAULT_TOOLS` 的名字即**白名单**：`ToolRuntime` 只执行出现在这里的工具。
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.agent.tools.knowledge import search_material
from app.agent.tools.knowledge_base import (
    create_knowledge_base,
    get_document_summary,
    list_documents,
    list_knowledge_bases,
)
from app.agent.tools.note import save_note
from app.agent.tools.practice import list_practice_history
from app.agent.tools.profile import get_mastery, get_weak_points
from app.agent.tools.runtime import ToolRuntime, ToolTraceStep
from app.agent.tools.tool_context import InjectedToolContext, ToolContext

# 注册顺序 = 暴露给模型的顺序（把"先看画像再取资料"的直觉顺序固化下来，便于可读的 trace）
_ATOMIC_TOOLS: tuple[BaseTool, ...] = (
    get_mastery,
    get_weak_points,
    list_practice_history,
    search_material,
    # 知识库现状（第 23 轮）：出题参数改成对话里问之后，模型要能报出库名/库里的资料
    list_knowledge_bases,
    list_documents,
    get_document_summary,
    save_note,
    create_knowledge_base,
)

TOOL_REGISTRY: dict[str, BaseTool] = {tool.name: tool for tool in _ATOMIC_TOOLS}
DEFAULT_TOOL_NAMES: frozenset[str] = frozenset(TOOL_REGISTRY)
READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "search_material",
        "get_mastery",
        "get_weak_points",
        "list_practice_history",
        "list_knowledge_bases",
        "list_documents",
        "get_document_summary",
    }
)

__all__ = [
    "DEFAULT_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "TOOL_REGISTRY",
    "InjectedToolContext",
    "ToolContext",
    "ToolRuntime",
    "ToolTraceStep",
    "create_knowledge_base",
    "get_document_summary",
    "get_mastery",
    "get_weak_points",
    "list_documents",
    "list_knowledge_bases",
    "list_practice_history",
    "save_note",
    "search_material",
]
