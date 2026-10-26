"""可观测（升级规划 §八 第 3 期）：agent loop 的 token 用量统计与轨迹存储。

规划原文的三件事都在这里落地：

1. **token 用量**：`UsageCallbackHandler` 挂在每一步模型流上，`on_llm_end` 里取聚合
   usage_metadata。流式下 on_llm_end 带全量 token 数**已实测**（前提
   `stream_usage=True`，见 `app/agent/llm.py`；实测 relay 返回
   input/output/total + reasoning 明细）；模型或中转不回传 usage 时 handler 保持
   None，统计显示为空值，loop 行为不变。
2. **轨迹存储**：`persist_loop_trace` 把一次 loop 的完整轨迹写进 `workflow_runs`
   （workflow_type="agent_loop"）。按规划「先写 `workflow_runs.state_json`，不急着
   建表」——轨迹从"只活在 `chat_messages.response_payload`"变成**可按 run_id 查询**
   的独立记录，展示职责仍归 response_payload，两者形状一致（同一份 trace dict）。
3. **成本口径**：本片只统计 supervisor loop 自身的模型调用；worker 委派（出题/评测
   等）的模型调用发生在 `WorkerGateway` 内部、各自创建 LLM 实例，不经过本 handler。
   要把 worker 用量也纳入，需把回调贯穿网关——等有真实统计诉求再做，不预建。

不引入单价/计费：usage 的 token 数是事实，"多少钱"取决于随时会变的价目表，
把它写死在代码里只会产生一个看似精确的假数字。
"""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.services.workflow_run_service import WorkflowRunService

# workflow_runs.workflow_type 是 String(32)："agent_loop" 与既有 "learning_quiz" 并列。
LOOP_WORKFLOW_TYPE = "agent_loop"


def extract_usage(response: Any) -> dict[str, int] | None:
    """从 `on_llm_end` 的 LLMResult 里取出聚合 usage_metadata；取不到返回 None（不是 0）。

    为什么返回 None 而不是 0：**"模型没报用量"和"用量真的是 0"是两回事**，
    统计口径必须能区分这两种，否则不回传 usage 的中转会把均值拉成虚假的 0。
    """
    try:
        message = response.generations[0][0].message
    except (AttributeError, IndexError, TypeError):
        return None
    usage = getattr(message, "usage_metadata", None)
    return dict(usage) if isinstance(usage, dict) else None


class UsageCallbackHandler(BaseCallbackHandler):
    """一次模型调用的用量收集器（`on_llm_end` 恰好一次 = 一次完整流）。

    提取阶段的任何异常都在这里吞掉：handler 跑在模型流内部，用量统计失败
    绝不能把一次正常的模型调用拖成报错（与 `ToolRuntime` / MCP 客户端同一契约）。
    """

    def __init__(self) -> None:
        self.usage: dict[str, int] | None = None

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        try:
            self.usage = extract_usage(response)
        except Exception:  # noqa: BLE001 - 用量统计绝不能打断模型流
            self.usage = None


class UsageTracker:
    """一次 run 的分步用量账本。

    每步 `new_handler()` → 流结束 `record_step()`：分步记录与 `LoopResult.steps`
    按 step 编号对齐（前端时间线可以直接在每一步旁边标 token 数），汇总进
    `LoopResult.usage`。某步拿不到 usage（假模型/中转不回传）时该步记录 None，
    **汇总只累计已知值**——不会把"不知道"当成 0 混进均值。
    """

    def __init__(self) -> None:
        self._steps: list[dict[str, Any]] = []
        self._input = 0
        self._output = 0
        self._total = 0

    def new_handler(self) -> UsageCallbackHandler:
        return UsageCallbackHandler()

    def record_step(self, step_index: int, handler: UsageCallbackHandler) -> None:
        usage = handler.usage
        row: dict[str, Any] = {"step": step_index}
        if usage is None:
            row.update({"input_tokens": None, "output_tokens": None, "total_tokens": None})
        else:
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (input_tokens + output_tokens))
            row.update(
                {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens,
                }
            )
            self._input += input_tokens
            self._output += output_tokens
            self._total += total_tokens
        self._steps.append(row)

    def step_tokens(self, step_index: int) -> dict[str, int | None]:
        """某一步的 token 字段（供拼进 `LoopResult.steps` 的同编号记录）。"""
        for row in self._steps:
            if row["step"] == step_index:
                return {
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "total_tokens": row["total_tokens"],
                }
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    def summary(self) -> dict[str, Any]:
        return {
            "steps": list(self._steps),
            "input_tokens": self._input,
            "output_tokens": self._output,
            "total_tokens": self._total,
        }


def persist_loop_trace(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    message_id: int | None,
    request_text: str,
    result: Any,
    usage: dict[str, Any] | None = None,
    error_message: str = "",
) -> str | None:
    """把一次 agent loop 的轨迹写进 `workflow_runs.state_json`，返回 run_id。

    `result` 是 `LoopResult`（鸭子类型：只用 `as_dict()`），避免 services 反向
    import agent 层形成循环。**任何失败都只记日志并返回 None**：调用点在 SSE 的
    finally 里，此时回答本身已经落库完成，轨迹存储绝不能把一次成功的回答拖成报错。
    """
    payload = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    state: dict[str, Any] = {
        "message_id": message_id,
        "request_preview": (request_text or "")[:200],
        "stop_reason": payload.get("stop_reason"),
        "trace": {
            "steps": payload.get("steps"),
            "tool_calls": payload.get("tool_calls"),
        },
        "usage": usage or {},
    }
    try:
        run = WorkflowRunService.create_run(
            db,
            user_id,
            session_id,
            workflow_type=LOOP_WORKFLOW_TYPE,
            status="failed" if error_message else "completed",
            state=state,
        )
        return run.run_id
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        main_logger.warning("agent loop 轨迹落库失败（不影响本次回答）：{}", exc)
        return None
