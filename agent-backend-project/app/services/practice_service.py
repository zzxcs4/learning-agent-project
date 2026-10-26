"""练习记录服务：出题落库、作答落库、按知识库查练习历史。

第 1 期闭环的地基。原来题目只活在 `workflow_runs.state_json` 里、评分只活在
`chat_messages.response_payload` 里，所以"掌握度 / 趋势 / 统计"根本算不出来；
本服务负责把这两类数据实体化成可聚合的数据行：

- 出题成功 -> 1 条 `practice_sets` + N 条 `questions`
- 评测完成 -> 每题 1 条 `attempts`，并把总分 / 状态回写 `practice_sets`

写入方只此一处，路由与工作流节点都只做参数接入。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.db.models import Attempt, PracticeSet, Question

# 练习状态：等待作答 / 已完成
STATUS_WAITING_ANSWER = "waiting_answer"
STATUS_COMPLETED = "completed"


class PracticeService:
    """练习闭环落库与查询。"""

    @staticmethod
    def create_practice_set(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int | None,
        title: str,
        questions: list[dict[str, Any]],
        status: str = STATUS_WAITING_ANSWER,
        run_id: str | None = None,
    ) -> PracticeSet:
        """出题落库：写 1 条 practice_sets 与 N 条 questions，返回练习套题行。

        `questions` 是出题节点产出的题目 JSON 列表（含 id / type / stem / options / points）。
        `question_code` 存模型给的题号（q1、q2…），它是"用户作答 question_id"与"评分明细
        question_id"之间的唯一映射键，所以必须原样落库。
        `run_id` 关联对应的工作流 run（不设外键）：练习记录回看 review/reflection、
        以及归档工具定位靠它。
        """
        practice_set = PracticeSet(
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            run_id=run_id,
            title=_clip(title, 255) or "学习练习",
            status=status,
            question_count=len(questions),
        )
        db.add(practice_set)
        db.flush()  # 先取到 practice_sets.id，供下面逐题挂外键

        for index, item in enumerate(questions):
            db.add(
                Question(
                    practice_set_id=practice_set.id,
                    question_code=_question_code(item, index),
                    focus_area=_clip(item.get("focus_area"), 128),
                    question_type=_clip(item.get("type"), 16) or "textarea",
                    stem=str(item.get("stem") or ""),
                    options=item.get("options") or None,
                    answer=item.get("answer") or None,
                    points=_to_int(item.get("points")),
                    difficulty=_to_float(item.get("difficulty")),
                )
            )

        db.commit()
        db.refresh(practice_set)
        return practice_set

    @staticmethod
    def get_practice_set_or_404(db: Session, *, user_id: int, practice_set_id: int) -> PracticeSet:
        """按 user_id + practice_set_id 取练习套题，不存在则抛 404。"""
        row = db.scalar(
            select(PracticeSet).where(
                PracticeSet.id == practice_set_id,
                PracticeSet.user_id == user_id,
            )
        )
        if row is None:
            raise_error(code=404, message="练习记录不存在")
        return row

    @staticmethod
    def list_questions(db: Session, *, practice_set_id: int) -> list[Question]:
        """按落库顺序取回题目（id 升序 = 出题顺序）。"""
        stmt = (
            select(Question)
            .where(Question.practice_set_id == practice_set_id)
            .order_by(Question.id.asc())
        )
        return list(db.scalars(stmt))

    @staticmethod
    def get_practice_detail(
        db: Session, *, user_id: int, practice_set_id: int
    ) -> tuple[PracticeSet, list[Question], dict[int, Attempt]]:
        """练习详情：套题 + 题目 + 逐题作答（`question_id -> Attempt`）。

        作答用字典返回而不是列表：路由层要按题目顺序组装"题目 + 我的答案 + 得分"，
        字典能避免题目没答时的错位。
        """
        practice_set = PracticeService.get_practice_set_or_404(
            db, user_id=user_id, practice_set_id=practice_set_id
        )
        questions = PracticeService.list_questions(db, practice_set_id=practice_set_id)
        attempts: dict[int, Attempt] = {}
        if questions:
            rows = db.scalars(
                select(Attempt).where(
                    Attempt.question_id.in_([question.id for question in questions])
                )
            )
            attempts = {int(row.question_id): row for row in rows}
        return practice_set, questions, attempts

    @staticmethod
    def record_attempts(
        db: Session,
        *,
        user_id: int,
        practice_set_id: int,
        items: list[dict[str, Any]],
        answers: list[dict[str, Any]],
        score: float | None = None,
        status: str = STATUS_COMPLETED,
    ) -> int:
        """作答落库：逐题写 attempts，回写 questions.answer（参考答案），更新套题状态。

        返回实际写入的答题记录条数。评分明细里题号对不上的条目会被跳过而不是抛错——
        模型输出抖动只应少记一条，不该把整场练习的落库带崩。
        """
        practice_set = PracticeService.get_practice_set_or_404(
            db, user_id=user_id, practice_set_id=practice_set_id
        )
        question_by_code = {
            question.question_code: question
            for question in PracticeService.list_questions(db, practice_set_id=practice_set_id)
        }
        answer_by_code = {
            str(item.get("question_id") or "").strip(): item.get("value")
            for item in answers or []
            if isinstance(item, dict)
        }

        created = 0
        for item in items or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("question_id") or "").strip()
            question = question_by_code.get(code)
            if question is None:
                continue

            correct_answer = _clean(item.get("correct_answer"))
            if correct_answer:
                # 出题阶段刻意不出答案（避免泄题），参考答案由评测结果回写。
                question.answer = {"correct_answer": correct_answer}

            db.add(
                Attempt(
                    question_id=question.id,
                    user_id=user_id,
                    answer={"value": answer_by_code.get(code)},
                    score=_to_float(item.get("score")),
                    is_correct=_to_bool(item.get("is_correct")),
                    analysis=_clean(item.get("analysis")),
                )
            )
            created += 1

        practice_set.total_score = _to_float(score)
        practice_set.status = status
        db.commit()
        return created

    @staticmethod
    def list_practice_history(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[PracticeSet], int]:
        """按知识库查练习历史：knowledge_base_id 为空时返回该用户的全部练习。

        返回 (当前页练习, 总条数)，供路由层组装分页响应。
        """
        filters = [PracticeSet.user_id == user_id]
        if knowledge_base_id is not None:
            filters.append(PracticeSet.knowledge_base_id == knowledge_base_id)

        total = db.scalar(select(func.count(PracticeSet.id)).where(*filters)) or 0
        stmt = (
            select(PracticeSet)
            .where(*filters)
            .order_by(PracticeSet.created_at.desc(), PracticeSet.id.desc())
            .offset(max(page - 1, 0) * page_size)
            .limit(page_size)
        )
        return list(db.scalars(stmt)), total


def _question_code(item: dict[str, Any], index: int) -> str:
    """题号兜底：模型没给 id 时按序号补 q{index+1}。"""
    return (_clip(item.get("id"), 16) or f"q{index + 1}")


def _clip(value: Any, limit: int) -> str | None:
    """字符串列取值：去空白、截断到列宽，空值统一为 None。"""
    text = str(value).strip() if value is not None else ""
    return text[:limit] or None


def _clean(value: Any) -> str | None:
    """JSON / TEXT 列取值：只去空白，不截断。"""
    text = str(value).strip() if value is not None else ""
    return text or None


def _to_int(value: Any) -> int:
    """模型给的数值可能是字符串，转换失败按 0 处理。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _to_float(value: Any) -> float | None:
    """模型给的数值可能是字符串，转换失败按空处理。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    """兼容 true/false、"true"/"1"/"是" 等写法；判不出来时留空而不猜。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    lowered = str(value).strip().lower()
    if lowered in {"true", "1", "yes", "y", "是", "正确"}:
        return True
    if lowered in {"false", "0", "no", "n", "否", "错误"}:
        return False
    return None
