"""练习补答验收（可复跑，**不需要后端服务**，用假模型评分，只写真库）。

验证的是「未作答的题事后还能作答」这件事：

1. 从未评分的练习 → 补答 2 题 → **只**给这 2 题落 attempt，总分按 points 归一化重算，
   状态变 completed；
2. 已评分的练习 → 补答那道"0 分 + 空答案"的题 → **原地更新同一条 attempt**（行数不变），
   总分重算，掌握度不漏不重复计（practice_count 保持 1）；
3. 已作答的题不能补答（400）、别人的练习不能补答（404）。

假模型是有意的：本切片要钉的是**落库口径**（谁被评分、写到哪一行、总分怎么算），
不是模型评分质量；真实模型的端到端跑法见 verify_practice_loop.py。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_practice_answers.py
    python scripts/verify_practice_answers.py --cleanup     # 跑完删掉本次产生的数据
"""

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.exceptions import AppException
from app.db.database import SessionLocal
from app.db.models import (
    Attempt,
    KnowledgeBase,
    PracticeSet,
    Question,
    User,
    UserKnowledgeMastery,
    UserProfile,
)
from app.services.practice_answer_service import PracticeAnswerService
from app.services.practice_service import PracticeService

# GBK 控制台下 ✅/❌ 会抛 UnicodeEncodeError——"语法全过却 exit 1"（交接文档坑 47）
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

FAILED: list[str] = []
PROMPTS: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    """打印一条断言结果；失败时追加到 FAILED。"""
    print(("✅ " if ok else "❌ ") + label + (f"  —— {detail}" if detail else ""))
    if not ok:
        FAILED.append(label)


