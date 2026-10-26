"""学习工作流路由。"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Path
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.schemas import (
    LearningWorkflowDetailResponse,
    LearningWorkflowParamsRequest,
    LearningWorkflowStartRequest,
    LearningWorkflowStartResponse,
    LearningWorkflowSubmitRequest,
    LearningWorkflowSubmitResponse,
)
from app.services.learning_workflow_service import LearningWorkflowService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post(
    "/start",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="启动学习测评工作流（教学保留）",
)
def start_learning_workflow(
    payload: LearningWorkflowStartRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """教学保留入口：前端主流程推荐通过 /agent/tasks/start 进入学习测评。

    第 23 轮起出题参数（知识库/题目数/发散度/难度）"不传就是没定"：此时**不出题**，
    而是返回 `status=awaiting_params` + 一张参数卡规格，用户确认后走 `/{run_id}/params`。
    """
    state = LearningWorkflowService(db).start(
        user_id=current_user.id,
        session_id=payload.session_id,
        request_text=payload.request_text,
        question_count=payload.question_count,
        knowledge_base_id=payload.knowledge_base_id,
        context_mode=payload.context_mode,
        divergence=payload.divergence,
        difficulty=payload.difficulty,
        provider=payload.provider,
    )
    data = LearningWorkflowStartResponse(
        run_id=state["run_id"],
        status=state["status"],
        quiz=state.get("quiz") or {},
        params=state.get("params") or {},
    )
    return StandardResponse(code=0, message="学习测评工作流启动成功", data=data)


@router.post(
    "/{run_id}/params",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="确认出题参数（对话中的参数卡）",
)
def configure_learning_workflow_params(
    run_id: Annotated[str, Path(min_length=1, max_length=64, description="工作流运行ID")],
    payload: LearningWorkflowParamsRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """参数卡提交：校验参数后**立即返回**，出题放后台跑。

    真模型出题要 30–60 秒，同步等会被前端 30 秒超时掐断——用户看到"报超时"，
    但服务端其实已经把题出好落库了（第 25 轮报的问题）。现在：运行推进到
    `generating_quiz`、参数卡消息就地变成"正在出题"，前端轮询运行状态，
    完成后消息原地换成题目。
    """
    prepared = LearningWorkflowService(db).begin_configure(
        user_id=current_user.id,
        run_id=run_id,
        question_count=payload.question_count,
        knowledge_base_id=payload.knowledge_base_id,
        divergence=payload.divergence,
        difficulty=payload.difficulty,
    )
    background_tasks.add_task(
        LearningWorkflowService.generate_for_run_in_background,
        current_user.id,
        run_id,
        prepared["state"],
    )
    data = LearningWorkflowStartResponse(
        run_id=prepared["run_id"],
        status=prepared["status"],
        quiz={},
        params={},
    )
    return StandardResponse(code=0, message="出题参数已确认，正在生成题目", data=data)


@router.post(
    "/{run_id}/submit",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="提交学习测评答案",
)
def submit_learning_workflow_answer(
    run_id: Annotated[str, Path(min_length=1, max_length=64, description="工作流运行ID")],
    payload: LearningWorkflowSubmitRequest,
    background_tasks: BackgroundTasks,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """提交整套题答案：校验后**立即返回**，评分与点评放后台跑。

    评分也是 LLM 调用（整套题+答案进提示词，20–40 秒），同步等会被前端 30 秒超时
    掐断——用户看到"报超时"，但分数其实已经落库（与出题那条同一类问题，第 25 轮）。
    现在：运行推进到 `grading`、题目消息就地变成"正在评分"，前端轮询运行状态，
    `completed` 后从 `state` 里取分数/逐题点评。A2A 接力放在后台评分之后
    （它要的是评分后的 state）。
    """
    answers = [item.model_dump() for item in payload.answers]
    prepared = LearningWorkflowService(db).begin_submit(
        user_id=current_user.id,
        run_id=run_id,
        answers=answers,
    )
    background_tasks.add_task(
        LearningWorkflowService.grade_for_run_in_background,
        current_user.id,
        run_id,
        answers,
    )
    data = LearningWorkflowSubmitResponse(
        run_id=prepared["run_id"],
        status=prepared["status"],
        score=0,
        evaluation={},
        weaknesses=[],
        review="正在生成评分与点评，请稍候。",
        reflection={},
        next_agent=None,
        # 评分还在后台跑，接力结果此刻未知：给空 dict（schema 是非可选 dict，不是 None）
        handoff_result={},
    )
    return StandardResponse(code=0, message="答案已提交，正在生成评分", data=data)


@router.get(
    "/{run_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="获取学习工作流状态",
)
def get_learning_workflow(
    run_id: Annotated[str, Path(min_length=1, max_length=64, description="工作流运行ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """按 run_id 恢复当前学习工作流状态。"""
    state = LearningWorkflowService(db).get_run(current_user.id, run_id)
    data = LearningWorkflowDetailResponse(
        run_id=state["run_id"],
        status=state["status"],
        state=state,
    )
    return StandardResponse(code=0, message="学习工作流状态获取成功", data=data)
