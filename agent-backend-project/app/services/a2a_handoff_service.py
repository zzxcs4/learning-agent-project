"""A2A 统一派发服务：负责把合法信封交给下一个专家 Agent。"""

from __future__ import annotations

from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.a2a import A2AEnvelope, decide_next_agent


HandoffHandler = Callable[[Session, A2AEnvelope, dict[str, Any]], dict[str, Any]]
NOTE_ARCHIVER_AGENT = "note_archiver"
# 学习测评工作流类型：归档"某一场练习"要靠它过滤 workflow_runs
WORKFLOW_TYPE_LEARNING_QUIZ = "learning_quiz"


class A2AHandoffService:
    """统一 A2A 接力入口，避免专家服务之间直接互调。"""

    _handlers: dict[str, HandoffHandler] = {}

    def __init__(self, db: Session):
        self.db = db

    @classmethod
    def register_handler(cls, agent_name: str, handler: HandoffHandler) -> None:
        """注册下游 Agent 处理器，后续接入新智能体时只扩展这里。"""
        cls._handlers[agent_name] = handler

    def dispatch(
        self,
        envelope_data: dict[str, Any] | None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """校验 A2A 信封并派发；目标 Agent 未接入时返回 pending。"""
        if not envelope_data:
            return self._result("skipped", None, "没有 A2A 信封")

        try:
            envelope = A2AEnvelope(**envelope_data)
        except Exception as exc:
            return self._result("blocked", None, f"A2A 信封格式非法: {exc}")

        next_agent = decide_next_agent(envelope)
        if not next_agent:
            status = "skipped" if not envelope.next_agent else "blocked"
            return self._result(status, envelope.next_agent, "未通过 A2A 调度校验")

        handler = self._handlers.get(next_agent)
        if handler is None:
            return self._result("pending", next_agent, "目标 Agent handler 尚未实现")

        try:
            data = handler(self.db, envelope, context or {})
        except Exception as exc:
            # 前缀用**实际**下游名：这里原来写死成 `[A2A][note_archiver]`，于是评测 worker
            # 失败时日志也自称 note_archiver，排查自动归档时会被带偏。
            print(f"[A2A][{next_agent}] 执行失败：error={exc}")
            return self._result("failed", next_agent, f"目标 Agent 执行失败: {exc}")

        return self._result("dispatched", next_agent, "已派发下一个 Agent", data)

    @staticmethod
    def _result(
        status: str,
        next_agent: str | None,
        reason: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "next_agent": next_agent,
            "reason": reason,
        }
        if data is not None:
            result["data"] = data
        return result


def _handle_note_archiver(
    db: Session,
    envelope: A2AEnvelope,
    context: dict[str, Any],
) -> dict[str, Any]:
    """执行笔记归档 Agent，生成内容后幂等保存。

    **进程内唯一的 `note_archiver` handler**（注册见文件末尾）。这条唯一性是有意的：
    `app/agent/workers.py` 曾经把同一个键再注册成一个 `ctx["gateway"]._run_archive(...)`
    的 lambda，而评分后的**自动接力**（`OrchestratorService.dispatch_a2a_handoff`）传的
    context 里没有 `gateway`，于是自动归档必然 `KeyError: 'gateway'` → 派发返回 `failed`
    → 笔记静默不写。现在 worker 层不再注册这个键，两条入口共用本函数。

    定位"要归档哪一场"分三档，优先级从高到低：

    1. `context["state"]`：A2A 自动接力（评分刚结束）走这档——内存里就是那份带分数、
       弱点、点评与反思的 state，不必回库重读，也不必再查 run；
    2. `envelope.payload["run_id"]`：supervisor 委派时模型明确给了 run_id，
       可以针对**任意一场**（不止最近一次）归档；
    3. **本会话最近完成的一场学习测评**：模型漏给 run_id 时的兜底（老数据没有 run_id，
       或列表工具没带出来），而不是报错或让模型瞎猜。这档要 `session_id` 划范围，
       所以 `dispatch_a2a_handoff` 会把 state 里的 `session_id` 一并放进 context。
    """
    from app.services.note_archive_service import NoteArchiveService, NoteService

    user_id = context.get("user_id")
    if user_id is None:
        raise ValueError("缺少 user_id，无法生成学习笔记")

    state = context.get("state")
    if isinstance(state, dict) and state:
        print(f"[A2A][note_archiver] 开始归档：run_id={state.get('run_id')}, user_id={user_id}")
        archive = NoteArchiveService(db).archive_from_state(int(user_id), state)
        return NoteService(db).save_learning_quiz_note(
            user_id=int(user_id),
            session_id=int(state["session_id"]),
            source_run_id=str(state["run_id"]),
            archive=archive,
        )

    run_id = envelope.payload.get("run_id") if isinstance(envelope.payload, dict) else None
    run_id = str(run_id or "").strip()
    if not run_id:
        run_id = _latest_completed_learning_run(
            db,
            user_id=int(user_id),
            session_id=_optional_int(context.get("session_id")),
        )
    if not run_id:
        raise ValueError(
            "缺少 run_id，且本会话没有已完成的学习测评可归档："
            "请先用 list_practice_history 查一场练习再归档"
        )

    from app.services.workflow_run_service import WorkflowRunService

    print(f"[A2A][note_archiver] 通过 run_id 归档：run_id={run_id}, user_id={user_id}")
    run = WorkflowRunService.get_run_or_404(db, int(user_id), str(run_id))
    archive = NoteArchiveService(db).archive_from_run(int(user_id), run.run_id)
    return NoteService(db).save_learning_quiz_note(
        user_id=int(user_id),
        session_id=run.session_id,
        source_run_id=run.run_id,
        archive=archive,
    )


def _latest_completed_learning_run(db: Session, *, user_id: int, session_id: int | None) -> str:
    """本会话最近一场已完成的学习测评 run_id；没有则返回空串。

    归档"最近这次"的兜底定位：有明确 run_id 就用明确的（`list_practice_history`
    已经带 run_id），只有模型漏给时才用这个回退。`session_id` 为空时不加会话过滤
    （仍限定在同一用户内），避免因为拿不到会话就把范围放宽到"全库任意一场"。
    """
    from app.db.models import WorkflowRun

    stmt = (
        select(WorkflowRun.run_id)
        .where(
            WorkflowRun.workflow_type == WORKFLOW_TYPE_LEARNING_QUIZ,
            WorkflowRun.user_id == user_id,
            WorkflowRun.status == "completed",
        )
        .order_by(WorkflowRun.updated_at.desc(), WorkflowRun.run_id.desc())
        .limit(1)
    )
    if session_id:
        stmt = stmt.where(WorkflowRun.session_id == session_id)
    return str(db.scalar(stmt) or "")


def _optional_int(value: Any) -> int | None:
    """可空整型：空值或转换失败按 None 处理（会话过滤只在拿到有效 id 时才加）。"""
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


A2AHandoffService.register_handler(NOTE_ARCHIVER_AGENT, _handle_note_archiver)
