"""学习工作流状态定义。"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowStatus(StrEnum):
    """学习工作流状态。"""

    CREATED = "created"
    GENERATING_QUIZ = "generating_quiz"
    WAITING_ANSWER = "waiting_answer"
    GRADING = "grading"
    COMPLETED = "completed"
    FAILED = "failed"


class LearningWorkflowState(BaseModel):
    """学习测评状态模型，支持跨请求分阶段补齐。"""

    run_id: str | None = Field(default=None, description="流程唯一标识，保存后生成")
    user_id: int = Field(description="用户ID")
    session_id: int = Field(description="会话ID")
    request_text: str = Field(description="用户出题需求文本")
    provider: str = Field(default="deepseek", description="大模型渠道标识")
    question_count: int | None = Field(
        default=None,
        ge=5,
        le=20,
        description="每轮题目数 5–20；None=还没定（第 23 轮起先问用户，默认 10）")
    knowledge_base_id: int | None = Field(
        default=None, description="本轮练习所属知识库ID（练习落库分组用，可空）")
    context_mode: str = Field(
        default="outline",
        description="出题上下文模式：outline=只给考纲（默认）；outline_with_source=考纲+本章原文")
    context_sections: list[str] = Field(
        default_factory=list, description="本轮出题覆盖的考纲章节（评测据此取同一批原文）")
    divergence: int | None = Field(
        default=None,
        ge=1,
        le=5,
        description="出题发散度 1–5：1=聚焦薄弱、5=全面探索（防止只练弱点）；None=还没定，按 3 处理")
    difficulty: str | None = Field(
        default=None,
        description="本次出题难度：入门/进阶/高级；None=还没定（默认取画像目标等级，按进阶处理）")
    quiz: dict[str, Any] = Field(default_factory=dict, description="AI生成测评题目")
    practice_set_id: int | None = Field(
        default=None, description="出题落库后的 practice_sets.id，评测阶段据此回写答题记录")
    answers: list[dict[str, Any]] = Field(default_factory=list, description="用户提交答案")
    evaluation: dict[str, Any] = Field(default_factory=dict, description="AI批改明细")
    score: int = Field(default=0, description="测评总分")
    weaknesses: list[str] = Field(default_factory=list, description="知识薄弱点")
    review: str = Field(default="", description="测评总结与学习建议")
    reflection: dict[str, Any] = Field(
        default_factory=dict,
        description="评测后反思（升级规划 §7.2）：出题质量自评结论（双向判据），只评不改，随 state_json 进 trace")
    status: WorkflowStatus = Field(default=WorkflowStatus.CREATED, description="流程当前阶段")
    a2a_envelope: dict[str, Any] = Field(default_factory=dict, description="A2A 契约信封，供下游专家 Agent 消费")