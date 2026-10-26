"""学习画像路由：统计 / 按知识库的掌握度 / 弱点清单。

画像页展示的三样东西都是**真数据**：练习次数与分数来自 `practice_sets`，
掌握度来自 `user_knowledge_mastery`，弱点来自 `user_profiles.weak_points`。
**不提供"知识覆盖率"**（2026-10-17 决定，理由见 `profile_service` 模块注释）。
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.schemas import (
    MasteryItem,
    MasteryListResponse,
    ProfileStatsResponse,
    ReviewItem,
    ReviewListResponse,
    WeakPointItem,
    WeakPointListResponse,
)
from app.services.profile_service import RECENT_SCORE_LIMIT, ProfileService
from app.services.review_service import ReviewService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get(
    "/stats",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="画像页统计（练习次数 / 平均分 / 最近一次 / 趋势）",
)
def get_profile_stats(
    db: DBSession,
    current_user: CurrentUser,
    recent: Annotated[
        int, Query(ge=1, le=50, description="趋势取最近多少场，默认 10")
    ] = RECENT_SCORE_LIMIT,
) -> StandardResponse:
    """返回总练习次数、已完成场次、平均分、最近一场与最近 N 场分数。"""
    data = ProfileStatsResponse(
        **ProfileService.stats(db, user_id=current_user.id, recent=recent)
    )
    return StandardResponse(code=0, message="画像统计获取成功", data=data)


@router.get(
    "/mastery",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="按知识库的掌握度与分区",
)
def get_profile_mastery(db: DBSession, current_user: CurrentUser) -> StandardResponse:
    """返回当前用户在每个知识库上的掌握度、分区与已练场次（按掌握度降序）。"""
    items = ProfileService.list_mastery(db, user_id=current_user.id)
    data = MasteryListResponse(items=[MasteryItem(**item) for item in items])
    return StandardResponse(code=0, message="掌握度获取成功", data=data)


@router.get(
    "/weak-points",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="弱点清单（活跃 / 已改进 / 已归档）",
)
def get_profile_weak_points(
    db: DBSession,
    current_user: CurrentUser,
    knowledge_base_id: Annotated[
        int | None, Query(gt=0, description="按知识库过滤（含未归属任何库的通用弱点）")
    ] = None,
) -> StandardResponse:
    """返回弱点清单与三类计数：活跃在前（按出现次数降序），已改进/已归档排后面。"""
    result = ProfileService.list_weak_points(
        db, user_id=current_user.id, knowledge_base_id=knowledge_base_id
    )
    data = WeakPointListResponse(
        total=result["total"],
        active=result["active"],
        improved=result["improved"],
        archived=result["archived"],
        items=[WeakPointItem(**item) for item in result["items"]],
    )
    return StandardResponse(code=0, message="弱点清单获取成功", data=data)


@router.get(
    "/reviews/due",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="今日到期复习（SM-2 调度）",
)
def get_due_reviews(db: DBSession, current_user: CurrentUser) -> StandardResponse:
    """返回今天该复习的弱点：活跃、已排程且 `next_review <= 今天`，按应复习日期升序。"""
    result = ReviewService.due_reviews(db, user_id=current_user.id)
    data = ReviewListResponse(
        total=result["total"],
        today=result["today"],
        items=[ReviewItem(**item) for item in result["items"]],
    )
    return StandardResponse(code=0, message="今日到期复习获取成功", data=data)
