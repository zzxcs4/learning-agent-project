"""提示词模板版本管理的API路由：仅包含查看、获取和回滚功能。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core import StandardResponse
from app.core.logging_config import main_logger
from app.db.database import get_db
from app.db.models import PromptTemplate
from app.schemas import (
    PromptTemplateResponse,
    PromptVersionListResponse,
    RollbackPromptRequest,
)
from app.services.prompt_template_service import (
    AGENT_CONFIG,
    prompt_template_manager,
)

router = APIRouter(tags=["prompt_management"])


@router.get("/templates/{agent_name}/{scene}", response_model=StandardResponse, summary="获取模板版本列表")
async def get_template_versions(
    agent_name: str,
    scene: str,
    db: Session = Depends(get_db),
):
    """获取某个智能体特定场景下的所有模板版本。"""
    try:
        templates = (
            db.query(PromptTemplate)
            .filter(
                PromptTemplate.agent_name == agent_name,
                PromptTemplate.scene == scene,
            )
            .order_by(PromptTemplate.version.desc())
            .all()
        )

        if not templates:
            return StandardResponse(
                code=0,
                message="未找到模板",
                data=None,
            )

        response = PromptVersionListResponse(
            agent_name=agent_name,
            scene=scene,
            versions=[PromptTemplateResponse.model_validate(t) for t in templates],
        )
        return StandardResponse(code=0, message="查询成功", data=response)
    except Exception as e:
        main_logger.error(f"查询模板失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"查询模板失败: {str(e)}",
        )


@router.post("/rollback", response_model=StandardResponse, summary="回滚提示词版本")
async def rollback_version(
    req: RollbackPromptRequest,
    db: Session = Depends(get_db),
):
    """一键回滚到指定版本。"""
    try:
        success = prompt_template_manager.rollback_prompt_version(
            db=db, template_id=req.template_id
        )
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="模板不存在",
            )
        return StandardResponse(code=0, message="回滚成功")
    except Exception as e:
        main_logger.error(f"回滚模板失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"回滚模板失败: {str(e)}",
        )


@router.get("/config/agents", response_model=StandardResponse, summary="获取智能体配置")
async def get_agent_config():
    """获取所有智能体的配置信息。"""
    return StandardResponse(code=0, message="查询成功", data=AGENT_CONFIG)
