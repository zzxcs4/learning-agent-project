"""Worker 委派层：把现有 4 个有角色人格的专家保留为 worker，由 supervisor 委派。

升级规划 §7.1 的关键修正（B′ 路线）：**新增委派关系，但不改角色身份**。

| 做法 | 角色身份 | 对多 agent 叙事 |
|---|---|---|
| 降级为 tool（函数、无提示词、无状态） | 不再是 Agent | 被削弱 ❌ |
| **保留为 worker（自己的提示词 + 子图，被 supervisor 委派）** | 仍是 Agent | **被强化** ✅ |

所以本文件的纪律是：
1. **`AGENT_CONFIG` 的提示词与 `app/agent/workflows/learning.py` 的子图一个字都不改**；
2. 委派必须走 **A2A 信封 + dispatcher 校验**（不是直接函数调用），复用第 1 期就建好的
   `HANDOFF_RULES` 白名单与置信度熔断；
3. worker 自己不实现第二套业务逻辑：出题/评测直接调用既有服务，讲解复用既有
   `difficulty_learner` 提示词位，归档走既有 `note_archiver` handler。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.agent.a2a import A2AEnvelope
from app.agent.llm import get_chat_model
from app.services.a2a_handoff_service import A2AHandoffService
from app.services.learning_workflow_service import LearningWorkflowService

# supervisor 在 A2A 信封里的身份名（同时也是 HANDOFF_RULES 的键）
SUPERVISOR_AGENT = "learning_partner"


@dataclass(frozen=True)
class WorkerSpec:
    """一个 worker 的角色说明：用于给 supervisor 的工具描述，也用于 trace 展示。"""

    name: str
    agent_name: str
    title: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)


WORKERS: dict[str, WorkerSpec] = {
    "delegate_quiz": WorkerSpec(
        name="delegate_quiz",
        agent_name="quiz_generate_workflow",
        title="出题 worker",
        description=(
            "委派「出题 Agent」按考纲与薄弱点出一套练习题，落库为一次练习并返回 run_id。"
            "用户做完后可以用 run_id 提交答案。"
            "**只传你确实知道的信息**：用户没说的不要替他编（尤其不要自己列知识范围/知识点清单），"
            "`request_text` 可以省略——系统会用用户的**原话**，范围以所挂知识库的考纲为准。"
            "**参数没传全时不会立刻出题**：会在对话里弹出参数卡（知识库/每轮题目数/发散度/难度）"
            "让用户确认，确认后题目自动出现——这时不要重复委派。"
        ),
        parameters={
            "request_text": "str|None(可省略；省略=用用户原话，不要自己改写或编造知识范围)",
            "question_count": "int|None(5–20,默认10)",
            "knowledge_base_id": "int|None(本会话挂载的库系统会自动带上；用户明确说不用库就传 0)",
            "divergence": "int|None(1聚焦薄弱…5全面探索)",
            "difficulty": "str|None(入门/进阶/高级)",
        },
    ),
    "delegate_evaluate": WorkerSpec(
        name="delegate_evaluate",
        agent_name="learning_evaluator",
        title="评测 worker",
        description=(
            "委派「学习测评评分 Agent」对一次已出题、待作答的练习评分并生成逐题点评。"
            "需要 run_id 与 answers；评分后掌握度与薄弱点会被更新。"
        ),
        parameters={"run_id": "str", "answers": "list[{question_id, answer}]"},
    ),
    "delegate_explain": WorkerSpec(
        name="delegate_explain",
        agent_name="difficulty_learner",
        title="难点讲解 worker",
        description=(
            "委派「难点学习 Agent」针对某个具体问题或错题做深入讲解（含资料依据）。"
        ),
        parameters={"topic": "str", "run_id": "str|None"},
    ),
    "delegate_archive": WorkerSpec(
        name="delegate_archive",
        agent_name="note_archiver",
        title="笔记归档 worker",
        description=(
            "委派「笔记归档 Agent」把一次练习的结果按既有归档口径写成结构化笔记。"
            "run_id 取自 list_practice_history 的返回（每场都带 run_id）；省略 run_id 时"
            "默认归档当前会话最近完成的一场学习测评。"
        ),
        parameters={"run_id": "str|None(省略=当前会话最近完成的一场)"},
    ),
}


# 讲解 worker 的兜底提示词：`prompt_templates` 表为空时用（与项目既有做法一致——
# `learning.py::_get_template_prompt` 拿不到就回退到代码内置提示词）。
_EXPLAIN_FALLBACK_PROMPT = (
    "你是一个难点讲解 Agent。请针对用户提出的知识点或错题做**由浅入深**的讲解：\n"
    "1. 先用一句话点明它到底在解决什么问题；\n"
    "2. 再讲清关键机制与容易混淆的地方；\n"
    "3. 最后给一个具体例子或自测问题。\n"
    "如果提供了参考资料，必须以资料为依据，资料里没有的不要编造。"
)


class WorkerGateway:
    """supervisor 委派 worker 的唯一入口：负责装信封、走 A2A 校验、执行、归一化结果。"""

    def __init__(self, db: Session, *, provider: str = "deepseek", llm: Any = None):
        self.db = db
        self.provider = provider
        self._llm = llm

    def delegate(
        self,
        worker_name: str,
        payload: dict[str, Any],
        *,
        user_id: int,
        session_id: int,
    ) -> dict[str, Any]:
        """委派一个 worker，返回统一结构的 dict（永不抛异常，失败也是结果）。"""
        # 注册必须在这里兜底，不能只在 loop 入口做：`delegate` 是公开入口
        # （验收脚本、后台任务都会直接调它），漏注册会让返回值变成
        # "目标 Agent handler 尚未实现"的 pending，而不是真正执行。
        register_worker_handlers()

        spec = WORKERS.get(worker_name)
        if spec is None:
            return {"worker": worker_name, "ok": False, "error": "未知 worker"}

        envelope = A2AEnvelope(
            from_agent=SUPERVISOR_AGENT,
            payload_type=worker_name,
            payload=dict(payload),
            next_agent=spec.agent_name,
            # supervisor 的委派是**用户显式要求**触发的，不是低质量模型的自动接力，
            # 所以置信度置 1.0 以通过 dispatcher 的熔断（熔断是为模型自动接力准备的）。
            confidence=1.0,
        )
        result = A2AHandoffService(self.db).dispatch(
            envelope.model_dump(),
            context={
                "user_id": user_id,
                "session_id": session_id,
                "gateway": self,
            },
        )
        return {
            "worker": worker_name,
            "agent": spec.agent_name,
            "title": spec.title,
            "ok": result.get("status") == "dispatched",
            "status": result.get("status"),
            "reason": result.get("reason"),
            "data": result.get("data") or {},
        }

    # ------------------------------------------------------------------ worker 实现
    def _run_quiz(self, envelope: A2AEnvelope, context: dict[str, Any]) -> dict[str, Any]:
        """出题 worker：调用既有学习工作流服务出题落库（不改 learning 子图）。"""
        payload = envelope.payload
        user_id = int(context["user_id"])
        session_id = int(payload.get("session_id") or context["session_id"])

        # 学习测评依赖"学习会话"（session_model=0）——这是既有约束，这里不改它。
        # 明确判一次并把话说清楚，比让用户看到一个 404「会话不存在」好得多。
        session_model = payload.get("session_model")
        if session_model is not None and int(session_model) != 0:
            raise ValueError(
                "出题需要学习会话（session_model=0），当前是笔记会话；"
                "请在学习会话里发起练习"
            )

        from app.services.session_service import SessionService

        SessionService.get_session_or_404(
            db=self.db, user_id=user_id, session_id=session_id, session_model=0
        )

        question_count = payload.get("question_count")
        state = LearningWorkflowService(self.db).start(
            user_id=user_id,
            session_id=session_id,
            request_text=str(payload.get("request_text") or "").strip() or "针对我的薄弱点出一套题",
            # 没传 = 没定：交给参数卡问用户（第 23 轮"只问缺的"口径）。
            # 不要在这里填默认值（发散度原来写 `or 3` 就是把"没定"伪装成"已定"）。
            question_count=int(question_count) if question_count else None,
            knowledge_base_id=_to_int_or_none(payload.get("knowledge_base_id")),
            context_mode=str(payload.get("context_mode") or "outline"),
            divergence=_to_int_or_none(payload.get("divergence")),
            difficulty=str(payload.get("difficulty") or "").strip() or None,
            provider=self.provider,
        )
        if state.get("status") == "awaiting_params":
            # 参数没齐：题目还没出，会话里已经写好一张参数卡。模型必须**停在这一步**：
            # 再委派一次会重复建 run（用户会看到两张卡）。
            return {
                "run_id": state.get("run_id"),
                "status": "awaiting_params",
                "params": state.get("params") or {},
                "note": (
                    "出题参数还没定：已在对话里写出参数卡（知识库/每轮题目数/发散度/难度），"
                    "请让用户先确认；确认后题目会自动出现，**本轮不要再委派出题**。"
                ),
            }
        quiz = state.get("quiz") or {}
        return {
            "run_id": state.get("run_id"),
            "status": state.get("status"),
            "question_count": len(quiz.get("questions") or []),
            "title": quiz.get("title"),
            "quiz": quiz,
        }

    def _run_evaluate(self, envelope: A2AEnvelope, context: dict[str, Any]) -> dict[str, Any]:
        """评测 worker：复用既有 submit_answer 链路（评分标准与掌握度回流都不动）。"""
        payload = envelope.payload
        user_id = int(context["user_id"])
        run_id = str(payload.get("run_id") or "").strip()
        answers = payload.get("answers") or []
        if not run_id:
            raise ValueError("缺少 run_id，无法评测")
        if not isinstance(answers, list) or not answers:
            raise ValueError("缺少 answers（形如 [{question_id, answer}]）")

        state = LearningWorkflowService(self.db).submit_answer(
            user_id=user_id,
            run_id=run_id,
            answers=answers,
        )
        return {
            "run_id": run_id,
            "status": state.get("status"),
            "score": state.get("score"),
            "review": state.get("review"),
            "weaknesses": state.get("weaknesses") or [],
            # 反思结论随 observation 回给主循环：委派链路上结论也进 trace（§7.2 "结论写入 trace"）
            "reflection": state.get("reflection") or {},
        }

    def _run_explain(self, envelope: A2AEnvelope, context: dict[str, Any]) -> dict[str, Any]:
        """难点讲解 worker：走与「难点讲解」入口**同一条**链路（卡 + 图 + 落库）。

        为什么不在这里自己调模型出一段文字（第 2 期的做法）：
        同一件事有两套实现，用户从"难点讲解"入口进和让学习搭档顺带讲一下，会得到
        完全不同的东西（一边有知识卡和图、一边只有一段 Markdown），而且两边的提示词
        会各自漂移。所以这里只做**参数归一**，业务全交给 `DifficultyLearnerService`。

        代价（刻意接受）：委派链路从"一次模型调用"变成"结构化 + 出图 + 落 COS"，
        loop 里这一步会明显变慢；换来的是产物一致、可刷新回看、图片进得了会话历史。
        """
        from app.services.difficulty_learner_service import DifficultyLearnerService

        payload = envelope.payload
        topic = str(payload.get("topic") or "").strip()
        if not topic:
            raise ValueError("缺少 topic，无法讲解")

        user_id = int(context["user_id"])
        session_id = int(payload.get("session_id") or context["session_id"])
        references = str(payload.get("references") or "").strip()
        # 本会话挂载的知识库：supervisor 侧会自动带上（见 learning_partner._delegate_worker）。
        # `0`（用户明确说不用库）在这里收敛成 None——它是"出题参数卡"的哨兵，讲解不认它。
        knowledge_base_id = _to_int_or_none(payload.get("knowledge_base_id")) or None

        return DifficultyLearnerService(
            self.db, provider=self.provider
        ).explain_for_loop(
            user_id=user_id,
            session_id=session_id,
            topic=topic,
            references=references,
            knowledge_base_id=knowledge_base_id,
        )

    # `delegate_archive` 没有自己的实现方法：归档口径（含"漏给 run_id 时落到本会话最近
    # 完成的一场"）全部在 `a2a_handoff_service._handle_note_archiver` 里，那里是进程内
    # **唯一**的 note_archiver handler。worker 层只负责把 supervisor 的 payload 装成信封
    # （见 `delegate`），不再另写一份归档逻辑，也不再二次注册那个 handler。

    def _resolve_agent_prompt(self, agent_name: str, scene: str) -> str | None:
        """按既有模板机制取角色提示词；表里没有就返回 None（由调用方兜底）。"""
        from app.services.prompt_template_service import prompt_template_manager

        try:
            return prompt_template_manager.get_full_prompt(agent_name, scene, self.db)
        except Exception:
            return None


def register_worker_handlers() -> None:
    """把**委派型** worker 注册进 A2A dispatcher（`note_archiver` 刻意不在这里注册）。

    单独函数而不是 import 副作用，是为了让"注册"这件事在 `learning_partner` 里显式可见，
    也让离线验收脚本可以在不触发注册的情况下测试未知 worker 的分支。

    这 3 个 handler 是"supervisor 委派"专用的：它们要求 `ctx["gateway"]`，因为委派链
    需要的是 worker 自己的业务实现（出题/评测/讲解）。它们**只可能**被
    `WorkerGateway.delegate` 派发到——那个入口一定会带上 `gateway`。

    `note_archiver` 不适用这个前提：除了委派，它还被**评分后的自动接力**
    （`OrchestratorService.dispatch_a2a_handoff`）派发，而那条路的 context 里没有
    `gateway`。历史版本在这里把它也注册成 `ctx["gateway"]` 版，于是自动归档必然
    `KeyError: 'gateway'` → 静默失败；而它的 handler 早在
    `a2a_handoff_service` 里就注册好了（`_handle_note_archiver`，自带 run_id 兜底）。
    所以这里**不要**再注册 `note_archiver`：一个键只能有一个 handler。
    """
    A2AHandoffService.register_handler(
        "quiz_generate_workflow", lambda db, env, ctx: ctx["gateway"]._run_quiz(env, ctx)
    )
    A2AHandoffService.register_handler(
        "learning_evaluator", lambda db, env, ctx: ctx["gateway"]._run_evaluate(env, ctx)
    )
    A2AHandoffService.register_handler(
        "difficulty_learner", lambda db, env, ctx: ctx["gateway"]._run_explain(env, ctx)
    )


def _message_text(message: Any) -> str:
    """兼容 str / list[dict] 两种 content 形态。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def _to_int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


def worker_tool_specs() -> list[dict[str, Any]]:
    """给前端 / 验收脚本用的 worker 清单（不依赖 LLM）。"""
    return [
        {
            "name": spec.name,
            "agent_name": spec.agent_name,
            "title": spec.title,
            "description": spec.description,
        }
        for spec in WORKERS.values()
    ]


def describe_workers_for_prompt() -> str:
    """把 worker 清单渲染进 supervisor 的提示词，让模型知道有哪些角色可委派。"""
    lines = ["可委派的 worker（有角色人格的专家 Agent）："]
    for spec in WORKERS.values():
        params = ", ".join(f"{key}:{value}" for key, value in spec.parameters.items())
        lines.append(f"- {spec.name}({params})：{spec.description}")
    # 原子工具清单从注册表派生：硬编码一份副本会在加工具时静默失同步
    # （模型看不到新工具 = 工具白写了；第 23 轮加知识库工具时就踩在这个点上）。
    from app.agent.tools import DEFAULT_TOOL_NAMES

    lines.append("可用的原子工具：" + " / ".join(sorted(DEFAULT_TOOL_NAMES)) + "。")
    lines.append("worker 与工具的区别：worker 有角色身份与自己的提示词，工具只是原子能力。")
    return "\n".join(lines)
