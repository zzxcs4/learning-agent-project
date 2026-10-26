"""工具运行期：白名单校验 + 上下文注入 + 计时 + trace。

loop 里**只有这一条路径**能执行工具，因此"模型能不能越权"这个问题有唯一答案：
- 名字必须在白名单里（模型幻觉出的工具名不会被执行）；
- 上下文由 `ToolContext` 注入，模型参数里没有 `user_id`；
- 任何异常都被收敛成"给模型看的可读 JSON 结果"，而不是把 loop 打断。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from app.agent.tools.tool_context import ToolContext

# 单条工具结果回灌给模型时的字符上限，防止一次检索把上下文挤爆
MAX_OBSERVATION_CHARS = 6000


@dataclass
class ToolTraceStep:
    """一次工具调用的可观测记录（升级规划 §7.2「Streaming / 可观测」）。"""

    name: str
    args: dict[str, Any]
    elapsed_ms: int
    ok: bool
    result_preview: str

    def as_event(self) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "name": self.name,
            "ok": self.ok,
            "elapsed_ms": self.elapsed_ms,
            "result_preview": self.result_preview,
        }


@dataclass
class ToolRuntime:
    """按白名单执行原子能力工具。"""

    context: ToolContext
    allowed_tools: frozenset[str]
    tools: dict[str, BaseTool]
    traces: list[ToolTraceStep] = field(default_factory=list)

    def available(self) -> list[BaseTool]:
        """暴露给模型的工具列表（保持注册顺序）。"""
        return [self.tools[name] for name in self.tools if name in self.allowed_tools]

    def handles(self, name: str) -> bool:
        """这个工具名是否由本 runtime 执行（白名单与注册表都命中）。

        为什么要有这个方法：loop 里必须区分"工具"与"worker"——**白名单之外**的名字
        才是委派给 worker 的。接 MCP 之后白名单会动态变大（`mcp_*`），
        若仍拿"内置工具名单"当判据，MCP 工具就会被误当成 worker 名去委派。
        所以判据统一收敛到"runtime 是否处理得了这个名字"。
        """
        return name in self.allowed_tools and name in self.tools

    def execute(self, name: str, args: dict[str, Any] | None = None) -> str:
        """执行一次工具调用，永远返回字符串（绝不抛异常打断 loop）。"""
        call_args = dict(args or {})

        if name not in self.allowed_tools:
            return json.dumps(
                {"error": f"工具 {name} 不在白名单内，未执行"},
                ensure_ascii=False,
            )

        tool = self.tools.get(name)
        if tool is None:
            return json.dumps({"error": f"工具 {name} 不存在"}, ensure_ascii=False)

        started = time.perf_counter()
        try:
            # 关键一行：把运行期上下文注入隐藏参数。模型提供的参数原样透传，
            # 但 user_id / db 这些它根本给不出来（不在 JSON Schema 里）。
            result = tool.invoke({**call_args, "ctx": self.context.as_injection()})
            text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
            ok = True
        except Exception as exc:  # 工具的失败是"观察结果"，不是 loop 的致命错误
            text = json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)
            ok = False

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        self.traces.append(
            ToolTraceStep(
                name=name,
                args=call_args,
                elapsed_ms=elapsed_ms,
                ok=ok,
                result_preview=text[:300],
            )
        )
        return text[:MAX_OBSERVATION_CHARS]
