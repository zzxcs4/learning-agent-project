"""验收：`POST /practice/{id}/answers`（补答接口）的 HTTP 层端到端。

`verify_practice_answers.py` 钉的是补答的**落库口径**（只评补答的题、原地更新 attempt、
总分归一化、掌握度不重复计）。这一套补的是它覆盖不到的一层：

- 路由/鉴权是否真的接上（未带令牌要拦住、别人的练习要 404、空列表要 422）；
- 响应体是否就是前端 `PracticeHistory` 适配的那份 `PracticeDetail`
  （`practice.status/total_score` + `attempts[].question_type/options/points/correct_answer`）；
- 没有可补答的题时返回 400 而**不调用模型**（不能白花一次评分额度）。

全程离线：进程内把 `get_chat_model` 换成假评分模型（不联网、不花额度），可以随时重跑。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_practice_answers_http.py
    python scripts/verify_practice_answers_http.py --keep   # 保留临时数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import delete, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

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
from app.main import app
from app.security import hash_password
from app.services.practice_service import PracticeService

# GBK 控制台下 ✅/❌ 会抛 UnicodeEncodeError（交接文档坑 47）
sys.stdout.reconfigure(encoding="utf-8")

TEMP_PASSWORD = "verify_pass_123"
PROMPTS: list[str] = []
PASSED = 0
FAILED = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"  PASS {name} {extra}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {extra}")


class FakeGradingModel:
    """假评分模型：回放固定 JSON，并记下被调用了几次（用来证明 400 分支不花额度）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def invoke(self, prompt: str) -> SimpleNamespace:
        PROMPTS.append(str(prompt))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _questions() -> list[dict[str, Any]]:
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


GRADE_PAYLOAD = {
    "score": 0,
    "items": [
        {"question_id": "q1", "score": 40, "is_correct": True, "correct_answer": "A",
         "analysis": "答对：GIL 让字节码串行执行。"},
        {"question_id": "q2", "score": 20, "is_correct": False,
         "correct_answer": "CPU 密集任务改用多进程。", "analysis": "只说影响，没说多进程。"},
    ],
    "weaknesses": [],
}


