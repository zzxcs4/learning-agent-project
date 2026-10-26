"""学习工作流接口模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LearningWorkflowStartRequest(BaseModel):
    """启动学习测评请求。

    第 23 轮起：这几个参数**不传就是"没定"**，系统会在对话里出一张参数卡先问用户
    （`status=awaiting_params`）；传了就不问（"只问缺的"）。所以默认值全是 `None`，
    真正的默认值（题数 10 / 发散度 3 / 难度按画像）在 `quiz_params_service` 里。
    """

    session_id: int = Field(gt=0, description="学习会话ID")
    request_text: str = Field(min_length=1, max_length=2000, description="用户出题要求")
    question_count: int | None = Field(
        default=None, ge=5, le=20, description="每轮题目数 5–20；不传则先问用户（默认 10）")
    knowledge_base_id: int | None = Field(
        default=None,
        ge=0,
        description="知识库ID；不传则先问用户（唯一库自动使用）；0=明确不使用知识库",
    )
    context_mode: str = Field(
        default="outline",
        max_length=32,
        description="出题上下文模式：outline=只给考纲（默认，省 token、题更聚焦）；"
        "outline_with_source=考纲+本章原文（题贴资料、可追溯，私有材料必用）；"
        "source_only=只给原文（资料本身是清单/速查表、考纲没意义时用）",
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


class LearningWorkflowParamsRequest(BaseModel):
    """参数卡提交请求（对话中先问再出题）。"""

    question_count: int | None = Field(default=None, ge=5, le=20, description="每轮题目数 5–20")
    knowledge_base_id: int | None = Field(
        default=None, ge=0, description="知识库ID；0=不使用知识库，空=回落默认"
    )
    divergence: int | None = Field(default=None, ge=1, le=5, description="发散度 1–5")
    difficulty: str | None = Field(default=None, max_length=16, description="难度：入门/进阶/高级")


class LearningAnswerItem(BaseModel):
    """单题答案。"""

    question_id: str = Field(min_length=1, max_length=64)
    value: Any


class LearningWorkflowSubmitRequest(BaseModel):
    """提交学习测评答案请求。"""

    answers: list[LearningAnswerItem] = Field(min_length=1, description="整套题答案")


class LearningWorkflowStartResponse(BaseModel):
    """启动学习测评响应。

    `status=awaiting_params` 时 `quiz` 为空，`params` 是参数卡规格（前端据此渲染控件）；
    用户提交后走 `/learning-workflows/{run_id}/params` 继续出题。
    """

    run_id: str
    status: str
    quiz: dict[str, Any]
    params: dict[str, Any] = Field(
        default_factory=dict, description="参数卡规格：awaiting_params 时用于对话中收集出题参数")


class LearningWorkflowSubmitResponse(BaseModel):
    """提交学习测评响应。"""

    run_id: str
    status: str
    score: int
    evaluation: dict[str, Any]
    weaknesses: list[str]
    review: str
    reflection: dict[str, Any] = Field(
        default_factory=dict,
        description="评测后反思（升级规划 §7.2）：本轮出题质量自评结论；自评跳过时 overall=skipped",
    )
    next_agent: str | None = Field(default=None, description="A2A 调度器建议的下一个专家 Agent")
    handoff_result: dict[str, Any] = Field(default_factory=dict, description="A2A 统一派发结果")


class LearningWorkflowDetailResponse(BaseModel):
    """学习工作流详情响应。"""

    run_id: str
    status: str
    state: dict[str, Any]
