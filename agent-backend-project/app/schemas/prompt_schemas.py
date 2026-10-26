"""提示词模板相关的请求/响应Schema。"""

from typing import Optional, List

from pydantic import BaseModel, Field


class PromptTemplateResponse(BaseModel):
    """提示词模板的响应。"""

    id: int
    agent_name: Optional[str]
    scene: str
    template_type: int
    name: str
    description: Optional[str]
    template_content: str
    variables: Optional[str]
    version: int
    is_active: int
    created_at: int

    class Config:
        from_attributes = True


class PromptVersionListResponse(BaseModel):
    """提示词版本列表的响应。"""

    agent_name: Optional[str]
    scene: str
    versions: List[PromptTemplateResponse]


class RollbackPromptRequest(BaseModel):
    """回滚提示词版本的请求。"""

    template_id: int = Field(..., description="要回滚到的模板ID")

