"""轻量流程总控服务：根据前端智能体名称和关键词或语义意图选择工作流或通用聊天。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Union

from fastapi import BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.settings import settings
from app.db import SessionLocal
from app.services.a2a_handoff_service import A2AHandoffService
from app.services.difficulty_learner_service import (
    DIFFICULTY_LEARNER_AGENT,
    DIFFICULTY_WORKFLOW_TYPE,
    DifficultyLearnerService,
)
from app.services.learning_workflow_service import LEARNING_WORKFLOW_TYPE, LearningWorkflowService
from app.services.prompt_template_service import AGENT_CONFIG
from app.services.quiz_params_service import normalize_knowledge_base_id
from app.services.stream_chat_service import StreamChatService
from app.utils.intent_classifier import classify_intent


CHAT_ROUTE_TYPE = "chat"
WORKFLOW_ROUTE_TYPE = "workflow"
LOOP_ROUTE_TYPE = "loop"
QUIZ_AGENT = "quiz_generate_workflow"

# ---------------------------------------------------------------------------
# 第 2 期门控（升级规划 §八「第 2 期」第一条）
#
# 目标：`agent_name=None`（前端不再指定角色）时由**系统**决定走哪条路：
#   - 简单问答 → 原样走今天的单轮 RAG 流式聊天（**延迟零变化**，这是硬指标）
#   - 多步 / 跨流程意图 → 进 agent loop（supervisor 自己规划、调工具、委派 worker）
#
# 复用现成资产、零新增模型：先 `WORKFLOW_ROUTE_RULES` 关键词快筛，再
# `classify_intent` 语义兜底。**判定不了就走聊天**——语义模型不可用（Redis/模型缺失）
# 时宁可保持现状，也不要为了"显得智能"把简单问答塞进 8 步 loop。
# ---------------------------------------------------------------------------
LOOP_PATH = "loop"
CHAT_PATH = "chat"

# 默认入口模式（2026-10-18 起）：
#   - `loop`（默认）：**一律进 agent loop**，由模型自己决定要不要调工具。用户不再需要
#     理解"普通聊天 / 智能体选哪个"，这与 DSH 的心智模型一致；简单问答在 loop 里走
#     "无需工具直接答"的快速路径，同样**只调用 1 次模型**。
#   - `gated`：保留原来的门控分流（简单问答 → 单轮 RAG 聊天），用于需要"简单问答完全
#     不受 agent 侧改动影响"的场合，也用于实测两种模式的首字延迟差。
AGENT_ENTRY_MODE_LOOP = "loop"
AGENT_ENTRY_MODE_GATED = "gated"

# 强动作词：一旦出现，几乎必然要求"系统真的做一件事"（而不是解释一件事）
_STRONG_ACTION_WORDS: tuple[str, ...] = (
    "出一套题",
    "出题",
    "出几道",
    "生成题",
    "出个题",
    "练习",
    "刷题",
    "测验",
    "测评",
    "小测",
    "评分",
    "批改",
    "归档",
    "总结成笔记",
    "整理成笔记",
    "存成笔记",
)
# 弱动作词：单个出现时单轮聊天就能办好（"讲讲这道题"），
# 只有和"顺承标记"或"多个动作"同现时才算多步交付
_WEAK_ACTION_WORDS: tuple[str, ...] = (
    "讲解",
    "讲讲",
    "讲一下",
    "解释一下",
    "评测",
    "看看哪里弱",
    "查一下",
    "总结",
)
# 顺承标记：出现就说明用户要的是"一串动作"
_SEQUENCE_MARKERS: tuple[str, ...] = (
    "然后",
    "接着",
    "并且",
    "同时",
    "最后",
    "再帮我",
    "顺便",
    "以及",
    "之后",
    "先",
)

_SIMPLE_QUESTION_EXAMPLES: tuple[str, ...] = (
    "这句话是什么意思",
    "这个知识点怎么理解",
    "资料里是怎么说的",
    "帮我解释一下这个词",
    "什么是向量数据库",
    "这个函数的参数有哪些",
)

_LOOP_EXAMPLES: tuple[str, ...] = (
    "帮我看看哪里弱，针对弱点出一套题，做完给我讲解，最后归档成笔记",
    "先查一下我的掌握度，然后按薄弱点出题",
    "出一套练习题，我做完你帮我评分并总结错题",
    "针对我的错题做讲解，然后把讲解归档成笔记",
    "看看我的练习历史，挑最差的章节出题考我",
)


@dataclass(frozen=True)
class WorkflowRouteRule:
    """工作流路由规则：先关键词快筛，再语义兜底。

    `force_workflow` 解决的是"**用户已经明确选了模式**，却还被文本内容重新判一遍"这件事：
    前端选了某个智能体，等于用户说"我就要这个"，此时再拿关键词/语义去否决他的选择，
    结果就是他点了「难点讲解」却拿到一段普通聊天。默认 False（保持既有智能体的零回归），
    只给"整句话就是主题、没有动词"这类场景打开。

    它是**最后一道兜底**（关键词、语义都试过之后才用），不是优先级最高的规则：
    这样日志里仍能看到"关键词快筛命中"这类真实原因。
    """

    workflow_type: str
    required_keywords: tuple[str, ...]
    intent_examples: tuple[str, ...]
    threshold: float = 0.5
    # 关键词与语义都没命中时，是否仍进入工作流（仅对"无需额外参数"的工作流打开）
    force_workflow: bool = False


WORKFLOW_ROUTE_RULES: dict[str, WorkflowRouteRule] = {
    QUIZ_AGENT: WorkflowRouteRule(
        workflow_type=LEARNING_WORKFLOW_TYPE,
        required_keywords=("题", "题目", "出题", "刷题", "练习", "测验", "测评", "小测", "面试题"),
        intent_examples=(
            "帮我生成一套学习测评",
            "我想做个测试",
            "来个随堂小考",
            "检测一下我对这个知识点的掌握情况",
        ),
    ),
    # 难点讲解（第一期：一张知识卡 + 可选配图）
    DIFFICULTY_LEARNER_AGENT: WorkflowRouteRule(
        workflow_type=DIFFICULTY_WORKFLOW_TYPE,
        required_keywords=("讲解", "讲讲", "讲一下", "讲一讲", "讲下", "难点", "不懂", "没听懂"),
        intent_examples=(
            "帮我讲讲这个知识点",
            "这道题我没弄懂，深入讲一下",
            "把这个问题拆开给我讲清楚",
        ),
        # 为什么这里必须 force：用户是**点着「难点讲解」进来的**，输入框里写的常常只是
        # 一个主题（"大模型参数的演化"），里面一个动词都没有。若仍按关键词/语义判，
        # 就会退化成普通聊天——用户明明选了这个模式却拿不到知识卡。
        # 出题侧不开这个开关：出题要参数（数量/难度），用户没表达出题意图时
        # 强行进工作流没有意义；讲解侧则不需要参数，任何主题都能讲。
        force_workflow=True,
    ),
}


class OrchestratorService:
    """流程总控：只负责路由选择和启动对应服务，不承载具体业务节点。"""

    def __init__(self, db: Session):
        self.db = db

    def start_task(
        self,
        *,
        user_id: int,
        session_id: int,
        request_text: str,
        select_model: str,
        session_model: int = 0,
        agent_name: str | None = None,
        question_count: int | None = None,
        knowledge_base_id: int | None = None,
        context_mode: str = "outline",
        divergence: int | None = None,
        difficulty: str | None = None,
        provider: str = "deepseek",
    ) -> Union[dict[str, Any], StreamingResponse]:
        """根据路由结果启动工作流，未命中时直接调用流式聊天服务。"""
        print("\n" + "="*80)
        print(f"[Orchestrator] 收到任务请求: agent_name='{agent_name}', session_id={session_id}")
        print(f"[Orchestrator] 用户输入: {request_text[:100]}...")

        clean_text = request_text.strip()
        clean_agent_name = agent_name.strip() if agent_name else None

        # 默认入口（第 2 期）：前端没指定角色时，由系统决定走 loop 还是单轮聊天。
        # 带 agent_name 的老调用**完全不走这里**，因此 5 条既有路径零回归。
        if not clean_agent_name:
            gate = self.classify_entry_path(clean_text, clean_agent_name)
            print(f"[Orchestrator] 入口决策: path={gate['path']}（{gate['reason']}）")
            if gate["path"] == LOOP_PATH:
                return self._start_loop(
                    user_id=user_id,
                    session_id=session_id,
                    request_text=clean_text,
                    select_model=select_model,
                    session_model=session_model,
                    # loop 只认"有效库"：`0`（前端"明确不使用知识库"的哨兵）在这里收敛成
                    # None，否则 search_material 会按 `knowledge_base_id = 0` 过滤（搜不到东西）。
                    # 出题分支则要把 `0` 原样带下去——参数卡靠它知道"用户已经选了不用库"。
                    knowledge_base_id=normalize_knowledge_base_id(knowledge_base_id),
                    provider=provider,
                )

        route = self.route(clean_agent_name, clean_text)
        if route.get("workflow_type") == LEARNING_WORKFLOW_TYPE:
            print(f"[Orchestrator] 路由决策: {route.get('route_type')} -> {route.get('workflow_type')}")
            print(f"[Orchestrator] 决策原因: {route.get('reason')}")
            state = LearningWorkflowService(self.db).start(
                user_id=user_id,
                session_id=session_id,
                request_text=clean_text,
                question_count=question_count,
                knowledge_base_id=knowledge_base_id,
                context_mode=context_mode,
                divergence=divergence,
                difficulty=difficulty,
                provider=provider,
            )
            return {
                "route_type": WORKFLOW_ROUTE_TYPE,
                "target_agent": QUIZ_AGENT,
                "workflow_type": LEARNING_WORKFLOW_TYPE,
                "run_id": state["run_id"],
                "status": state["status"],
                # 参数没齐时不出题，改由前端把参数卡消息渲染出来（见 quiz_params_service）
                "data": {
                    "quiz": state.get("quiz") or {},
                    "params": state.get("params") or {},
                },
            }

        if route.get("workflow_type") == DIFFICULTY_WORKFLOW_TYPE:
            print(f"[Orchestrator] 路由决策: {route.get('route_type')} -> {route.get('workflow_type')}")
            print(f"[Orchestrator] 决策原因: {route.get('reason')}")
            # 同步执行：出图要几十秒，但前端这条路本来就带"智能体正在运行…"的等待提示，
            # 而重试+超时都收敛在服务层内部（见 difficulty_learner_service）。
            result = DifficultyLearnerService(self.db, provider=provider).start(
                user_id=user_id,
                session_id=session_id,
                request_text=clean_text,
                knowledge_base_id=normalize_knowledge_base_id(knowledge_base_id),
                provider=provider,
            )
            return {
                "route_type": WORKFLOW_ROUTE_TYPE,
                "target_agent": DIFFICULTY_LEARNER_AGENT,
                "workflow_type": DIFFICULTY_WORKFLOW_TYPE,
                "run_id": result["run_id"],
                "status": result["status"],
                "data": result["data"],
            }

        chat_scene = AGENT_CONFIG[clean_agent_name]["scene"] if clean_agent_name else None
        print("[Orchestrator] 未命中工作流，直接调用 StreamChatService.stream_chat")
        return StreamChatService.stream_chat(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            session_model=session_model,
            request_text=request_text,
            select_model=select_model,
            provider=provider,
            agent_name=clean_agent_name,
            scene=chat_scene,
        )

    def dispatch_a2a_handoff(
        self,
        state: dict[str, Any],
        user_id: int,
        background_tasks: BackgroundTasks | None = None,
    ) -> dict[str, Any]:
        """统一触发 A2A 接力派发；可选后台执行，避免接口同步等待下游 Agent。"""
        # `session_id` 一并进 context：`note_archiver` 的"本会话最近完成的一场"兜底要靠它
        # 划范围（拿不到会退化成"该用户任意一场"）。自动接力与 supervisor 委派共用同一个
        # handler，两条入口的 context 形状必须对齐，否则 handler 只能二选一地写。
        context: dict[str, Any] = {"state": state, "user_id": user_id}
        if isinstance(state, dict) and state.get("session_id") is not None:
            context["session_id"] = state.get("session_id")

        if background_tasks is None:
            return A2AHandoffService(self.db).dispatch(
                state.get("a2a_envelope"),
                context=context,
            )

        envelope = state.get("a2a_envelope") if isinstance(state, dict) else None
        if not isinstance(envelope, dict) or not envelope.get("next_agent"):
            return {"status": "skipped", "next_agent": None, "reason": "没有需要接力的下游 Agent"}

        background_tasks.add_task(self._dispatch_a2a_handoff_task, state, user_id)
        return {"status": "pending", "next_agent": envelope.get("next_agent"), "reason": "A2A 接力已提交后台执行"}

    @staticmethod
    def _dispatch_a2a_handoff_task(state: dict[str, Any], user_id: int) -> None:
        db = SessionLocal()
        try:
            result = OrchestratorService(db).dispatch_a2a_handoff(state, user_id)
            print(f"[Orchestrator][A2A] 后台接力结束：run_id={state.get('run_id')}, next_agent={result.get('next_agent')}, status={result.get('status')}")
        except Exception as exc:
            print(f"[Orchestrator][A2A] 后台接力异常：run_id={state.get('run_id')}, error={exc}")
        finally:
            db.close()

    def classify_entry_path(
        self, request_text: str, agent_name: str | None = None
    ) -> dict[str, str | None]:
        """决定这条请求走"单轮流式聊天"还是"agent loop"。

        **两种模式**（`AGENT_ENTRY_MODE`）：

        - `loop`（默认）：直接进 loop，不再分流。loop 里"无需工具直接答"的快速路径
          仍然只调用 1 次模型，所以简单问答没有多花一次往返；代价是模型多绑了工具
          Schema（一点 prefill 开销），换来的是"资料型问题会被自动检索"这类能力。
        - `gated`：保留原门控——关键词快筛命中 → loop；否则 `classify_intent` 语义
          兜底，**判不出来一律走 chat**（宁可少一次 loop，也不让简单问答多走 7 步）。
          语义模型不可用时 `classify_intent` 返回 default_label（=chat），门控自然退化为
          "只认关键词"，与升级前行为一致。

        显式指定 `agent_name` 时**不经过这里**（调用方已经先判过），保持确定性快车道。
        """
        text = (request_text or "").strip()
        if not text:
            return {"path": CHAT_PATH, "reason": "空输入"}

        if settings.agent_entry_mode == AGENT_ENTRY_MODE_LOOP:
            return {
                "path": LOOP_PATH,
                "reason": f"默认入口：一律进 agent loop（AGENT_ENTRY_MODE={AGENT_ENTRY_MODE_LOOP}）",
            }

        hit = self._fast_path_loop_reason(text)
        if hit:
            return {"path": LOOP_PATH, "reason": hit}

        intent_match = classify_intent(
            text=text,
            examples={
                LOOP_PATH: _LOOP_EXAMPLES,
                CHAT_PATH: _SIMPLE_QUESTION_EXAMPLES,
            },
            threshold=0.5,
            default_label=CHAT_PATH,
        )
        if intent_match.label == LOOP_PATH:
            return {
                "path": LOOP_PATH,
                "reason": f"语义判定为多步任务(score: {intent_match.score:.2f})",
            }
        return {
            "path": CHAT_PATH,
            "reason": (
                f"判定为简单问答，走原单轮聊天(score: {intent_match.score:.2f})"
            ),
        }

    @staticmethod
    def _fast_path_loop_reason(text: str) -> str | None:
        """零成本快筛：什么情况下**一定**该进 loop。

        三个判据（任一成立），刻意保守——误判成 loop 的代价（简单问答多走 7 步）
        远大于误判成 chat 的代价（少一次自动多步协作）：
        1. 出现强动作词 **且** 有顺承标记（"帮我看看哪里弱，针对弱点出一套题"）；
        2. 出现两个以上不同的动作词（"出题并评分"）；
        3. 强动作词 + 长文本（≥60 字，通常是在描述一串需求）。
        注意**标点不算判据**：本项目里"extract() 是做什么的，参数有哪些"这种
        带逗号的简单提问很常见，不能因此把它塞进 loop。
        """
        strong = [word for word in _STRONG_ACTION_WORDS if word in text]
        weak = [word for word in _WEAK_ACTION_WORDS if word in text]
        has_sequence = any(marker in text for marker in _SEQUENCE_MARKERS)

        if strong and has_sequence:
            return f"关键词快筛：强动作({strong[0]}) + 顺承标记"
        if len(strong) + len(weak) >= 2:
            actions = (strong + weak)[:3]
            return f"关键词快筛：多个动作({'、'.join(actions)})"
        if strong and len(text) >= 60:
            return f"关键词快筛：强动作({strong[0]}) + 长需求描述"
        return None

    def _start_loop(
        self,
        *,
        user_id: int,
        session_id: int,
        request_text: str,
        select_model: int,
        session_model: int,
        knowledge_base_id: int | None,
        provider: str,
    ) -> StreamingResponse:
        """进 agent loop：supervisor 自己规划、调工具、委派 worker，并以 SSE 回推轨迹。"""
        from app.agent.learning_partner import LearningPartnerService

        service = LearningPartnerService(
            self.db, provider=provider, session_model=session_model
        )
        # 会话归属校验放在这里（建流之前），保证"会话不存在"返回的是 404 JSON，
        # 而不是一条 200 之后才断掉的 SSE 流。
        service.validate_session(user_id, session_id, session_model)

        generator = service.stream(
            user_id=user_id,
            session_id=session_id,
            session_model=session_model,
            request_text=request_text,
            select_model=select_model,
            knowledge_base_id=knowledge_base_id,
        )
        return StreamingResponse(
            generator,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    def route(self, agent_name: str | None, request_text: str) -> dict[str, str | None]:
        """混合路由：关键词快筛 → 语义兜底 → 显式选择兜底。

        注意语义这层的**降级是静默的**：`classify_intent` 在嵌入模型加载失败时返回
        `default_label`（即 chat），所以模型没装好时这里会退化成纯关键词路由。
        这是既有行为（`SemanticIntentClassifier._get_model` 失败后不再重试），
        不要把它误当成"语义判定为聊天"——详见 `_semantic_hit()`。
        """
        if not agent_name:
            return {"route_type": CHAT_ROUTE_TYPE, "workflow_type": None, "reason": "未指定智能体，使用通用聊天"}

        if agent_name not in AGENT_CONFIG:
            raise_error(code=400, message="未知智能体", detail={"agent_name": agent_name})

        rule = WORKFLOW_ROUTE_RULES.get(agent_name)
        if not rule:
            return {"route_type": CHAT_ROUTE_TYPE, "workflow_type": None, "reason": "该智能体未配置工作流"}

        def hit(reason: str) -> dict[str, str | None]:
            return {
                "route_type": WORKFLOW_ROUTE_TYPE,
                "workflow_type": rule.workflow_type,
                "reason": reason,
            }

        # ① 关键词快筛：明确的动词/意图，不加载语义模型。
        if self._keyword_hit(request_text, rule.required_keywords):
            return hit("关键词快筛命中")

        # ② 语义兜底。不可用时必须区别于"语义判定为聊天"，所以单独封装。
        sem = self._semantic_hit(rule, request_text)
        if sem is not None:
            return hit(sem)

        # ③ 兜底的兜底：用户显式选了这个智能体，而且它的工作流**不需要额外参数**时，
        #    尊重用户的选择，不拿"文本里没动词"去否决他点了按钮这个事实。
        #
        #    为什么放在关键词/语义**之后**而不是之前：这条规则是安全网，不是主路径。
        #    放前面会让关键词表变成死代码、日志里再也看不到"关键词快筛命中"
        #    （而这行日志正是判断"路由为什么这么走"的主要依据）。
        #    结果一样（都进工作流），但诊断信息保住了。
        if rule.force_workflow:
            return hit("用户已显式选择该智能体，且工作流无需额外参数 → 兜底进入")

        return {"route_type": CHAT_ROUTE_TYPE, "workflow_type": None, "reason": "关键词和语义均未命中"}

    @staticmethod
    def _semantic_hit(rule: WorkflowRouteRule, request_text: str) -> str | None:
        """语义兜底；命中返回带分数的原因，**未命中或不可用返回 None**。

        区分"不可用"与"未命中"只是为了让日志说得清：以前两种情况都收敛成
        "关键词和语义均未命中"，模型根本没装好时也会这么写，排查时会被误导。
        """
        intent_match = classify_intent(
            text=request_text,
            examples={rule.workflow_type: rule.intent_examples},
            threshold=rule.threshold,
            default_label=CHAT_ROUTE_TYPE,
        )
        if intent_match.label != rule.workflow_type:
            return None
        # 语义分类器加载失败时会返回 default_label + score=0.0；label 撞上 workflow_type
        # 是不可能的（default_label 就是 chat），所以这里不需要额外判 score。
        return f"语义确认命中(score: {intent_match.score:.2f})"

    @staticmethod
    def _keyword_hit(text: str, keywords: tuple[str, ...]) -> bool:
        """宽松关键词快筛，明确出题意图无需加载语义模型。"""
        return any(keyword in text for keyword in keywords)
