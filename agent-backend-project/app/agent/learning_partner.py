"""学习搭档（supervisor）：手写 ReAct loop，把"谁来决定"从前端下拉框收回系统内部。

**架构原则（升级规划 §七，贯穿全局）**：自主性只上移到编排层，业务层保持确定性。

- 本文件 = 编排层：决定"要不要做、做哪些、按什么顺序、要不要重来"，是新增的自主性；
- `workers.py` 委派的 4 个 worker = 业务层：出题、评分、讲解、归档仍走既有 LangGraph
  与单轮 JSON，**自主性为零**（评分不会自由发挥）。

为什么手写 loop 而不用 `create_react_agent`：loop 不依赖 `prebuilt`，可规避规划里那条
"LangGraph prebuilt API 签名未核对"的风险；本项目需要把自己的 `ToolRuntime`（白名单 +
上下文注入 + trace）接进去，手写比适配 prebuilt 更短也更可控。

loop 形状（`max_steps=8`）：
```
build messages → [stream model step] → 有 tool_calls？
   ├─ 否 → 这一轮的文字就是最终答案，结束（简单请求只需要 1 次模型调用）
   └─ 是 → 执行工具/worker → 观察结果回灌 → 下一步
```
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from sqlalchemy.orm import Session

from app.agent.llm import REASONING_FIELD
from app.agent.mcp import describe_mcp_tools_for_prompt, mcp_langchain_tools
from app.agent.prompt_layer import build_personalized_prompt, build_stream_chain_context
from app.agent.tools import DEFAULT_TOOL_NAMES, TOOL_REGISTRY, ToolContext, ToolRuntime
from app.agent.workers import (
    SUPERVISOR_AGENT,
    WorkerGateway,
    describe_workers_for_prompt,
    register_worker_handlers,
)
from app.core.logging_config import main_logger
from app.db.models import ChatMessage
from app.services.memory_service import MemoryService
from app.services.quiz_params_service import normalize_knowledge_base_id
from app.services.session_service import SessionService
from app.services.trace_service import UsageTracker, persist_loop_trace

# 手写 loop 的步数上限：一次模型调用 = 一步
MAX_STEPS = 8

STEP_THINK = "think"
STEP_TOOL = "tool"
STEP_FINAL = "final"

_LOOP_SYSTEM_SUFFIX = """你是用户的学习搭档（supervisor），可以调用工具查资料，并把具体任务委派给专职 worker。

【最重要的一条：工具与 worker 的分工不能混】
- **原子工具（search_material / get_mastery / get_weak_points / list_practice_history）只能"查"**。
  它们回答"是什么情况"，本身不产生任何交付物。
- **需要"做"出东西时必须委派 worker，不要自己拿工具凑**：
  · 要出练习题 → `delegate_quiz`
  · 要评分 / 批改一次练习 → `delegate_evaluate`
  · 要深入讲解某个知识点或错题 → `delegate_explain`
  · 要把一次练习的结果归档成笔记 → `delegate_archive`
- `save_note` **只用于"把用户明确给出的内容记下来"**，它不是归档通道。
  **绝不要**用 `save_note` 代替 `delegate_quiz` / `delegate_archive`：那样没有 `run_id`，
  用户拿不到可提交的练习题，归档也没有来源追踪——这是本系统最容易被做错的一件事。
- **归档必须带上 run_id，让用户能确认是哪一场**：`list_practice_history` 每场都带
  `run_id`。用户说"归档这次/那场/某次练习"时，
  **先调 `list_practice_history`**（必要时按题目/得分/时间定位是哪一场，或直接拿最新一场的
  `run_id`），再把那个 `run_id` 传给 `delegate_archive`。不要臆造 run_id，也不要
  不问清楚就把"最近一次"之外的一场当成用户说的那场。
- 判据一句话：**用户要"结果"就委派 worker；用户只是"问情况"才用工具。**

