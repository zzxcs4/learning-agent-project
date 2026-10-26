"""LangGraph checkpoint 公共封装。

**持久化取舍（升级规划 §八 第 3 期第 5 条，已明确）**：`MemorySaver` 只活在进程内，
重启即丢；跨请求的状态恢复**以 `workflow_runs.state_json` 为权威**
（`workflow_run_service` 负责创建/更新，`learning_workflow_service` 从中恢复）。
checkpointer 只服务"单次进程内运行"的图执行断点，**不引入持久化 checkpointer 后端**——
这是规划里"或明确以 state_json 为权威"那个分支的落选答案备案。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.checkpoint.memory import MemorySaver


@lru_cache(maxsize=1)
def get_workflow_checkpointer() -> MemorySaver:
    """返回进程内共享的工作流 checkpointer。"""
    return MemorySaver()


def build_thread_config(thread_id: str) -> dict[str, dict[str, Any]]:
    """构造 LangGraph 恢复同一流程所需的 thread 配置。"""
    return {"configurable": {"thread_id": thread_id}}
