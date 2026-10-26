"""流程总控路由。"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.schemas import AgentTaskStartRequest, AgentTaskStartResponse
from app.services.orchestrator_service import OrchestratorService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


@router.post(
    "/start",
    response_model=None,
    responses=common_error_responses,
    summary="启动流程总控任务",
)
def start_agent_task(
    payload: AgentTaskStartRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> Any:
    """根据智能体名称和关键词选择工作流；未命中时直接复用通用聊天流。"""
    result = OrchestratorService(db).start_task(
        user_id=current_user.id,
        session_id=payload.session_id,
        session_model=payload.session_model,
        request_text=payload.request_text,
        select_model=payload.select_model,
        agent_name=payload.agent_name,
        question_count=payload.question_count,
        knowledge_base_id=payload.knowledge_base_id,
        context_mode=payload.context_mode,
        divergence=payload.divergence,
        difficulty=payload.difficulty,
        provider=payload.provider,
    )

    if not isinstance(result, dict):
        return result

    data = AgentTaskStartResponse(**result)
    return StandardResponse(code=0, message="流程总控路由成功", data=data)