def main() -> int:
    parser = argparse.ArgumentParser(description="补答接口的 HTTP 层验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vansh{suffix}"  # users.username 只有 20 字符
    other_name = f"vansho{suffix}"
    user_id = other_id = 0
    practice_id = other_practice_id = graded_practice_id = 0

    # 离线：`invoke_json` 里 `llm or get_chat_model(provider=...)` 是模型的唯一解析点
    import app.agent.workflows.common as workflow_common

    saved_get_chat_model = workflow_common.get_chat_model
    fake_model = FakeGradingModel(GRADE_PAYLOAD)
    workflow_common.get_chat_model = lambda provider="deepseek": fake_model  # type: ignore[assignment]

    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            user = User(username=username, password=hash_password(TEMP_PASSWORD))
            other = User(username=other_name, password=hash_password(TEMP_PASSWORD))
            db.add_all([user, other])
            db.commit()
            db.refresh(user)
            db.refresh(other)
            user_id, other_id = user.id, other.id

            practice = PracticeService.create_practice_set(
                db, user_id=user_id, knowledge_base_id=None,
                title="补答HTTP验收：未评分", questions=_questions(),
            )
            practice_id = practice.id
            other_practice = PracticeService.create_practice_set(
                db, user_id=other_id, knowledge_base_id=None,
                title="补答HTTP验收：别人的", questions=_questions(),
            )
            other_practice_id = other_practice.id

            # 已评过分的练习：q1 已作答，只有 q2 可补答
            graded = PracticeService.create_practice_set(
                db, user_id=user_id, knowledge_base_id=None,
                title="补答HTTP验收：已有评分", questions=_questions(), status="completed",
            )
            graded_practice_id = graded.id
            graded_questions = PracticeService.list_questions(db, practice_set_id=graded.id)
            first_question = next(row for row in graded_questions if row.question_code == "q1")
            db.add(
                Attempt(
                    question_id=first_question.id, user_id=user_id,
                    answer={"value": "A"}, score=40.0, is_correct=True, analysis="答对",
                )
            )
            db.commit()
            print(f"临时用户 id={user_id} / id={other_id}；练习 id={practice_id} / "
                  f"{other_practice_id} / {graded_practice_id}")

            # ---- 鉴权 ----
            no_token = client.post(
                f"/practice/{practice_id}/answers",
                json={"answers": [{"question_id": "q1", "value": "A"}]},
            )
            check("未带令牌被拦住", no_token.status_code in (401, 403),
                  f"status={no_token.status_code}")

            login = client.post(
                "/auth/login", json={"username": username, "password": TEMP_PASSWORD}
            )
            check("登录成功", login.status_code == 200, f"status={login.status_code}")
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

            # ---- 输入校验：空列表 422，别人的练习 404 ----
            empty = client.post(f"/practice/{practice_id}/answers", json={"answers": []},
                                headers=headers)
            check("空答案列表被 schema 拦住（422，不是 200 空跑）", empty.status_code == 422,
                  f"status={empty.status_code}")
            stranger = client.post(
                f"/practice/{other_practice_id}/answers",
                json={"answers": [{"question_id": "q1", "value": "A"}]},
                headers=headers,
            )
            check("别人的练习补答 → 404", stranger.status_code == 404,
                  f"status={stranger.status_code}")

            # ---- 没有可补答的题：400 且**不调模型** ----
            calls_before = len(PROMPTS)
            dup = client.post(
                f"/practice/{graded_practice_id}/answers",
                json={"answers": [{"question_id": "q1", "value": "B"}]},
                headers=headers,
            )
            check("已作答的题不能补答 → 400", dup.status_code == 400,
                  f"status={dup.status_code} body={dup.text[:120]}")
            check("这条 400 没有调用评分模型（不白花额度）", len(PROMPTS) == calls_before,
                  f"模型调用 {len(PROMPTS) - calls_before} 次")

            # ---- 正常补答：只对未作答的题评分，并合并进本场练习 ----
            ok = client.post(
                f"/practice/{practice_id}/answers",
                json={
                    "answers": [
                        {"question_id": "q1", "value": "A"},
                        {"question_id": "q2", "value": "会让 CPU 密集任务变慢"},
                        {"question_id": "q3", "value": "   "},  # 空答案：不算补答
                    ]
                },
                headers=headers,
            )
            check("补答接口返回 200", ok.status_code == 200,
                  f"status={ok.status_code} body={ok.text[:160]}")
            data = (ok.json() or {}).get("data") or {}
            practice_data = data.get("practice") or {}
            check("接口提示语说明了合并口径",
                  "补答" in str((ok.json() or {}).get("message") or ""),
                  str((ok.json() or {}).get("message")))
            check("状态变 completed", practice_data.get("status") == "completed",
                  str(practice_data.get("status")))
            check("总分按 points 归一化：(40+20)/100 → 60.0",
                  practice_data.get("total_score") == 60.0,
                  str(practice_data.get("total_score")))

            attempts = data.get("attempts") or []
            by_code = {row.get("question_code"): row for row in attempts}
            check("响应里逐题记录齐全（q1/q2/q3）", set(by_code) == {"q1", "q2", "q3"},
                  str(sorted(by_code)))
            check("补答的 q1/q2 有分数与点评",
                  by_code["q1"].get("score") == 40 and by_code["q2"].get("score") == 20
                  and bool(by_code["q2"].get("analysis")),
                  str({code: by_code[code].get("score") for code in ("q1", "q2")}))
            check("未作答的 q3 没有 attempt 分（补答只覆盖提交的题）",
                  by_code["q3"].get("score") is None
                  and by_code["q3"].get("user_answer") is None,
                  str(by_code["q3"]))
            check("前端适配需要的字段都在（题型/选项/分值/参考答案）",
                  by_code["q1"].get("question_type") == "radio"
                  and by_code["q1"].get("points") == 40
                  and by_code["q1"].get("options")
                  and by_code["q1"].get("correct_answer") == "A",
                  str({key: by_code["q1"].get(key) for key in
                       ("question_type", "points", "correct_answer")}))
            check("答案按 value 落库（前端不必再解一层 {value:..}）",
                  by_code["q2"].get("user_answer") == {"value": "会让 CPU 密集任务变慢"},
                  str(by_code["q2"].get("user_answer")))
            check("提示词只包含补答的题（q3 不进去）",
                  '"id": "q2"' in PROMPTS[-1] and '"id": "q3"' not in PROMPTS[-1])

            # ---- 补答后的详情接口与补答响应口径一致 ----
            detail = client.get(f"/practice/{practice_id}", headers=headers)
            detail_data = (detail.json() or {}).get("data") or {}
            check("重新取详情拿到同一个总分（前端刷新后不会跳回旧分）",
                  (detail_data.get("practice") or {}).get("total_score") == 60.0,
                  str((detail_data.get("practice") or {}).get("total_score")))

            if not args.keep:
                _cleanup(
                    db,
                    user_ids=[user_id, other_id],
                    practice_ids=[practice_id, other_practice_id, graded_practice_id],
                )
                print("[9] 已清理本次产生的数据")
    finally:
        workflow_common.get_chat_model = saved_get_chat_model  # type: ignore[assignment]
        db.close()

    print(f"\n补答接口 HTTP 验收：{PASSED} 通过 / {FAILED} 失败")
    return 1 if FAILED else 0


def _cleanup(db, *, user_ids: list[int], practice_ids: list[int]) -> None:
    """按外键顺序删掉本次验收的行。"""
    question_ids = list(
        db.scalars(select(Question.id).where(Question.practice_set_id.in_(practice_ids)))
    )
    if question_ids:
        db.execute(delete(Attempt).where(Attempt.question_id.in_(question_ids)))
        db.execute(delete(Question).where(Question.id.in_(question_ids)))
    db.execute(delete(PracticeSet).where(PracticeSet.id.in_(practice_ids)))
    db.execute(delete(UserKnowledgeMastery).where(UserKnowledgeMastery.user_id.in_(user_ids)))
    # 掌握度回流会顺带写 user_profiles（弱点清单），删用户前必须先清掉
    db.execute(delete(UserProfile).where(UserProfile.user_id.in_(user_ids)))
    kb_ids = list(db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.user_id.in_(user_ids))))
    if kb_ids:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
    db.execute(delete(User).where(User.id.in_(user_ids)))
    db.commit()


if __name__ == "__main__":
    sys.exit(main())
