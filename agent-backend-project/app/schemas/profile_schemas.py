"""学习画像与练习历史的接口模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .learning_workflow_schemas import LearningAnswerItem


class PracticeHistoryItem(BaseModel):
    """练习历史列表项（轻量，不带题目）。"""

    id: int = Field(description="练习ID")
    title: str = Field(description="练习标题")
    knowledge_base_id: int | None = Field(default=None, description="所属知识库ID，可为空")
    status: str = Field(description="状态：waiting_answer / completed")
    total_score: int | None = Field(default=None, description="总分，未完成为空")
    question_count: int = Field(description="题目数量")
    run_id: str | None = Field(default=None, description="关联的工作流 run_id（归档/取总评用）")
    created_at: int = Field(description="创建时间（Unix 秒）")


class PracticeHistoryResponse(BaseModel):
    """练习历史分页响应。"""

    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页数量")
    total: int = Field(description="总条数")
    has_more: bool = Field(description="是否还有下一页")
    practices: list[PracticeHistoryItem] = Field(default_factory=list, description="当前页练习")


class AttemptItem(BaseModel):
    """单题作答与评分明细。"""

    question_id: int = Field(description="题目ID")
    question_code: str = Field(description="模型题号（q1、q2…）")
    question_type: str = Field(description="题型：radio / checkbox / textarea")
    stem: str = Field(description="题干")
    focus_area: str | None = Field(default=None, description="出题模型标注的考点")
    options: list[dict[str, Any]] | None = Field(
        default=None, description="选项（单选/多选有值），回看时用于把答案 value 还原成选项文本"
    )
    points: int = Field(description="题目分值")
    user_answer: Any = Field(default=None, description="用户作答（原样返回）")
    score: int | None = Field(default=None, description="本题得分")
    is_correct: bool | None = Field(default=None, description="本题是否判定正确")
    correct_answer: str | None = Field(default=None, description="参考答案（由评测结果回写到 questions.answer）")
    analysis: str | None = Field(default=None, description="本题点评")


class PracticeDetailResponse(BaseModel):
    """练习详情响应。"""

    practice: PracticeHistoryItem = Field(description="练习基本信息")
    attempts: list[AttemptItem] = Field(default_factory=list, description="逐题作答与评分")
    review: str | None = Field(default=None, description="整体总评（来自对应 run 的 state，练习记录回看用）")
    reflection: dict[str, Any] | None = Field(default=None, description="自评反思结论（来自对应 run 的 state）")


class PracticeAnswerRequest(BaseModel):
    """练习补答请求：只提交本场练习里尚未作答的题目。"""

    answers: list[LearningAnswerItem] = Field(
        min_length=1, description="补答内容（question_id 为题号，如 q1）"
    )


class MasteryItem(BaseModel):
    """单个知识库的掌握度。"""

    knowledge_base_id: int = Field(description="知识库ID")
    knowledge_base_name: str | None = Field(default=None, description="知识库名称（库已删则空）")
    mastery_score: float = Field(description="掌握度 0-100（EMA 滚动结果）")
    zone: str = Field(description="分区：补课区 / 过渡区 / 优势区")
    practice_count: int = Field(description="参与过评分的练习场次")
    last_assessed_at: int | None = Field(default=None, description="最近一次评测时间（Unix 秒）")


class MasteryListResponse(BaseModel):
    """掌握度列表响应。"""

    items: list[MasteryItem] = Field(default_factory=list, description="按掌握度降序")


class WeakPointItem(BaseModel):
    """一条弱点记录。"""

    point: str = Field(description="弱点描述")
    knowledge_base_id: int | None = Field(default=None, description="所属知识库ID，可为空（通用弱点）")
    times_seen: int = Field(description="出现次数")
    absent_streak: int = Field(description="连续未复现的场次（复发即归零）")
    improved: bool = Field(description="是否已改进")
    archived: bool = Field(description="是否已归档（不再进出题提示词）")
    axis: str = Field(default="knowledge", description="轴：knowledge / performance")
    first_seen: str | None = Field(default=None, description="首次出现日期")
    last_seen: str | None = Field(default=None, description="最近出现日期")
    last_checked_at: str | None = Field(default=None, description="最近一次核对日期")


class WeakPointListResponse(BaseModel):
    """弱点清单响应（含统计，前端可据此画"已改进/已归档"分布）。"""

    total: int = Field(description="总条数")
    active: int = Field(description="活跃（未改进且未归档）")
    improved: int = Field(description="已改进")
    archived: int = Field(description="已归档")
    items: list[WeakPointItem] = Field(default_factory=list, description="活跃在前，按出现次数降序")


class ProfileStatsResponse(BaseModel):
    """画像页顶部统计。"""

    practice_count: int = Field(description="总练习次数（含未完成）")
    completed_count: int = Field(description="已完成评分的场次")
    average_score: float = Field(description="已完成场次的平均分")
    latest_score: int | None = Field(default=None, description="最近一场得分")
    latest_practice_at: int | None = Field(default=None, description="最近一场时间（Unix 秒）")
    recent_scores: list[int] = Field(default_factory=list, description="最近若干场分数（时间升序，画趋势用）")


class ReviewItem(BaseModel):
    """一条今日到期复习（SM-2 调度产出，见 review_service）。"""

    point: str = Field(description="弱点描述（复习主题）")
    knowledge_base_id: int | None = Field(default=None, description="所属知识库ID，可为空（通用弱点）")
    times_seen: int = Field(description="出现次数")
    ease_factor: float = Field(description="SM-2 难度因子（越高间隔增长越快）")
    interval_days: int = Field(description="当前复习间隔（天）")
    next_review: str = Field(description="应复习日期（ISO，<= 今天即到期）")
    last_seen: str | None = Field(default=None, description="最近出现日期")


class ReviewListResponse(BaseModel):
    """今日到期复习列表（画像页"今天该复习什么"）。"""

    total: int = Field(description="到期条数")
    today: str = Field(description="判定用的今天（ISO 日期）")
    items: list[ReviewItem] = Field(default_factory=list, description="按应复习日期升序，同日按出现次数降序")