其他要求：
1. 先判断用户到底要什么。**简单问答直接回答**，不要为了走流程而调用工具。
2. 需要事实依据时先调 search_material 检索用户的知识库；需要知道用户水平时调 get_mastery / get_weak_points / list_practice_history。
3. 工具和 worker 的返回是**观察结果**，不是给用户看的最终回答。你要据此继续推理，最后用自然语言把结果讲清楚。
4. 不要臆造工具没有返回的数据（例如掌握度分数、题目内容）。
5. 委派 worker 后，把返回的 `run_id` 明确告诉用户，并说明"做完后可以直接提交答案"。
6. **同一轮里不要反复用同一个工具搜同一件事**：一次检索没结果就换个说法或直接说明查不到，
   不要连搜三四遍把步数耗光。
"""


def _mounted_knowledge_base_note(
    db: Session, user_id: int, knowledge_base_id: int | None
) -> str:
    """「本会话挂载了哪个知识库」的提示词片段；没挂载时返回空串（不占预算）。

    只在拿到有效 id 时生成；库里查不到（已删）也不报错——委派参数里那个 id 会被
    知识库归属校验挡成 404，这里只负责让模型**不要重复问用户**。
    """
    mounted_id = normalize_knowledge_base_id(knowledge_base_id)
    if mounted_id is None:
        return ""
    from sqlalchemy import select

    from app.db.models import KnowledgeBase

    row = db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == mounted_id, KnowledgeBase.user_id == user_id
        )
    )
    name = str(getattr(row, "name", "") or "")
    label = f"「{name}」" if name else f"id={mounted_id}"
    return (
        f"【本会话已挂载知识库】用户已在输入区挂载 {label}"
        f"（knowledge_base_id={mounted_id}），检索范围已限定在它之内。\n"
        "- 出题时**不要再问用户选哪个知识库**：委派 `delegate_quiz` 时直接省略 "
        "knowledge_base_id（系统会自动沿用本会话挂载的库），或显式传同一个 id。\n"
        "- 用户当轮明确说要换一个库、或不用知识库时，按用户的要求做。"
    )


@dataclass
class LoopResult:
    """一次 loop 的完整结果，供 SSE / 非流式调用与验收脚本共用。"""

    answer: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 第 3 期可观测：分步 + 汇总的 token 用量（`UsageTracker.summary()` 的产物）。
    # 模型/中转不回传 usage 时各步为 None 值、汇总可能为 0——"拿不到"与"真的是 0"
    # 的区分见 trace_service.extract_usage。
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str = "completed"
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": self.usage,
            "stop_reason": self.stop_reason,
            "run_id": self.run_id,
        }


class LearningPartnerService:
    """supervisor：门控命中"多步 / 跨流程意图"后由 `OrchestratorService` 调用。"""

    def __init__(
        self,
        db: Session,
        *,
        llm: Any = None,
        provider: str = "deepseek",
        worker_gateway: Any = None,
        tool_names: frozenset[str] | None = None,
        session_model: int | None = None,
        mcp_tools: dict[str, Any] | None = None,
    ):
        self.db = db
        self.provider = provider
        # 允许注入假模型/假网关，离线验收脚本因此不需要真调 LLM
        self._llm = llm
        self._gateway = worker_gateway
        self.tool_names = tool_names or DEFAULT_TOOL_NAMES
        # 会话模式（0 学习 / 2 笔记）：委派给出题 worker 时要带上，
        # 否则它在笔记会话里只能报一个含糊的 404。
        self._session_model = session_model
        # 本会话挂载的知识库（第 24 轮）：用户在输入区挂上的库要**跟着整轮走**——
        # 它既限定 `search_material` 的范围，也是出题委派的默认归属（见 `_delegate_worker`）。
        self._knowledge_base_id: int | None = None
        # 本轮用户**原话**（第 25 轮）：委派出题时用它当"用户要求"，不让模型自己改写扩写。
        # 模型改写的那段会以 `model_brief` 记进委派载荷（trace 里看得到），但不进提示词——
        # 实测它会压过知识库考纲，把题出成"模型自己编的知识体系"（用户 2026-10-20 报的问题）。
        self._request_text: str = ""
        # 外部 MCP 工具（第 3 期）：None = 按配置自动加载；{} = 显式不挂载（验收脚本用）
        self._mcp_tools = mcp_tools

    # ------------------------------------------------------------------ 对外入口
    def validate_session(self, user_id: int, session_id: int, session_model: int) -> None:
        """校验会话归属；不通过时抛 404。

        公开且**必须在建流之前调用**：调用方（`OrchestratorService._start_loop`）要先拿到
        404，再决定是否返回 `StreamingResponse`。放在生成器里会让异常在响应头之后才抛出。
        """
        SessionService.get_session_or_404(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            session_model=session_model,
        )
        self._session_model = session_model

    def run_events(
        self,
        *,
        user_id: int,
        session_id: int,
        request_text: str,
        knowledge_base_id: int | None = None,
        run_id: str | None = None,
        history_before_message_id: int | None = None,
        load_history: bool = True,
        session_model: int | None = None,
    ) -> Generator[dict[str, Any], None, LoopResult]:
        """执行 loop 并逐条产出事件；`return` 值（Generator 的返回值）是 `LoopResult`。

        `history_before_message_id`：历史记忆的截断点，默认取本会话当前最大 message id
        （即"这一轮之前"的历史）。`load_history=False` 时完全不读记忆，供离线验收使用。
        """
        register_worker_handlers()
        self._session_model = session_model
        # `0` = 前端"明确不使用知识库"的哨兵（第 24 轮）：检索范围只认有效 id，
        # 否则会退化成 `WHERE knowledge_base_id = 0`（一条资料都搜不到）。
        mounted_knowledge_base_id = normalize_knowledge_base_id(knowledge_base_id)
        self._knowledge_base_id = mounted_knowledge_base_id
        self._request_text = str(request_text or "").strip()

        ctx = ToolContext(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            knowledge_base_id=mounted_knowledge_base_id,
            run_id=run_id,
        )
        # 外部 MCP 工具与内置原子工具**放在同一个 runtime 里**：白名单、计时、trace
        # 都走同一条路径，所以 MCP 调用在前端轨迹里的形状与内置工具完全一致。
        mcp_tools = mcp_langchain_tools() if self._mcp_tools is None else dict(self._mcp_tools)
        runtime = ToolRuntime(
            context=ctx,
            allowed_tools=frozenset(self.tool_names) | frozenset(mcp_tools),
            tools={**TOOL_REGISTRY, **mcp_tools},
        )
        gateway = self._gateway or WorkerGateway(
            self.db, provider=self.provider, llm=self._llm
        )
        llm = self._llm

        messages = self._build_messages(
            user_id=user_id,
            session_id=session_id,
            request_text=request_text,
            knowledge_base_id=knowledge_base_id,
            history_before_message_id=history_before_message_id,
            load_history=load_history,
            mcp_tools=mcp_tools,
        )

        bound = self._bind_tools(llm, runtime)
        result = LoopResult()
        answer_parts: list[str] = []
        # 第 3 期可观测：每步模型调用挂一个用量收集器，分步记录与 result.steps 对齐
        usage = UsageTracker()

        for step_index in range(1, MAX_STEPS + 1):
            yield {"type": "step", "index": step_index, "kind": STEP_THINK, "text": ""}
            started = time.perf_counter()
            step_handler = usage.new_handler()
            step_stream = self._stream_model_step(bound, messages, step_handler)
            # 手动 next() 而不是 `for delta in step_stream`：既要**边收边发**（yield 点在
            # 模型流内部，见 `_stream_model_step`），又要拿到生成器的返回值
            # （`StopIteration.value` = 累积后的完整消息）才能解析 tool_calls。
            #
            # 副作用是事件顺序变了：这一步的文字现在**先于**它的 tool_call 事件到达前端。
            # 这是有意的，也更诚实——`answer_parts` 本来就把每一步的文字都拼进最终答案，
            # 那段文字迟早要给用户看；旧写法只是把它压到整步结束才一起放出来。
            while True:
                try:
                    kind, piece = next(step_stream)
                except StopIteration as stop:
                    final_message = stop.value
                    break
                # 思考内容单独成事件、**不进 answer_parts**：它不是答案的一部分，
                # 只是"模型在想什么"的进度反馈（思考型模型一段推理能有几千字，
                # 混进答案会直接污染落库的 response_text）。
                if kind == "reasoning":
                    yield {"type": "reasoning", "text": piece}
                    continue
                answer_parts.append(piece)
                yield {"type": "delta", "text": piece}
            elapsed_ms = int((time.perf_counter() - started) * 1000)

            tool_calls = self._extract_tool_calls(final_message)
            usage.record_step(step_index, step_handler)
            result.steps.append(
                {
                    "index": step_index,
                    "kind": STEP_TOOL if tool_calls else STEP_FINAL,
                    "elapsed_ms": elapsed_ms,
                    "tool_calls": [self._call_name(call) for call in tool_calls],
                    "text_preview": self._message_text(final_message)[:200],
                    **usage.step_tokens(step_index),
                }
            )

            if not tool_calls:
                result.stop_reason = "final_answer"
                break

            messages.append(self._as_assistant_message(final_message, tool_calls))
            for call in tool_calls:
                name = self._call_name(call)
                args = self._call_args(call)
                yield {"type": "tool_call", "name": name, "args": args, "step": step_index}

                if runtime.handles(name):
                    observation = runtime.execute(name, args)
                    record = runtime.traces[-1]
                    yield record.as_event()
                    outcome = {
                        "ok": record.ok,
                        "elapsed_ms": record.elapsed_ms,
                        "result_preview": record.result_preview,
                    }
                else:
                    # 委派也计时：之前这里写死 None，前端"委派明细"里只有工具会显示耗时，
                    # 而委派恰恰是最慢的一步，缺它最误导。
                    started = time.perf_counter()
                    observation, delegated = self._delegate_worker(
                        gateway, name, args, user_id=user_id, session_id=session_id
                    )
                    outcome = {
                        # 成败取委派结果里的 ok：网关契约是"永不抛异常，失败也是结果"
                        # （`{worker, ok, status, reason, data}`），异常路径也补了 ok=False。
                        # 原来靠观察文本前缀 `{"worker_error"` 猜，结果**网关自己判失败**时
                        # （ok=False 但不是异常）事件仍报 ok=True，前端状态与轨迹都把失败当成功。
                        "ok": bool(delegated.get("ok")),
                        "elapsed_ms": int((time.perf_counter() - started) * 1000),
                        "result_preview": observation[:300],
                    }
                    event: dict[str, Any] = {"type": "tool_result", "name": name, **outcome}
                    # 委派 worker 额外回推 run_id：出题与知识卡这两种消息都是 worker
                    # **自己落库**的（`chat_messages.request_id = "{run_id}:quiz"` /
                    # `"{run_id}:difficulty"`），前端要靠它认领"这一轮新出的卡"。
                    # SSE 路径按设计不回刷消息列表，不给这个键的话 worker 写的那条消息
                    # 在界面上根本不存在（用户只看到一句"讲好了"，看不到卡）。
                    claimed_run_id = self._delegated_run_id(name, delegated)
                    if claimed_run_id:
                        event["run_id"] = claimed_run_id
                    yield event

                # 结果一并写进轨迹（而不只是发给流）：
                # 刷新页面后前端只有落库的轨迹，若这里不记，"工具失败"在回放里就与成功一模一样。
                result.tool_calls.append({"name": name, "args": args, **outcome})
                messages.append(
                    ToolMessage(content=observation, tool_call_id=self._call_id(call))
                )
        else:
            # for-else：8 步都没收敛，明确告知而不是假装完成
            result.stop_reason = "max_steps"
            notice = (
                f"\n\n（已连续执行 {MAX_STEPS} 步仍未收敛，先给出当前结论；"
                "如果还缺信息，请把问题拆得更具体一些。）"
            )
            answer_parts.append(notice)
            yield {"type": "delta", "text": notice}

        result.usage = usage.summary()
        result.answer = "".join(answer_parts).strip()
        return result

    def stream(
        self,
        *,
        user_id: int,
        session_id: int,
        session_model: int,
        request_text: str,
        select_model: int = 0,
        knowledge_base_id: int | None = None,
    ) -> Generator[str, None, None]:
        """把 loop 事件编成 SSE 帧并负责落库，形状与单轮聊天一致（前端可零改动消费）。"""
        from app.services.stream_chat_service import _sse_data

        # 会话校验必须在**构造 StreamingResponse 之前**完成。若放在生成器内部，
        # 404 会在响应头已经发出、状态码已经定成 200 之后才抛出来，客户端只会看到
        # 一条断掉的流（而不是一个可读的错误），这是实测踩到的坑。
        self.validate_session(user_id, session_id, session_model)

        row = ChatMessage(
            user_id=user_id,
            session_id=session_id,
            select_model=select_model,
            request_id=uuid.uuid4().hex,
            request_text=request_text,
            response_text="",
            request_segments=[],
            response_segments=[],
            response_payload=None,
            status=0,
            file_extracted_text="",
        )
        self.db.add(row)
        self.db.commit()
        self.db.refresh(row)

        yield _sse_data({"type": "start", "message_id": row.id, "mode": "agent_loop"})

        result = LoopResult()
        error_message = ""
        try:
            generator = self.run_events(
                user_id=user_id,
                session_id=session_id,
                request_text=request_text,
                knowledge_base_id=knowledge_base_id,
                # 历史截断点 = 刚落库的占位行 id，保证历史里没有"本轮问题"的重复
                history_before_message_id=row.id,
                session_model=session_model,
            )
            while True:
                try:
                    event = next(generator)
                except StopIteration as stop:
                    result = stop.value or LoopResult()
                    break
                yield _sse_data(event)
        except Exception as exc:
            error_message = str(exc)
            main_logger.exception("learning_partner loop failed", exc_info=exc)
        finally:
            row.response_text = result.answer or error_message
            # 落库形状与 done 事件里的 trace **完全一致**（同一个 helper）：
            # 前端刷新后从 response_payload 读到的轨迹，与流式期间累积的轨迹同构，
            # AgentTrace 组件因此只需要一套渲染逻辑。
            row.response_payload = self._trace_payload(result)
            self.db.add(row)
            self.db.commit()
            # 第 3 期可观测：同一份轨迹另存到 workflow_runs（type=agent_loop），可按
            # run_id 查询；展示职责仍归 response_payload。失败只记日志——此时回答
            # 已经落库完成，轨迹存储绝不能把一次成功的回答拖成报错。
            persist_loop_trace(
                self.db,
                user_id=user_id,
                session_id=session_id,
                message_id=row.id,
                request_text=request_text,
                result=result,
                usage=result.usage,
                error_message=error_message,
            )
            if result.answer and not error_message:
                MemoryService(self.db).working.append_turn(
                    user_id=user_id,
                    session_id=session_id,
                    user_msg=request_text,
                    ai_msg=result.answer,
                )

        if error_message:
            yield _sse_data({"type": "error", "message": error_message})
        yield _sse_data(
            {
                "type": "done",
                "response_text": result.answer,
                "sources": [],
                # 保留原有扁平字段（老客户端不该因为多了一个 trace 而拿不到 steps）
                "stop_reason": result.stop_reason,
                "steps": result.steps,
                # 第 3 期可观测：本次回答的 token 用量（分步 + 汇总）
                "usage": result.usage,
                # 新增：完整轨迹（含工具名与参数），供前端渲染步骤时间线
                "trace": self._trace_payload(result),
            }
        )

    @staticmethod
    def _trace_payload(result: LoopResult) -> dict[str, Any]:
        """把 loop 结果收敛成前端可渲染的轨迹结构（live 与落库共用同一形状）。"""
        return {
            "type": "agent_loop_trace",
            "stop_reason": result.stop_reason,
            "steps": result.steps,
            "tool_calls": result.tool_calls,
            # 第 3 期可观测：token 用量跟轨迹同进同出（response_payload / done 事件 /
            # workflow_runs.state_json 三处形状一致）
            "usage": result.usage,
        }

    # ------------------------------------------------------------------ loop 内部
    def _build_messages(
        self,
        *,
        user_id: int,
        session_id: int,
        request_text: str,
        knowledge_base_id: int | None,
        history_before_message_id: int | None = None,
        load_history: bool = True,
        mcp_tools: dict[str, Any] | None = None,
    ) -> list[Any]:
        """组装消息列表。

        这里**不用 `ChatPromptTemplate`**（与单轮聊天不同）：`build_stream_chain_context`
        会把 system 提示词里的 `{` / `}` 转义成 `{{` / `}}`，而工具的 JSON Schema 与
        trace 说明里必然带花括号，转义后模型看到的是双花括号。直接构造 message 列表
        可以完全绕开模板语法，也不损失历史记忆能力（历史仍来自 `MemoryService`）。
        """
        system_prompt = build_personalized_prompt(
            db=self.db, user_id=user_id, agent_name=SUPERVISOR_AGENT, scene="workflow"
        ) or (
            "你是一个可靠、务实的学习搭档，基于用户自己的资料回答问题，不要编造资料里没有的内容。"
        )
        system_prompt = f"{system_prompt}\n\n{_LOOP_SYSTEM_SUFFIX}\n{describe_workers_for_prompt()}"
        # 本会话挂了知识库时明确告诉模型：检索已被限定在这一个库里，出题也**不用再问用户**
        # 选哪个库（`_delegate_worker` 会把它补进委派参数）。没有这一句时模型会反问
        # "你想用哪个知识库"，而用户明明已经在输入区挂好了（第 24 轮实测）。
        mounted = _mounted_knowledge_base_note(self.db, user_id, knowledge_base_id)
        if mounted:
            system_prompt += f"\n\n{mounted}"
        # 挂了外部 MCP 工具时把清单写进提示词（没有时返回空串，不占预算）。
        # 传的是**已包装好的工具表**而不是 None：None 会让它再去问一次管理器，
        # 而这里已经有权威答案了（少一次连接、也保证提示词里的名字与白名单必然一致）。
        system_prompt += describe_mcp_tools_for_prompt(mcp_tools or {})

        messages: list[Any] = [SystemMessage(content=system_prompt)]
        if load_history:
            messages.extend(
                self._load_history(
                    user_id,
                    session_id,
                    request_text,
                    before_message_id=history_before_message_id,
                )
            )
        messages.append(HumanMessage(content=request_text))
        return messages

    def _load_history(
        self,
        user_id: int,
        session_id: int,
        request_text: str,
        *,
        before_message_id: int | None = None,
    ) -> list[Any]:
        """复用统一记忆入口拿历史；失败时退化为无历史，不影响 loop 可用性。"""
        if before_message_id is None:
            before_message_id = self._latest_message_id(user_id, session_id)
        if before_message_id <= 0:
            return []
        try:
            _, stream_inputs = build_stream_chain_context(
                db=self.db,
                user_id=user_id,
                session_id=session_id,
                current_message_id=before_message_id,
                provider=self.provider,
                request_text=request_text,
                system_prompt="占位（历史加载用）",
                rag_context="",
            )
            history = list(stream_inputs.get("history") or [])
        except Exception as exc:
            main_logger.warning("Agent loop 历史记忆加载失败，将以无历史运行：{}", exc)
            return []
        # 只保留对话消息：SystemMessage 会被 loop 自己的 system prompt 取代
        return [message for message in history if not isinstance(message, SystemMessage)]

    def _latest_message_id(self, user_id: int, session_id: int) -> int:
        """本会话当前最大的 message id，作为"这一轮之前"的历史截断点。"""
        try:
            from sqlalchemy import func, select

            value = self.db.scalar(
                select(func.max(ChatMessage.id)).where(
                    ChatMessage.user_id == user_id,
                    ChatMessage.session_id == session_id,
                )
            )
        except Exception:
            return 0
        return int(value or 0)

    def _bind_tools(self, llm: Any, runtime: ToolRuntime) -> Any:
        """绑定原子工具；模型不支持工具调用时退化为纯对话（loop 仍能直接答）。"""
        model = llm or self._default_llm()
        tools = runtime.available()
        if not tools:
            return model
        try:
            return model.bind_tools(tools)
        except Exception as exc:
            main_logger.warning("模型不支持工具绑定，退化为一轮直答：{}", exc)
            return model

    def _default_llm(self) -> Any:
        from app.agent.llm import get_chat_model

        return get_chat_model(provider=self.provider)

    def _stream_model_step(
        self, bound: Any, messages: list[Any], handler: Any = None
    ) -> Generator[tuple[str, str], None, Any]:
        """流一次模型输出：**边到边 yield `(类型, 文本)`**，返回值是累积后的完整消息。

        类型只有两种：
        - `("reasoning", 文本)`：思考型模型的推理增量（`llm.py::REASONING_FIELD`）。**不是答案**；
        - `("delta", 文本)`：真正的回答增量，进 `answer_parts`。

        为什么必须是生成器（而不是返回 `(消息, 增量列表)` 的普通函数）：
        返回列表的写法得先把这个流**抽干**才能 `return`，于是模型其实已经全部生成完，
        增量才被调用方一口气重放出去——实测 12 片 × 50ms 的流，12 个 delta 事件
        全部落在同一毫秒（跨度 0.000s，平均间隔 0.0ms）。协议上是流式，观感上是
        "一起崩出来"。改成生成器后 yield 点回到了模型流内部，到达时间才被保留下来。

        **打字机效果的唯一来源就是这个时间信息**：前端不做任何字符级平滑
        （参考实现 DeepSeek Harness 同样不做——它也只是逐 chunk 直通 + 按动画帧重绘）。

        累积仍然必须做：工具调用参数是碎片式的 `tool_call_chunks`，只有累积成完整
        消息才能解析出可执行的 `tool_calls`。**执行**工具需要完整消息，**显示**文字
        不需要——旧写法把这两件事绑成了一个判断，那才是根因。
        """
        accumulated: Any = None
        # 用量收集器挂在本步的流上（config 形参对 RunnableBinding 与验收假模型都成立）：
        # 真链路里 on_llm_end 在流结束时携带聚合 usage（实测，前提 stream_usage=True）。
        for chunk in bound.stream(messages, config={"callbacks": [handler]} if handler else None):
            accumulated = chunk if accumulated is None else accumulated + chunk
            reasoning = self._chunk_reasoning(chunk)
            if reasoning:
                yield ("reasoning", reasoning)
            text = self._chunk_text(chunk)
            if text:
                yield ("delta", text)
        if accumulated is None:
            accumulated = AIMessage(content="")
        return accumulated

    @staticmethod
    def _chunk_reasoning(chunk: Any) -> str:
        """这一段 chunk 里的推理文本（非思考模型没有，返回空串）。

        字段由 `app/agent/llm.py::ReasoningAwareChatOpenAI` 补进 `additional_kwargs`
        ——原生 `ChatOpenAI` 会把这个非标准字段**丢掉**（见那个类的 docstring）。
        """
        extra = getattr(chunk, "additional_kwargs", None)
        if not isinstance(extra, dict):
            return ""
        value = extra.get(REASONING_FIELD)
        return value if isinstance(value, str) else ""

    def _delegate_worker(
        self,
        gateway: Any,
        name: str,
        args: dict[str, Any],
        *,
        user_id: int,
        session_id: int,
    ) -> tuple[str, dict[str, Any]]:
        """委派 worker，返回 `(给模型看的观察文本, 委派结果原始 dict)`。

        观察文本是 loop 回灌给模型的东西；原始 dict 给调用方用——出题委派要从
        `data.run_id` 回推出题消息的认领键，靠解析观察文本猜太脆
        （那是 JSON 字符串，`default=str` 之后形状不再可控）。
        """
        payload = {**args, "session_id": session_id, "session_model": self._session_model}
        # 本会话挂了知识库而模型没指定时，沿用挂载的库（第 24 轮）：
        # 用户已经挂好了库，出题再被问一次"用哪个知识库"是明显的倒退——参数卡上那一项
        # 会因为拿到 id 而变成"已定"，不再追问。模型显式传了值就尊重模型（含 0=不用库）。
        if (
            name == "delegate_quiz"
            and "knowledge_base_id" not in args
            and self._knowledge_base_id is not None
        ):
            payload["knowledge_base_id"] = self._knowledge_base_id
        # 讲解同样要认"本会话挂载的库"：不带上它，讲解就只会用模型自己的知识，
        # 而用户明明挂了资料（出题会认、讲解不认，是同一类明显的不一致）。
        if (
            name == "delegate_explain"
            and "knowledge_base_id" not in args
            and self._knowledge_base_id is not None
        ):
            payload["knowledge_base_id"] = self._knowledge_base_id
        if name == "delegate_quiz" and self._request_text:
            # 出题要求一律用**用户原话**（第 25 轮）：模型爱把"出题"扩写成一段自编知识范围
            # （"围绕「大模型核心原理」…侧重 Transformer/Embedding…"），那段字进了出题提示词的
            # `用户要求：` 一栏后**压过知识库考纲**，题就出成了模型自己编的知识体系；
            # 只带知识库 id、不乱编的那次反而正常——用户报的就是这个差异。
            # 模型写的那段不丢：留在 `model_brief` 里进 trace，只是不进提示词、不再冒充用户的话。
            # （`self._request_text` 为空说明这一轮不是正常入口驱动，那就别动模型的入参。）
            model_brief = str(payload.get("request_text") or "").strip()
            if model_brief and model_brief != self._request_text:
                payload["model_brief"] = model_brief
            payload["request_text"] = self._request_text
        try:
            result = gateway.delegate(
                name,
                payload,
                user_id=user_id,
                session_id=session_id,
            )
        except Exception as exc:
            return (
                json.dumps(
                    {"worker_error": f"{type(exc).__name__}: {exc}", "worker": name},
                    ensure_ascii=False,
                ),
                {"worker": name, "ok": False},
            )
        return json.dumps(result, ensure_ascii=False, default=str), result

    @staticmethod
    def _delegated_run_id(name: str, delegated: Any) -> str:
        """从委派结果里取出**需要前端认领**的 run_id；其余委派或失败时返回空串。

        认领名单只有两个：`delegate_quiz`（题目消息）与 `delegate_explain`（知识卡消息）——
        它们的共同点是"worker 自己往会话里写了一条消息"。评测/归档的产物在别处展示
        （结果弹窗 / 笔记本），多刷一次消息列表只会造成闪烁。
        """
        if name not in {"delegate_quiz", "delegate_explain"}:
            return ""
        if not isinstance(delegated, dict) or not delegated.get("ok"):
            return ""
        data = delegated.get("data")
        if not isinstance(data, dict):
            return ""
        return str(data.get("run_id") or "")

    # ------------------------------------------------------------------ 解析工具
    @staticmethod
    def _extract_tool_calls(message: Any) -> list[Any]:
        raw = getattr(message, "tool_calls", None) or []
        calls: list[Any] = []
        for call in raw:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
            if name:
                calls.append(call)
        return calls

    @staticmethod
    def _call_name(call: Any) -> str:
        if isinstance(call, dict):
            return str(call.get("name") or "")
        return str(getattr(call, "name", "") or "")

    @staticmethod
    def _call_args(call: Any) -> dict[str, Any]:
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
        return dict(args) if isinstance(args, dict) else {}

    @staticmethod
    def _call_id(call: Any) -> str:
        if isinstance(call, dict):
            return str(call.get("id") or "")
        return str(getattr(call, "id", "") or "")

    @staticmethod
    def _as_assistant_message(message: Any, tool_calls: list[Any]) -> AIMessage:
        """把累积的 chunk 归一成 AIMessage，保证 tool_calls 是规范形态。"""
        normalized = [
            {
                "name": LearningPartnerService._call_name(call),
                "args": LearningPartnerService._call_args(call),
                "id": LearningPartnerService._call_id(call),
                "type": "tool_call",
            }
            for call in tool_calls
        ]
        return AIMessage(
            content=LearningPartnerService._message_text(message),
            tool_calls=normalized,
        )

    @staticmethod
    def _chunk_text(chunk: Any) -> str:
        content = getattr(chunk, "content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return ""

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", message)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(item.get("text", ""))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        return str(content or "")
