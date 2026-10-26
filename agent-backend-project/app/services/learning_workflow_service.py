"""学习测评工作流服务。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.agent.workflows.checkpoint import build_thread_config, get_workflow_checkpointer
from app.db.models import ChatMessage
from app.services.retry_service import RetryExecutor
from app.services.quiz_params_service import (
    build_params_spec,
    normalize_knowledge_base_id,
    resolve_submitted_params,
)
from app.services.session_service import SessionService
from app.services.workflow_run_service import WorkflowRunService


LEARNING_WORKFLOW_TYPE = "learning_quiz"


class LearningWorkflowService:
    """负责学习工作流启动、提交和恢复。"""

    def __init__(self, db: Session, llm=None):
        self.db = db
        self.llm = llm

    @staticmethod
    def _plain_state(state: Any) -> dict[str, Any]:
        """将 LangGraph/Pydantic 状态转换为可落库的普通 dict。"""
        if hasattr(state, "model_dump"):
            data = state.model_dump(mode="json")
        else:
            data = dict(state or {})
        if data.get("status") is not None:
            data["status"] = str(data["status"])
        return data

    def _create_learning_message(
        self,
        *,
        user_id: int,
        session_id: int,
        request_id: str,
        request_text: str,
        response_text: str,
        response_payload: dict[str, Any] | None,
    ) -> ChatMessage:
        """把学习工作流结果写入会话消息，供前端历史列表渲染。"""
        row = ChatMessage(
            user_id=user_id,
            session_id=session_id,
            select_model=2,
            request_id=request_id,
            request_text=request_text,
            response_text=response_text,
            request_segments=[],
            response_segments=[],
            response_payload=response_payload,
            status=0,
            file_extracted_text="",
        )
        self.db.add(row)
        self.db.commit()
        return row

    def _get_learning_quiz_message(
        self,
        *,
        user_id: int,
        session_id: int,
        run_id: str,
    ) -> ChatMessage | None:
        """获取出题阶段写入的聊天消息，用于提交答案后原地更新。"""
        stmt = (
            select(ChatMessage)
            .where(
                ChatMessage.user_id == user_id,
                ChatMessage.session_id == session_id,
                ChatMessage.request_id == f"{run_id}:quiz",
            )
            .order_by(ChatMessage.id.desc())
        )
        return self.db.scalar(stmt)

    def _update_learning_message(
        self,
        *,
        user_id: int,
        session_id: int,
        run_id: str,
        request_text: str,
        response_text: str,
        response_payload: dict[str, Any] | None,
    ) -> None:
        """提交答案后更新同一条聊天消息，避免历史里同时保留表单和结果。"""
        row = self._get_learning_quiz_message(
            user_id=user_id,
            session_id=session_id,
            run_id=run_id,
        )
        if row is None:
            self._create_learning_message(
                user_id=user_id,
                session_id=session_id,
                request_id=f"{run_id}:quiz",
                request_text=request_text,
                response_text=response_text,
                response_payload=response_payload,
            )
            return

        row.response_text = response_text
        row.response_payload = response_payload
        self.db.commit()

    def start(
        self,
        user_id: int,
        session_id: int,
        request_text: str,
        question_count: int | None = None,
        knowledge_base_id: int | None = None,
        context_mode: str = "outline",
        divergence: int | None = None,
        difficulty: str | None = None,
        provider: str = "deepseek",
    ) -> dict[str, Any]:
        """启动学习测评：参数齐了就直接出题，缺参数就先在对话里问（**只问缺的**）。

        第 23 轮起出题参数（知识库 / 每轮题目数 / 发散度 / 难度）不再由发送前的表单决定：
        调用方传了就是"已定"，没传的字段会被参数卡问出来（`status=awaiting_params`），
        用户提交后走 `configure()` 继续出题——两条入口（默认模式委派 / 直接出题测评）共用它。
        """
        SessionService.get_session_or_404(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            session_model=0,
        )

        run_id = uuid.uuid4().hex
        # `knowledge_base_id=0` 是"用户明确不使用知识库"的哨兵（参数卡/前端 pill 用它表达已选）。
        # 它只影响**规格判定**（不再问用户），落库与检索一律按"无库"（NULL）处理。
        bound_knowledge_base_id = normalize_knowledge_base_id(knowledge_base_id)
        state = {
            "run_id": run_id,
            "user_id": user_id,
            "session_id": session_id,
            "request_text": request_text.strip(),
            "question_count": question_count,
            # 知识库归属：为空时练习仍可正常出题，只是 practice_sets.knowledge_base_id 为空
            "knowledge_base_id": bound_knowledge_base_id,
            # 出题上下文模式：outline=只给考纲（默认）/ outline_with_source=考纲+本章原文
            "context_mode": context_mode,
            # 出题发散度：1=聚焦薄弱，5=全面探索（防止只在薄弱点上打转）
            "divergence": divergence,
            # 本次难度：入门/进阶/高级（目标认知层级，进提示词）
            "difficulty": difficulty,
            "provider": provider,
            "status": "created",
        }
        spec = build_params_spec(
            self.db,
            user_id=user_id,
            question_count=question_count,
            knowledge_base_id=knowledge_base_id,
            divergence=divergence,
            difficulty=difficulty,
        )
        if spec["missing"]:
            return self._await_params(
                state=state, run_id=run_id, spec=spec, session_id=session_id, user_id=user_id
            )

        run = WorkflowRunService.create_run(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            workflow_type=LEARNING_WORKFLOW_TYPE,
            status="created",
            state=state,
            run_id=run_id,
        )
        return self._generate_and_persist(run=run, state=state, request_text=request_text.strip())

    def _await_params(
        self,
        *,
        state: dict[str, Any],
        run_id: str,
        spec: dict[str, Any],
        session_id: int,
        user_id: int,
    ) -> dict[str, Any]:
        """参数不全：建一个 `awaiting_params` 的运行 + 在会话里写一张参数卡。"""
        run = WorkflowRunService.create_run(
            db=self.db,
            user_id=user_id,
            session_id=session_id,
            workflow_type=LEARNING_WORKFLOW_TYPE,
            status="awaiting_params",
            state=state,
            run_id=run_id,
        )
        state["run_id"] = run.run_id
        state["status"] = "awaiting_params"
        self._create_learning_message(
            user_id=user_id,
            session_id=session_id,
            request_id=f"{run.run_id}:quiz",
            request_text=str(state.get("request_text") or ""),
            response_text="请先确认本轮出题的参数，确认后我马上出题。",
            response_payload={
                "type": "learning_quiz",
                "version": 1,
                "run_id": run.run_id,
                "status": "awaiting_params",
                "params": spec,
            },
        )
        return {**state, "quiz": {}, "params": spec}

    def _prepare_configure(
        self,
        user_id: int,
        run_id: str,
        *,
        question_count: int | None = None,
        knowledge_base_id: int | None = None,
        divergence: int | None = None,
        difficulty: str | None = None,
        next_status: str,
        card_status: str | None = None,
    ) -> tuple[Any, dict[str, Any]]:
        """参数卡提交的**前半段**：校验 + 参数回落 + 把运行推进到 `next_status`。

        `card_status` 非空时顺手把那张参数卡消息就地改成"正在出题"（异步路径用）：
        用户刷新页面回来看到的是进度，而不是又一次"确认出题"（再点会撞 400）。
        """
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        if run.workflow_type != LEARNING_WORKFLOW_TYPE:
            raise_error(code=400, message="工作流类型不匹配")
        if run.status != "awaiting_params":
            raise_error(code=400, message="当前工作流不需要确认出题参数")

        state = dict(run.state_json)
        state["run_id"] = run.run_id
        spec = build_params_spec(
            self.db,
            user_id=user_id,
            question_count=state.get("question_count"),
            knowledge_base_id=state.get("knowledge_base_id"),
            divergence=state.get("divergence"),
            difficulty=state.get("difficulty"),
        )
        submitted = {
            "question_count": question_count,
            "knowledge_base_id": knowledge_base_id,
            "divergence": divergence,
            "difficulty": difficulty,
        }
        state.update(
            resolve_submitted_params(self.db, user_id=user_id, spec=spec, submitted=submitted)
        )
        state["status"] = next_status
        WorkflowRunService.update_run(self.db, run, next_status, state)
        if card_status:
            self._update_learning_message(
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.run_id,
                request_text=str(state.get("request_text") or ""),
                response_text="正在按你确认的参数出题，稍等一下。",
                response_payload={
                    "type": "learning_quiz",
                    "version": 1,
                    "run_id": run.run_id,
                    "status": card_status,
                    # 留着 spec，前端那张卡还渲染得出来（此时只展示进度、不再给表单）
                    "params": spec,
                },
            )
        return run, state

    def configure(
        self,
        user_id: int,
        run_id: str,
        *,
        question_count: int | None = None,
        knowledge_base_id: int | None = None,
        divergence: int | None = None,
        difficulty: str | None = None,
    ) -> dict[str, Any]:
        """参数卡提交（**同步**版）：校验参数 → 继续出题，并把那张参数卡消息**原地换成题目**。

        原地更新（而不是再写一条）：一次练习在会话里只占一条消息，历史回看时不会
        同时留着"参数卡"和"题目"两条（与提交答案后的原地更新同一口径）。

        路由层用的是异步版（`begin_configure` + `generate_for_run`）：真模型出题要
        30–60 秒，同步等会被前端 30 秒超时掐断（第 25 轮用户报的"报超时但其实出了题"）。
        这个方法留给服务级调用方与验收脚本（它们没有 HTTP 超时）。
        """
        run, state = self._prepare_configure(
            user_id,
            run_id,
            question_count=question_count,
            knowledge_base_id=knowledge_base_id,
            divergence=divergence,
            difficulty=difficulty,
            next_status="created",
        )
        return self._generate_and_persist(
            run=run, state=state, request_text=str(state.get("request_text") or "")
        )

    def begin_configure(
        self,
        user_id: int,
        run_id: str,
        *,
        question_count: int | None = None,
        knowledge_base_id: int | None = None,
        divergence: int | None = None,
        difficulty: str | None = None,
    ) -> dict[str, Any]:
        """参数卡提交（**异步版前半段**，路由层用）：校验+回落参数，立刻返回。

        返回的 `state` 要交给 `generate_for_run_in_background`（后台任务用**自己的**
        DB 会话；请求那个会话在响应返回后就被关掉了）。
        """
        run, state = self._prepare_configure(
            user_id,
            run_id,
            question_count=question_count,
            knowledge_base_id=knowledge_base_id,
            divergence=divergence,
            difficulty=difficulty,
            next_status="generating_quiz",
            card_status="generating_quiz",
        )
        return {"run_id": run.run_id, "status": "generating_quiz", "state": state}

    def generate_for_run(
        self, *, user_id: int, run_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        """异步路径的**后半段**：跑出题图并原地把"正在出题"换成题目。

        出错时除了 `mark_failed`（`_generate_and_persist` 里做的）还要**把消息改成失败说明**：
        否则用户看到的是一张永远停在"正在出题…"的卡，只会以为系统卡死了。
        """
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        try:
            return self._generate_and_persist(
                run=run, state=state, request_text=str(state.get("request_text") or "")
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务不能往外抛（抛了没人接）
            self._fail_quiz_message(run, request_text=str(state.get("request_text") or ""), reason=str(exc))
            raise

    @staticmethod
    def generate_for_run_in_background(user_id: int, run_id: str, state: dict[str, Any]) -> None:
        """FastAPI `BackgroundTasks` 入口：自带 DB 会话，异常只记日志（不影响已返回的响应）。"""
        from app.db.database import SessionLocal

        db = SessionLocal()
        try:
            LearningWorkflowService(db).generate_for_run(user_id=user_id, run_id=run_id, state=state)
        except Exception as exc:  # noqa: BLE001 - 后台失败只记日志
            print(f"[Learning][出题] 后台出题失败：run_id={run_id}, error={exc}")
        finally:
            db.close()

    def _fail_quiz_message(self, run: Any, *, request_text: str, reason: str) -> None:
        """原地把"正在出题…"改成失败说明（尽力而为：写不进去也不能再抛）。"""
        try:
            self._update_learning_message(
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.run_id,
                request_text=request_text,
                response_text=f"出题失败：{reason}。可以再说一次「出题」重试。",
                response_payload={
                    "type": "learning_quiz",
                    "version": 1,
                    "run_id": run.run_id,
                    "status": "failed",
                },
            )
        except Exception as exc:  # noqa: BLE001 - 二次失败只记日志
            print(f"[Learning][出题] 失败说明写入会话失败：run_id={run.run_id}, error={exc}")

    def _generate_and_persist(
        self, *, run: Any, state: dict[str, Any], request_text: str
    ) -> dict[str, Any]:
        """跑出题图并把结果落成会话消息（`start` 与 `configure` 共用的一段）。"""
        try:
            final_state = RetryExecutor.run(
                state=state,
                node_name="generate_quiz",
                operation=lambda: self._generate_quiz_state(run.run_id, state),
            )
        except Exception as exc:
            WorkflowRunService.mark_failed(self.db, run, state, "generate_quiz", str(exc))
            raise_error(code=500, message=f"生成学习题目失败: {exc}")

        final_state["run_id"] = run.run_id
        WorkflowRunService.update_run(self.db, run, "waiting_answer", final_state)
        quiz = final_state.get("quiz") or {}
        # 参数卡已经写过一条同 request_id 的消息时，这里把它原地换成题目（从卡变题）
        self._update_learning_message(
            user_id=run.user_id,
            session_id=run.session_id,
            run_id=run.run_id,
            request_text=request_text,
            response_text="已完成出题，请完成后提交答案。",
            response_payload={
                "type": "learning_quiz",
                "version": 1,
                "run_id": run.run_id,
                "status": "waiting_answer",
                "quiz": quiz,
            },
        )
        return final_state

    def submit_answer(
        self,
        user_id: int,
        run_id: str,
        answers: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """提交整套题答案并完成评分点评（**同步**版，服务级/老调用方）。

        路由层用的是异步版（`begin_submit` + `grade_for_run`）：评分同样是 LLM 调用
        （整套题 + 答案进提示词，20–40 秒），同步等会被前端 30 秒超时掐断
        （第 25 轮修出题那条时发现的同一类问题）。
        """
        run = self._prepare_submit(user_id, run_id, answers)
        return self._grade_and_persist(
            run=run, answers=answers, request_text=str((run.state_json or {}).get("request_text") or "")
        )

    def _prepare_submit(
        self, user_id: int, run_id: str, answers: list[dict[str, Any]], *, card_status: str | None = None
    ) -> Any:
        """提交答案的**前半段**：校验 + 把运行推进到 `grading`（答案先落进 state）。

        `card_status` 非空时把那条题目消息就地改成"正在评分"（**保留 quiz**）：
        用户刷新页面回来看到的是进度而不是一张已经交过的答题卡（再交会撞 400）。
        """
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        if run.workflow_type != LEARNING_WORKFLOW_TYPE:
            raise_error(code=400, message="工作流类型不匹配")
        if run.status != "waiting_answer":
            raise_error(code=400, message="当前工作流不可提交答案")

        state = dict(run.state_json)
        state["answers"] = answers
        state["run_id"] = run.run_id
        state["status"] = "grading"
        WorkflowRunService.update_run(self.db, run, "grading", state)
        if card_status:
            quiz = state.get("quiz") or {}
            self._update_learning_message(
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.run_id,
                request_text=str(state.get("request_text") or ""),
                response_text="正在按你的答案生成评分与点评，稍等一下。",
                response_payload={
                    "type": "learning_quiz",
                    "version": 1,
                    "run_id": run.run_id,
                    "status": card_status,
                    # 保留题目：刷新回来还能看到自己做的是哪套题（只是不能再交一次）
                    "quiz": quiz,
                },
            )
        return run

    def begin_submit(
        self, user_id: int, run_id: str, answers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """提交答案（**异步版前半段**，路由层用）：校验+落状态，立刻返回。"""
        run = self._prepare_submit(user_id, run_id, answers, card_status="grading")
        return {
            "run_id": run.run_id,
            "status": "grading",
            "state": dict(run.state_json),
        }

    def _grade_and_persist(
        self, *, run: Any, answers: list[dict[str, Any]], request_text: str
    ) -> dict[str, Any]:
        """跑评分图并把结果落成会话消息（同步/异步两条路共用的一段）。"""
        state = dict(run.state_json)
        try:
            final_state = RetryExecutor.run(
                state=state,
                node_name="evaluate_answer",
                operation=lambda: self._evaluate_answer_state(run.run_id, answers),
            )
        except Exception as exc:
            WorkflowRunService.mark_failed(self.db, run, state, "evaluate_answer", str(exc))
            raise_error(code=500, message=f"学习答案评分失败: {exc}")

        final_state["run_id"] = run.run_id
        # 第 27 轮起**不再把整段点评写进 AI 回复**（用户：评分/点评只在完成弹窗里看，
        # 之后去练习记录回看）。这里只留一句完成话术，完整结果在 payload/结果弹窗里。
        response_text = "本次练习已完成，点击上方按钮可查看评分结果与总评。"
        WorkflowRunService.update_run(self.db, run, "completed", final_state)
        # 第 27 轮起**保留结果载荷**（不再清空成一条纯文本 AI 回复）：把测评结果随消息落库，
        # 这样出题卡片随时能重新点开看评分/总评/反思，练习记录里也能回看 review/reflection。
        # status=completed 告诉前端"这张卡已经有结果"，直接展示结果视图而不是答题表单。
        result_payload = (
            {
                "type": "learning_quiz",
                "version": 1,
                "status": "completed",
                "run_id": run.run_id,
                "quiz": (final_state.get("quiz") or {}),
                "score": final_state.get("score"),
                "evaluation": (final_state.get("evaluation") or {}),
                "weaknesses": (final_state.get("weaknesses") or []),
                "review": final_state.get("review") or "",
                "reflection": (final_state.get("reflection") or {}),
                # 提交的作答：刷新页面后能恢复"我的答案"（与 GET /run 的 state.answers 一致）
                "answers": (final_state.get("answers") or []),
            }
            if final_state.get("status") == "completed"
            else None
        )
        self._update_learning_message(
            user_id=run.user_id,
            session_id=run.session_id,
            run_id=run.run_id,
            request_text=request_text,
            response_text=response_text,
            response_payload=result_payload,
        )
        return final_state

    def grade_for_run(
        self, *, user_id: int, run_id: str, answers: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """异步路径的**后半段**：评分 + 原地把"正在评分"换成点评，并触发 A2A 接力。

        出错时除 `mark_failed` 还要把消息改成失败说明（否则用户看到一张永远"正在评分"的卡）。
        A2A 接力放在这里（而不是路由）：它要的是**评分后**的 state，异步化后那个 state
        只在后台存在；在后台任务里同步跑下游 Agent 与原来的
        `_dispatch_a2a_handoff_task`（自带会话的后台任务）是同一件事。
        """
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        request_text = str((run.state_json or {}).get("request_text") or "")
        try:
            final_state = self._grade_and_persist(
                run=run, answers=answers, request_text=request_text
            )
        except Exception as exc:  # noqa: BLE001 - 后台任务不能往外抛（抛了没人接）
            self._fail_grade_message(run, request_text=request_text, reason=str(exc))
            raise
        try:
            from app.services.orchestrator_service import OrchestratorService

            final_state["handoff_result"] = OrchestratorService(self.db).dispatch_a2a_handoff(
                final_state, user_id
            )
        except Exception as exc:  # noqa: BLE001 - 接力失败不能反过来把评分算失败
            print(f"[Learning][评分] A2A 接力失败：run_id={run.run_id}, error={exc}")
        return final_state

    @staticmethod
    def grade_for_run_in_background(user_id: int, run_id: str, answers: list[dict[str, Any]]) -> None:
        """FastAPI `BackgroundTasks` 入口：自带 DB 会话，异常只记日志。"""
        from app.db.database import SessionLocal

        db = SessionLocal()
        try:
            LearningWorkflowService(db).grade_for_run(
                user_id=user_id, run_id=run_id, answers=answers
            )
        except Exception as exc:  # noqa: BLE001 - 后台失败只记日志
            print(f"[Learning][评分] 后台评分失败：run_id={run_id}, error={exc}")
        finally:
            db.close()

    def _fail_grade_message(self, run: Any, *, request_text: str, reason: str) -> None:
        """原地把"正在评分…"改成失败说明（尽力而为：写不进去也不能再抛）。"""
        try:
            self._update_learning_message(
                user_id=run.user_id,
                session_id=run.session_id,
                run_id=run.run_id,
                request_text=request_text,
                response_text=f"评分失败：{reason}。可以再说一次「出题」重新出一套再练。",
                response_payload={
                    "type": "learning_quiz",
                    "version": 1,
                    "run_id": run.run_id,
                    "status": "failed",
                    "quiz": (dict(run.state_json or {}).get("quiz") or {}),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 二次失败只记日志
            print(f"[Learning][评分] 失败说明写入会话失败：run_id={run.run_id}, error={exc}")

    def get_run(self, user_id: int, run_id: str) -> dict[str, Any]:
        """获取工作流当前状态，用于前端刷新恢复。"""
        run = WorkflowRunService.get_run_or_404(self.db, user_id, run_id)
        state = dict(run.state_json)
        state["run_id"] = run.run_id
        state["status"] = run.status
        return state

    def _generate_quiz_state(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        """执行出题图并返回最终状态。"""
        from app.agent.workflows.learning import build_learning_graph

        graph = build_learning_graph(self.llm, checkpointer=get_workflow_checkpointer(), db=self.db)
        config = build_thread_config(run_id)
        graph.invoke(state, config=config)
        return self._plain_state(graph.get_state(config).values)

    # 教学保留：旧版在学习服务内解析 A2A 信封并计算 next_agent。
    # 该职责已迁移到 OrchestratorService.dispatch_a2a_handoff(...)
    # 和 A2AHandoffService.dispatch(...)，避免学习服务承担跨 Agent 调度职责。
    #
    # def _resolve_next_agent(self, state: dict[str, Any]) -> str | None:
    #     """根据 A2A 信封计算下一个专家 Agent。"""
    #     envelope_data = state.get("a2a_envelope") or {}
    #     if not envelope_data:
    #         return None
    #     from app.agent.a2a import A2AEnvelope, decide_next_agent
    #
    #     try:
    #         return decide_next_agent(A2AEnvelope(**envelope_data))
    #     except Exception:
    #         return None

    def _evaluate_answer_state(self, run_id: str, answers: list[dict[str, Any]]) -> dict[str, Any]:
        """执行评分图并返回最终状态。"""
        from langgraph.types import Command

        from app.agent.workflows.learning import build_learning_graph

        graph = build_learning_graph(self.llm, checkpointer=get_workflow_checkpointer(), db=self.db)
        config = build_thread_config(run_id)
        final_state = graph.invoke(Command(resume=answers), config=config)
        return self._plain_state(final_state)
