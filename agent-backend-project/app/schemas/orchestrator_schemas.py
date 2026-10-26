"""流程总控接口模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AgentTaskStartRequest(BaseModel):
    """流程总控启动请求。"""

    session_id: int = Field(gt=0, description="会话ID")
    session_model: int = Field(default=0, ge=0, le=2, description="会话模式：0学习 2笔记（1 原为面试，已停用）")
    request_text: str = Field(min_length=1, max_length=8000, description="用户输入")
    agent_name: str | None = Field(default=None, max_length=64, description="前端选择的智能体名称")
    select_model: int = Field(default=0, ge=0, le=5, description="模型选择标记")
    question_count: int | None = Field(
        default=None, ge=5, le=20, description="每轮题目数 5–20；不传则先问用户（默认 10）")
    knowledge_base_id: int | None = Field(
        default=None,
        ge=0,
        description="知识库ID（出题/练习落库分组 + loop 检索范围）；不传则先问用户（唯一库自动使用）；0=明确不使用知识库",
    )
    context_mode: str = Field(
        default="outline",
        max_length=32,
        description="出题上下文模式：outline=只给考纲（默认）；outline_with_source=考纲+本章原文；source_only=只给原文",
    )
    divergence: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="出题发散度 1–5（1=聚焦薄弱 … 5=全面探索）；不传则先问用户（默认 3 均衡）",
    )
    difficulty: str | None = Field(
        default=None,
        max_length=16,
        description="难度：入门/进阶/高级；不传则先问用户（默认取画像目标等级）",
    )
    provider: str = Field(default="deepseek", max_length=32, description="模型供应商")


class AgentTaskStartResponse(BaseModel):
    """流程总控启动响应。"""

    route_type: str = Field(description="路由类型：workflow/chat")
    target_agent: str | None = Field(default=None, description="实际命中的智能体")
    workflow_type: str | None = Field(default=None, description="工作流类型")
    run_id: str | None = Field(default=None, description="工作流运行ID")
    status: str = Field(description="当前状态")
    data: dict[str, Any] = Field(default_factory=dict, description="业务数据")
