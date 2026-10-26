"""工具运行上下文：把 Agent loop 的运行期依赖（db / 用户 / 会话）与工具本身解耦。

为什么不让模型直连数据库（升级规划 §七「Memory」条）：
1. **权限边界**——每个工具显式声明它要读/写什么，`user_id` 由运行期注入而不是模型填写，
   模型无法通过伪造参数越权读取别人的资料；
2. **可审计**——注入点只有一处，配合 `ToolRuntime` 的 trace 即可回放"谁在什么上下文里调了什么"；
3. **可测试**——离线验收脚本可以注入一个假的 `ToolExecutor`，无需连 Qdrant 与 LLM。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from langchain_core.tools import InjectedToolArg
from sqlalchemy.orm import Session

# 所有工具共用的隐藏参数注解。
#
# `InjectedToolArg` 的效果是**双向**的：既让"运行期必须由我们注入"这件事显式化，
# 又让该参数**不出现在暴露给模型的 JSON Schema 里**（实测 `tool.args` 与
# `convert_to_openai_tool()` 都不含它）。所以模型只能决定"调哪个工具、传什么业务参数"，
# 决定不了 `user_id` 是谁。
InjectedToolContext = Annotated[dict[str, Any], InjectedToolArg]


@dataclass
class ToolContext:
    """一次 Agent loop 运行期间所有工具共享的上下文。"""

    db: Session
    user_id: int
    session_id: int
    knowledge_base_id: int | None = None
    # 只为 trace 展示保留：这次 loop 的 run_id（没有工作流时为 None）
    run_id: str | None = None

    def as_injection(self) -> dict[str, object]:
        """转成注入工具调用的 dict（对应 `InjectedToolArg` 的那个参数）。"""
        return {
            "db": self.db,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "knowledge_base_id": self.knowledge_base_id,
            "run_id": self.run_id,
        }
