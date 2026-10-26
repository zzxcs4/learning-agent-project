"""工作流运行状态持久化服务。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.db.models import WorkflowRun


class WorkflowRunService:
    """封装工作流运行记录的创建、读取和状态更新。"""

    @staticmethod
    def create_run(
        db: Session,
        user_id: int,
        session_id: int,
        workflow_type: str,
        status: str,
        state: dict[str, Any],
        run_id: str | None = None,
    ) -> WorkflowRun:
        """创建一条工作流运行记录。"""
        run = WorkflowRun(
            run_id=run_id or uuid.uuid4().hex,
            user_id=user_id,
            session_id=session_id,
            workflow_type=workflow_type,
            status=status,
            state_json=state,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def get_run_or_404(db: Session, user_id: int, run_id: str) -> WorkflowRun:
        """按 run_id 获取当前用户的工作流记录。"""
        stmt = select(WorkflowRun).where(
            WorkflowRun.run_id == run_id,
            WorkflowRun.user_id == user_id,
        )
        run = db.scalar(stmt)
        if not run:
            raise_error(code=404, message="工作流记录不存在")
        return run

    @staticmethod
    def update_run(
        db: Session,
        run: WorkflowRun,
        status: str,
        state: dict[str, Any],
        error_message: str | None = None,
    ) -> WorkflowRun:
        """更新工作流状态。"""
        run.status = status
        run.state_json = state
        run.error_message = error_message
        if status == "completed" and run.completed_at is None:
            run.completed_at = int(time.time())
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    @staticmethod
    def mark_failed(
        db: Session,
        run: WorkflowRun,
        state: dict[str, Any],
        failed_node: str,
        error_message: str,
    ) -> WorkflowRun:
        """记录工作流最终失败状态。"""
        failed_state = dict(state)
        failed_state["status"] = "failed"
        failed_state["failed_node"] = failed_node
        failed_state["last_error"] = error_message
        return WorkflowRunService.update_run(db, run, "failed", failed_state, error_message)