class FakeGradingLLM:
    """假评测模型：只回放给定 JSON，同时把提示词留下来给断言检查。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def invoke(self, prompt: str) -> SimpleNamespace:
        PROMPTS.append(str(prompt))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _questions() -> list[dict]:
    """3 道题：单选 40 + 简答 40 + 简答 20，合计 100 分。"""
    return [
        {
            "id": "q1",
            "type": "radio",
            "stem": "GIL 的核心作用是什么？",
            "options": [{"value": "A", "label": "保证同一时刻只有一个线程执行字节码"}],
            "points": 40,
            "difficulty": 3,
        },
        {"id": "q2", "type": "textarea", "stem": "说明 GIL 对 CPU 密集任务的影响", "points": 40},
        {"id": "q3", "type": "textarea", "stem": "如何规避 GIL？", "points": 20},
    ]


def _grade_payload(*items: dict) -> dict:
    return {"score": 0, "items": list(items), "weaknesses": []}


def main() -> int:
    parser = argparse.ArgumentParser(description="练习补答验收")
    parser.add_argument("--cleanup", action="store_true", help="跑完删除本次产生的数据")
    args = parser.parse_args()

    stamp = int(time.time())
    db = SessionLocal()
    user_id = knowledge_base_id = 0
    try:
        user = User(username=f"vans{stamp}", password="x")
        db.add(user)
        db.commit()
        db.refresh(user)
        user_id = user.id
        knowledge_base = KnowledgeBase(user_id=user_id, name=f"验收库_{stamp}")
        db.add(knowledge_base)
        db.commit()
        db.refresh(knowledge_base)
        knowledge_base_id = knowledge_base.id
        print(f"[0] 临时用户 user_id={user_id} / 知识库 knowledge_base_id={knowledge_base_id}")

        # ---------------- 场景 1：从未评分的练习，补答其中 2 题 ----------------
        practice = PracticeService.create_practice_set(
            db,
            user_id=user_id,
            knowledge_base_id=None,
            title="补答验收：未评分练习",
            questions=_questions(),
        )
        llm = FakeGradingLLM(
            _grade_payload(
                {"question_id": "q1", "score": 40, "is_correct": True,
                 "correct_answer": "A", "analysis": "答对：GIL 让字节码串行执行。"},
                {"question_id": "q2", "score": 20, "is_correct": False,
                 "correct_answer": "CPU 密集任务无法并行，应改用多进程。",
                 "analysis": "只说到影响，没说到多进程。"},
            )
        )
        updated, questions, attempts = PracticeAnswerService.submit_missing_answers(
            db,
            user_id=user_id,
            practice_set_id=practice.id,
            answers=[
                {"question_id": "q1", "value": "A"},
                {"question_id": "q2", "value": "会让 CPU 密集任务变慢"},
                {"question_id": "q3", "value": "  "},  # 空答案应被忽略
            ],
            llm=llm,
        )
        by_code = {question.question_code: question for question in questions}
        check("只给补答的 2 题落 attempt（第 3 题仍无记录）",
              len(attempts) == 2 and by_code["q3"].id not in attempts,
              f"attempts={len(attempts)}")
        check("q1 得分 40 / 判对",
              attempts.get(by_code["q1"].id) is not None
              and attempts[by_code["q1"].id].score == 40
              and attempts[by_code["q1"].id].is_correct is True)
        check("答案按 value 落库（不是整条 dict）",
              attempts[by_code["q1"].id].answer == {"value": "A"}
              and attempts[by_code["q2"].id].answer == {"value": "会让 CPU 密集任务变慢"})
        check("参考答案回写 questions.answer",
              (by_code["q1"].answer or {}).get("correct_answer") == "A")
        check("总分按 points 归一化：(40+20)/100 → 60.0",
              updated.total_score == 60.0, f"total_score={updated.total_score}")
        check("状态变 completed", updated.status == "completed", f"status={updated.status}")
        check("提示词只包含补答的题（q3 不进去）",
              '"id": "q2"' in PROMPTS[-1] and '"id": "q3"' not in PROMPTS[-1])
        check("未选知识库时掌握度按 skip 处理（不炸）", updated.knowledge_base_id is None)

        # ---------------- 场景 1b：已作答 / 未知题号不能补答 ----------------
        try:
            PracticeAnswerService.submit_missing_answers(
                db,
                user_id=user_id,
                practice_set_id=practice.id,
                answers=[{"question_id": "q1", "value": "B"}, {"question_id": "q9", "value": "x"}],
                llm=llm,
            )
            check("已作答的题不能补答（应抛 400）", False, "没有抛异常")
        except AppException as exc:
            check("已作答的题不能补答（400）", exc.code == 400, f"code={exc.code}")

        # ---------------- 场景 2：已评分的练习，补答那道空答案题 ----------------
        practice2 = PracticeService.create_practice_set(
            db,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            title="补答验收：已有评分（含未作答）",
            questions=_questions(),
            status="completed",
        )
        practice2.total_score = 60.0
        db.commit()
        question_rows = PracticeService.list_questions(db, practice_set_id=practice2.id)
        codes = {question.question_code: question for question in question_rows}
        seeded = {
            "q1": ({"value": "A"}, 40.0, True, "答对"),
            "q2": ({"value": ""}, 0.0, False, "未作答，参考答案：应改用多进程。"),
            "q3": ({"value": "用多进程"}, 20.0, True, "答对"),
        }
        for code, (answer, score, is_correct, analysis) in seeded.items():
            db.add(
                Attempt(
                    question_id=codes[code].id,
                    user_id=user_id,
                    answer=answer,
                    score=score,
                    is_correct=is_correct,
                    analysis=analysis,
                )
            )
        mastery = UserKnowledgeMastery(
            user_id=user_id, knowledge_base_id=knowledge_base_id,
            mastery_score=50.0, practice_count=1, remark="验收占位",
        )
        db.add(mastery)
        db.commit()
        q2_attempt_id = db.scalar(
            select(Attempt.id).where(Attempt.question_id == codes["q2"].id)
        )

        llm2 = FakeGradingLLM(
            _grade_payload(
                {"question_id": "q2", "score": 30, "is_correct": False,
                 "correct_answer": "CPU 密集任务改用多进程。",
                 "analysis": "补答后基本说到点，但没提进程池开销。"}
            )
        )
        updated2, questions2, attempts2 = PracticeAnswerService.submit_missing_answers(
            db,
            user_id=user_id,
            practice_set_id=practice2.id,
            answers=[{"question_id": "q2", "value": "改用多进程"}],
            llm=llm2,
        )
        check("同一题的 attempt 是原地更新（行数不变、id 不变）",
              len(attempts2) == 3
              and db.scalar(select(Attempt.id).where(Attempt.question_id == codes["q2"].id))
              == q2_attempt_id,
              f"attempts={len(attempts2)} new_id="
              f"{db.scalar(select(Attempt.id).where(Attempt.question_id == codes['q2'].id))}")
        check("补答后的分数 / 点评 / 答案都写进同一条记录",
              attempts2[codes["q2"].id].score == 30
              and attempts2[codes["q2"].id].answer == {"value": "改用多进程"}
              and "进程池开销" in (attempts2[codes["q2"].id].analysis or ""))
        check("总分重算为 (40+30+20)/100 → 90.0",
              updated2.total_score == 90.0, f"total_score={updated2.total_score}")
        db.refresh(mastery)
        check("已评分的练习补答不重复计场次（practice_count 保持 1）",
              mastery.practice_count == 1, f"practice_count={mastery.practice_count}")
        check("已评分的练习补答不动掌握度（mastery_score 保持 50）",
              abs(float(mastery.mastery_score) - 50.0) < 0.01,
              f"mastery_score={mastery.mastery_score}")

        # ---------------- 场景 3：别人的练习不能补答 ----------------
        stranger = User(username=f"vanso{stamp}", password="x")
        db.add(stranger)
        db.commit()
        db.refresh(stranger)
        try:
            PracticeAnswerService.submit_missing_answers(
                db,
                user_id=stranger.id,
                practice_set_id=practice2.id,
                answers=[{"question_id": "q1", "value": "A"}],
                llm=llm2,
            )
            check("别人的练习不能补答（应抛 404）", False, "没有抛异常")
        except AppException as exc:
            check("别人的练习不能补答（404）", exc.code == 404, f"code={exc.code}")

        if args.cleanup:
            _cleanup(db, user_ids=[user_id, stranger.id], knowledge_base_ids=[knowledge_base_id])
            print("[9] 已清理本次产生的数据")
    finally:
        db.close()

    print(f"\n练习补答验收：{'全部通过' if not FAILED else '失败 ' + str(len(FAILED)) + ' 条'}")
    for item in FAILED:
        print(f"  - {item}")
    return 1 if FAILED else 0


def _cleanup(db, *, user_ids: list[int], knowledge_base_ids: list[int]) -> None:
    """删除本次验收产生的行（顺序：attempts → questions → practice_sets → 掌握度 → 库 → 用户）。"""
    practice_ids = list(
        db.scalars(select(PracticeSet.id).where(PracticeSet.user_id.in_(user_ids)))
    )
    question_ids = (
        list(db.scalars(select(Question.id).where(Question.practice_set_id.in_(practice_ids))))
        if practice_ids
        else []
    )
    if question_ids:
        db.execute(delete(Attempt).where(Attempt.question_id.in_(question_ids)))
        db.execute(delete(Question).where(Question.id.in_(question_ids)))
    if practice_ids:
        db.execute(delete(PracticeSet).where(PracticeSet.id.in_(practice_ids)))
    db.execute(delete(UserKnowledgeMastery).where(UserKnowledgeMastery.user_id.in_(user_ids)))
    # 掌握度更新会顺带写 user_profiles（弱点清单），删用户前必须先清掉
    db.execute(delete(UserProfile).where(UserProfile.user_id.in_(user_ids)))
    if knowledge_base_ids:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(knowledge_base_ids)))
    db.execute(delete(User).where(User.id.in_(user_ids)))
    db.commit()


if __name__ == "__main__":
    sys.exit(main())
