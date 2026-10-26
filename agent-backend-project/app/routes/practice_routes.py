"""练习历史路由：分页列表 + 单场详情（逐题作答与评分）。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import Attempt, PracticeSet, Question, User, WorkflowRun
from app.schemas import (
    AttemptItem,
    PracticeAnswerRequest,
    PracticeDetailResponse,
    PracticeHistoryItem,
    PracticeHistoryResponse,
)
from app.services.practice_answer_service import PracticeAnswerService
from app.services.practice_service import PracticeService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _to_history_item(practice_set: PracticeSet) -> PracticeHistoryItem:
    """把练习套题行转成列表项。"""
    return PracticeHistoryItem(
        id=practice_set.id,
        title=practice_set.title,
        knowledge_base_id=practice_set.knowledge_base_id,
        status=practice_set.status,
        total_score=(
            int(practice_set.total_score) if practice_set.total_score is not None else None
        ),
        question_count=int(practice_set.question_count or 0),
        run_id=practice_set.run_id,
        created_at=int(practice_set.created_at or 0),
    )


def _run_review_state(
    db: Session, practice_set: PracticeSet
) -> tuple[str | None, dict | None]:
    """按 practice_set.run_id 读取对应 run 的 review/reflection，供练习详情回看。

    详情只读，读不到（run 已删 / 老数据没有 run_id）就回退成空——不为此抛错。
    返回 (review, reflection)。
    """
    run_id = practice_set.run_id
    if not run_id:
        return None, None
    run = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run_id))
    if run is None or not run.state_json:
        return None, None
    state = run.state_json
    reflection = state.get("reflection") if isinstance(state.get("reflection"), dict) else None
    return str(state.get("review") or "") or None, reflection


def _to_detail(
    db: Session,
    practice_set: PracticeSet,
    questions: list[Question],
    attempts: dict[int, Attempt],
) -> PracticeDetailResponse:
    """套题 + 逐题作答 + 总评/反思拼成详情响应（详情查询与补答评分共用同一份组装）。"""
    review, reflection = _run_review_state(db, practice_set)
    items: list[AttemptItem] = []
    for question in questions:
        attempt = attempts.get(question.id)
        # 参考答案在评测时回写到 questions.answer，详情页要能回看
        answer_payload = question.answer if isinstance(question.answer, dict) else {}
        items.append(
            AttemptItem(
                question_id=question.id,
                question_code=question.question_code or "",
                question_type=question.question_type or "textarea",
                stem=question.stem or "",
                focus_area=question.focus_area,
                options=question.options if isinstance(question.options, list) else None,
                points=int(question.points or 0),
                user_answer=attempt.answer if attempt is not None else None,
                score=(
                    int(attempt.score)
                    if attempt is not None and attempt.score is not None
                    else None
                ),
                is_correct=attempt.is_correct if attempt is not None else None,
                correct_answer=(
                    str(answer_payload.get("correct_answer") or "").strip() or None
                ),
                analysis=attempt.analysis if attempt is not None else None,
            )
        )
    return PracticeDetailResponse(
        practice=_to_history_item(practice_set), attempts=items, review=review, reflection=reflection
    )


@router.get(
    "",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="分页获取练习历史",
)
def list_practice_history(
    db: DBSession,
    current_user: CurrentUser,
    page: Annotated[int, Query(ge=1, description="页码，从1开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量，1-100")] = 20,
    knowledge_base_id: Annotated[
        int | None, Query(gt=0, description="按知识库过滤，不传则返回全部")
    ] = None,
) -> StandardResponse:
    """分页查询当前用户的练习历史（最新在前）。"""
    practices, total = PracticeService.list_practice_history(
        db=db,
        user_id=current_user.id,
        knowledge_base_id=knowledge_base_id,
        page=page,
        page_size=page_size,
    )
    data = PracticeHistoryResponse(
        page=page,
        page_size=page_size,
        total=total,
        has_more=page * page_size < total,
        practices=[_to_history_item(item) for item in practices],
    )
    return StandardResponse(code=0, message="练习历史获取成功", data=data)


@router.get(
    "/{practice_set_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="获取单场练习详情（逐题作答与评分）",
)
def get_practice_detail(
    practice_set_id: Annotated[int, Path(gt=0, description="练习ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """返回套题信息 + 每道题的题干、我的答案、得分、点评。"""
    practice_set, questions, attempts = PracticeService.get_practice_detail(
        db=db, user_id=current_user.id, practice_set_id=practice_set_id
    )
    return StandardResponse(
        code=0,
        message="练习详情获取成功",
        data=_to_detail(db, practice_set, questions, attempts),
    )


@router.post(
    "/{practice_set_id}/answers",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="为练习里未作答的题目补答并重新评分",
)
def submit_practice_answers(
    practice_set_id: Annotated[int, Path(gt=0, description="练习ID")],
    payload: PracticeAnswerRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """补答本场练习里未作答的题目，重新评分后返回更新过的详情。"""
    practice_set, questions, attempts = PracticeAnswerService.submit_missing_answers(
        db=db,
        user_id=current_user.id,
        practice_set_id=practice_set_id,
        answers=[item.model_dump() for item in payload.answers],
    )
    return StandardResponse(
        code=0,
        message="补答已评分",
        data=_to_detail(db, practice_set, questions, attempts),
    )
