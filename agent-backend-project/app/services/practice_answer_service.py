"""练习补答服务：让"未作答"的题目事后还能作答，而不是永远停在参考评分上。

为什么需要它：评测节点会给**每道题**都打分（包括用户没作答的题），于是那些题会带着
0 分 + 参考答案 + 点评落进 `attempts`；用户在练习记录里看到的是"参考评分"，却没法补答。
本服务对补交的答案重新评分，并把结果**合并回同一场练习**：

1. 只评补答的那几道题（一次 LLM 调用，提示词与在线评测链路完全同源）；
2. 已存在的 attempt 行**原地更新**——同一题不能有两条答题记录，否则聚合会重复计分；
3. 总分按全部题目的 `points` 归一化重算（未作答的题计 0 分，口径与掌握度一致）；
4. 这一场练习此前**从未评过分**时才更新掌握度与弱点（它这才真的第一次拿到分数）；
   已经评过分的场景不重复计场次、也不重复做弱点老化。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.logging_config import main_logger
from app.db.models import Attempt, PracticeSet, Question, WorkflowRun
from app.services.practice_service import PracticeService, _to_bool, _to_float

# 与 learning_workflow_service.LEARNING_WORKFLOW_TYPE 同值：这里只需要一个字符串做查询
# 过滤，直接 import 那个模块会把 checkpointer / retry 一整串依赖带进本模块。
LEARNING_WORKFLOW_TYPE = "learning_quiz"


class PracticeAnswerService:
    """练习补答：重新评分 + 合并回本场练习。"""

    @staticmethod
    def submit_missing_answers(
        db: Session,
        *,
        user_id: int,
        practice_set_id: int,
        answers: list[dict[str, Any]],
        llm: Any = None,
        provider: str = "deepseek",
    ) -> tuple[PracticeSet, list[Question], dict[int, Attempt]]:
        """对未作答的题目补答并重新评分，返回更新后的（练习套题, 题目, 逐题作答）。

        `answers` 形如 `[{"question_id": "q1", "value": ...}]`（question_id = 题号）。
        只接受"本场尚未作答"的题：已作答的题不会被覆盖（想改答案就是一次新的练习），
        **一条有效的补答都没有**时抛 400，而不是静默什么都没做。
        """
        practice_set, questions, attempts = PracticeService.get_practice_detail(
            db, user_id=user_id, practice_set_id=practice_set_id
        )
        pending = {
            question.question_code: question
            for question in questions
            if _is_unanswered(attempts.get(question.id))
        }
        submitted: list[tuple[Question, Any]] = []
        for item in answers or []:
            if not isinstance(item, dict):
                continue
            question = pending.get(str(item.get("question_id") or "").strip())
            value = item.get("value")
            if question is None or _is_empty_answer(value):
                continue
            submitted.append((question, value))
        if not submitted:
            raise_error(code=400, message="没有可补答的题目：只能提交本场练习里尚未作答的题")

        first_grading = not any(attempt.score is not None for attempt in attempts.values())
        data = _grade(
            db,
            practice_set=practice_set,
            submitted=submitted,
            user_id=user_id,
            llm=llm,
            provider=provider,
        )
        items = {
            str(item.get("question_id") or "").strip(): item
            for item in _as_list(data.get("items"))
            if isinstance(item, dict)
        }
        if not any(question.question_code in items for question, _ in submitted):
            raise_error(code=500, message="补答评分未返回可用的逐题结果，请稍后重试")
        weaknesses = [
            str(item).strip()
            for item in _as_list(data.get("weaknesses"))
            if str(item).strip()
        ]

        for question, value in submitted:
            item = items.get(question.question_code) or {}
            correct_answer = str(item.get("correct_answer") or "").strip()
            if correct_answer:
                # 与评测链路同口径：参考答案回写 questions.answer（出题阶段刻意不出答案）
                question.answer = {"correct_answer": correct_answer}
            attempt = attempts.get(question.id)
            if attempt is None:
                attempt = Attempt(question_id=question.id, user_id=user_id)
                db.add(attempt)
                attempts[question.id] = attempt
            attempt.answer = {"value": value}
            attempt.score = _to_float(item.get("score"))
            attempt.is_correct = _to_bool(item.get("is_correct"))
            attempt.analysis = str(item.get("analysis") or "").strip() or None

        practice_set.total_score = _normalized_score(questions, attempts)
        practice_set.status = "completed"
        # 先提交答题记录：掌握度更新（内部自己 commit）失败时不能把补答结果一起回滚掉
        db.commit()
        main_logger.info(
            f"练习 {practice_set_id} 补答 {len(submitted)} 题已落库，"
            f"总分重算为 {practice_set.total_score}"
        )

        if first_grading:
            _update_mastery(
                db,
                practice_set_id=practice_set_id,
                user_id=user_id,
                knowledge_base_id=practice_set.knowledge_base_id,
                weaknesses=weaknesses,
            )
        else:
            main_logger.info(
                f"练习 {practice_set_id} 此前已评过分：补答不重复计场次，掌握度/弱点不动"
            )
        return practice_set, questions, attempts


def _grade(
    db: Session,
    *,
    practice_set: PracticeSet,
    submitted: list[tuple[Question, Any]],
    user_id: int,
    llm: Any,
    provider: str,
) -> dict[str, Any]:
    """只把补答的题交给评测节点：提示词与在线评测用的是同一个构造器。"""
    from app.agent.workflows.common import invoke_json
    from app.agent.workflows.learning import _build_dynamic_evaluation_prompt
    from app.services.knowledge_context_service import KnowledgeContextService

    state: dict[str, Any] = {
        "quiz": {
            "title": practice_set.title,
            "questions": [_question_payload(question) for question, _ in submitted],
        },
        "answers": [
            {"question_id": question.question_code, "value": value}
            for question, value in submitted
        ],
        "user_id": user_id,
        "knowledge_base_id": practice_set.knowledge_base_id,
    }
    references = KnowledgeContextService.render_references(
        db,
        knowledge_base_id=practice_set.knowledge_base_id,
        sections=_original_context_sections(
            db, user_id=user_id, practice_set_id=practice_set.id
        ),
    )
    if references:
        main_logger.info(f"练习 {practice_set.id} 补答评分参考资料: {len(references)} 字符")
    else:
        main_logger.warning(f"练习 {practice_set.id} 补答评分没有参考资料：本次评分无资料依据")
    return invoke_json(
        llm, _build_dynamic_evaluation_prompt(state, db, references), provider=provider
    )


def _original_context_sections(
    db: Session, *, user_id: int, practice_set_id: int
) -> list[str] | None:
    """取这场练习**出题时**轮到的那几章：参考资料要用同一批原文。

    找不到 run 就返回 None——`render_references` 会退化为"按 asked_count 升序补几章"，
    总比完全没有资料依据好。JSON 提取依赖 MySQL（本机 5.7 支持）。
    """
    try:
        state = db.scalar(
            select(WorkflowRun.state_json)
            .where(
                WorkflowRun.user_id == user_id,
                WorkflowRun.workflow_type == LEARNING_WORKFLOW_TYPE,
                func.json_extract(WorkflowRun.state_json, "$.practice_set_id")
                == int(practice_set_id),
            )
            .order_by(WorkflowRun.id.desc())
            .limit(1)
        )
    except Exception as exc:  # noqa: BLE001 - 回查失败只是少一批参考资料
        main_logger.warning(f"回查练习 {practice_set_id} 的出题章节失败: {exc}")
        return None
    if not isinstance(state, dict):
        return None
    sections = state.get("context_sections")
    if not isinstance(sections, list) or not sections:
        return None
    return [str(item).strip() for item in sections if str(item).strip()]


def _update_mastery(
    db: Session,
    *,
    practice_set_id: int,
    user_id: int,
    knowledge_base_id: int | None,
    weaknesses: list[str],
) -> None:
    """补答让一场"从未评过分"的练习拿到了真实分数 → 正常回流掌握度与弱点。"""
    from app.services.mastery_service import MasteryService

    try:
        summary = MasteryService.update_after_practice(
            db,
            user_id=user_id,
            practice_set_id=practice_set_id,
            knowledge_base_id=knowledge_base_id,
            weaknesses=weaknesses,
        )
        main_logger.info(f"练习 {practice_set_id} 补答后掌握度/弱点更新: {summary}")
    except Exception as exc:  # noqa: BLE001 - 掌握度失败不能把已经落库的评分变成 500
        db.rollback()
        main_logger.warning(f"练习 {practice_set_id} 补答后掌握度更新失败（不影响评分）: {exc}")


def _question_payload(question: Question) -> dict[str, Any]:
    """题目 → 评测提示词认识的形状（与出题落库的 quiz.questions 同构）。"""
    return {
        "id": question.question_code,
        "type": question.question_type,
        "stem": question.stem or "",
        "options": question.options if isinstance(question.options, list) else [],
        "points": int(question.points or 0),
    }


def _normalized_score(
    questions: list[Question], attempts: dict[int, Attempt]
) -> float | None:
    """全场总分：`100 × Σ已得分 / Σpoints`（未作答的题计 0 分），与掌握度同口径。"""
    total_points = sum(float(question.points or 0) for question in questions)
    scored = [
        attempt
        for attempt in (attempts.get(question.id) for question in questions)
        if attempt is not None and attempt.score is not None
    ]
    if total_points > 0:
        return round(100.0 * sum(float(attempt.score or 0) for attempt in scored) / total_points, 1)
    # 兜底：模型没给 points（全为 0）时按判对比例算
    judged = [attempt for attempt in attempts.values() if attempt.is_correct is not None]
    if not judged:
        return None
    return round(100.0 * sum(1 for attempt in judged if attempt.is_correct) / len(judged), 1)


def _is_unanswered(attempt: Attempt | None) -> bool:
    """这道题是否还没有作答：没有答题记录，或记录里的答案值为空。"""
    if attempt is None:
        return True
    answer = attempt.answer if isinstance(attempt.answer, dict) else {}
    return _is_empty_answer(answer.get("value"))


def _is_empty_answer(value: Any) -> bool:
    """空答案判定：None / 空串 / 空数组都算未作答。"""
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    return str(value).strip() == ""


def _as_list(value: Any) -> list[Any]:
    """把模型可能返回的空值或非列表值收敛成列表（避免为一个 helper 引入 langchain）。"""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
