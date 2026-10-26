"""验收：第 3 期反思（升级规划 §7.2）——评测后一次自评，判据双向，结论写入 trace。

零 token 消耗（自评调用走脚本化假模型）；[1] [4] 需要真 MySQL（临时数据自建自清）：

| 段 | 验的是什么 | 手段 |
|---|---|---|
| [1] | 输入组装（出题时注入的那批弱点 / 章节 / 题干 / 得分） | 真 MySQL 弱点 + 构造 state |
| [2] | 结论收敛（白名单 / overall 推导 / 无证据降级 unknown） | 纯函数 + 假模型 |
| [3] | 失败契约（模型报错、非 JSON、db 缺失 → skipped，绝不抛） | 假模型 |
| [4] | 评测节点集成（挂点位置、双向判据进提示词、结论回 state） | 调 node（假模型两次调用） |
| [5] | 响应契约（结论真的能到前端：`/submit` 带 reflection，老 run 不炸） | 假 service 直调路由函数 |

用法（在 agent-backend-project 目录下）：

    python scripts/verify_reflection.py
    python scripts/verify_reflection.py --keep     # 保留临时数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langchain_core.messages import AIMessage
from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.agent.workflows.learning import create_evaluate_and_review_node
from app.agent.workflows.state import LearningWorkflowState
from app.db.database import SessionLocal
from app.db.models import User, UserProfile
from app.security import hash_password
from app.services.mastery_service import MasteryService
from app.services.reflection_service import ReflectionService

TEMP_PASSWORD = "verify_reflection_123"
WEAK_POINT = "GIL 与多线程的关系"

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class ScriptedLLM:
    """按调用次数回放脚本响应的假模型（记录提示词，供"判据是否进提示词"断言）。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: Any) -> AIMessage:
        self.prompts.append(str(prompt))
        index = min(len(self.prompts) - 1, len(self.responses) - 1)
        return AIMessage(content=self.responses[index])


class RaisingLLM:
    """一调用就炸的假模型（验失败契约）。"""

    def invoke(self, prompt: Any) -> AIMessage:
        raise RuntimeError("relay 不可用")


def _reflection_json(**overrides: Any) -> str:
    payload: dict[str, Any] = {
        "weak_point_coverage": {"verdict": "missed", "covered": [], "missed": [WEAK_POINT]},
        "chapter_rotation": {"verdict": "diverse", "chapters": ["索引入门", "事务隔离"]},
        "duplication": {"verdict": "fresh", "duplicates": []},
        "difficulty": {"verdict": "appropriate", "evidence": "得分居中"},
        "suggestions": ["下一轮补一道 GIL 题"],
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _state(user_id: int, **overrides: Any) -> LearningWorkflowState:
    payload: dict[str, Any] = {
        "user_id": user_id,
        "session_id": 1,
        "request_text": "针对薄弱点出一套题",
        "knowledge_base_id": None,
        "context_sections": ["索引入门", "事务隔离"],
        "quiz": {
            "questions": [
                {
                    "id": "q1",
                    "code": "q1",
                    "type": "radio",
                    "stem": "GIL 会阻止多线程并行吗",
                    "options": [{"label": "A", "value": "会"}, {"label": "B", "value": "不会"}],
                    "answer": "A",
                    "points": 10,
                    "focus_area": "GIL",
                }
            ]
        },
        "answers": [],
        "practice_set_id": None,
    }
    payload.update(overrides)
    return LearningWorkflowState(**payload)


# ---------------------------------------------------------------------------
# [1] 输入组装（真 MySQL）
# ---------------------------------------------------------------------------
def verify_collect_inputs(db: Any, user_id: int) -> dict[str, Any]:
    print("\n[1] 自评输入组装（真 MySQL 弱点画像）")

    MasteryService.merge_weak_points(
        db,
        user_id=user_id,
        knowledge_base_id=None,
        points=[WEAK_POINT],
        practice_set_id=910101,
        practice_score=50.0,
    )
    db.commit()  # merge 自身不 commit（"一次 commit"契约属于 update_after_practice）

    inputs = ReflectionService.collect_inputs(
        db, state=_state(user_id), recent_stems=["旧题干 A", "旧题干 B"], score=72
    )
    check(
        "弱点清单来自归并前的画像（出题时注入的那批）",
        inputs["weak_points"] == [WEAK_POINT],
        str(inputs["weak_points"]),
    )
    check("章节取本轮出题上下文", inputs["chapters"] == ["索引入门", "事务隔离"], str(inputs["chapters"]))
    check(
        "题干与考点标注组装正确",
        inputs["questions"] == [{"stem": "GIL 会阻止多线程并行吗", "focus_area": "GIL"}],
        str(inputs["questions"]),
    )
    check("最近题干与得分透传", inputs["recent_stems"] == ["旧题干 A", "旧题干 B"] and inputs["score"] == 72)
    return inputs


# ---------------------------------------------------------------------------
# [2] 结论收敛（纯函数 + 假模型）
# ---------------------------------------------------------------------------
def verify_normalization(inputs: dict[str, Any]) -> None:
    print("\n[2] 结论收敛：白名单 / overall 推导 / 无证据降级")

    result = ReflectionService.reflect(ScriptedLLM([_reflection_json()]), inputs)
    check("合法结论原样保留（verdict 白名单内）", result["weak_point_coverage"]["verdict"] == "missed", str(result["weak_point_coverage"]))
    check(
        "overall 由判据推导：missed -> warn（模型自己说了不算）",
        result["overall"] == "warn",
        result["overall"],
    )
    check(
        "证据快照对得上输入",
        result["evidence"]
        == {
            "score": 72,
            "question_count": 1,
            "chapters": ["索引入门", "事务隔离"],
            "weak_point_count": 1,
            "recent_stem_count": 2,
        },
        str(result["evidence"]),
    )
    check("建议最多留 3 条", len(result["suggestions"]) == 1)

    bad = ReflectionService.reflect(
        ScriptedLLM(
            [
                _reflection_json(
                    weak_point_coverage={"verdict": "很好", "covered": "x", "missed": None},
                    chapter_rotation={"verdict": "narrow"},
                    difficulty={"verdict": "随便"},
                )
            ]
        ),
        inputs,
    )
    check("白名单外的 verdict 一律收成 unknown", bad["difficulty"]["verdict"] == "unknown", str(bad["difficulty"]))
    check(
        "单个坏判据就够 warn（narrow -> warn）",
        bad["overall"] == "warn",
        bad["overall"],
    )

    all_good = ReflectionService.reflect(
        ScriptedLLM(
            [
                _reflection_json(
                    weak_point_coverage={"verdict": "good", "covered": [WEAK_POINT], "missed": []},
                    chapter_rotation={"verdict": "diverse"},
                    duplication={"verdict": "fresh"},
                    difficulty={"verdict": "appropriate"},
                )
            ]
        ),
        inputs,
    )
    check("四条判据全好 -> pass", all_good["overall"] == "pass", all_good["overall"])

    no_weak = ReflectionService.reflect(
        ScriptedLLM([_reflection_json(weak_point_coverage={"verdict": "good", "covered": ["幻觉弱点"], "missed": []})]),
        {"weak_points": [], "chapters": ["索引入门"], "questions": [], "recent_stems": [], "score": 72},
    )
    check(
        "没有弱点就不谈覆盖（防幻觉：good 收成 unknown）",
        no_weak["weak_point_coverage"]["verdict"] == "unknown" and no_weak["weak_point_coverage"]["covered"] == [],
        str(no_weak["weak_point_coverage"]),
    )

    no_chapter = ReflectionService.reflect(
        ScriptedLLM([_reflection_json()]),
        {"weak_points": [WEAK_POINT], "chapters": [], "questions": [], "recent_stems": [], "score": None},
    )
    check(
        "无章节 / 无得分 -> 对应判据 unknown",
        no_chapter["chapter_rotation"]["verdict"] == "unknown"
        and no_chapter["difficulty"]["verdict"] == "unknown",
        f"{no_chapter['chapter_rotation']['verdict']} / {no_chapter['difficulty']['verdict']}",
    )

    empty = ReflectionService.reflect(
        ScriptedLLM([_reflection_json()]),
        {"weak_points": [], "chapters": [], "questions": [], "recent_stems": [], "score": None},
    )
    check(
        "四项全无证据 -> 全部 unknown，overall=unknown（不假装通过）",
        empty["overall"] == "unknown"
        and empty["duplication"]["verdict"] == "unknown"
        and empty["weak_point_coverage"]["verdict"] == "unknown",
        f"{empty['overall']} / dup={empty['duplication']['verdict']}",
    )


# ---------------------------------------------------------------------------
# [3] 失败契约
# ---------------------------------------------------------------------------
def verify_failure_contract(inputs: dict[str, Any]) -> None:
    print("\n[3] 失败契约：自评失败收敛成 skipped，绝不抛")

    broken = ReflectionService.reflect(RaisingLLM(), inputs)
    check(
        "模型报错 -> skipped 带原因（评测结果不受影响）",
        broken["overall"] == "skipped" and "RuntimeError" in broken["reason"],
        str(broken.get("reason"))[:60],
    )

    not_json = ReflectionService.reflect(ScriptedLLM(["这套题还不错，我觉得没问题"]), inputs)
    check("非 JSON 输出 -> skipped", not_json["overall"] == "skipped", str(not_json.get("reason"))[:60])

    no_db = ReflectionService.after_evaluate(
        None, state=_state(1), recent_stems=[], score=72, llm=ScriptedLLM([_reflection_json()])
    )
    check(
        "db 缺失 -> 弱点读不到、coverage 降级 unknown，仍能出结论（不抛）",
        no_db["overall"] == "pass" and no_db["weak_point_coverage"]["verdict"] == "unknown",
        f"{no_db['overall']} / {no_db['weak_point_coverage']['verdict']}",
    )


# ---------------------------------------------------------------------------
# [4] 评测节点集成
# ---------------------------------------------------------------------------
def verify_node(db: Any, user_id: int) -> None:
    print("\n[4] 评测节点集成（挂点位置 + 双向判据进提示词 + 结论回 state）")

    evaluation_json = json.dumps(
        {
            "score": 72,
            "items": [{"question_id": 1, "score": 0, "is_correct": False}],
            "weaknesses": [WEAK_POINT],
            "display_text": "本题概念还没吃透，建议回看原文。",
        },
        ensure_ascii=False,
    )
    llm = ScriptedLLM([evaluation_json, _reflection_json()])
    node = create_evaluate_and_review_node(llm=llm, db=db)
    updates = node(_state(user_id))

    check("评测结论不受反思影响", updates["evaluation"]["score"] == 72 and updates["score"] == 72)
    check(
        "反思结论写进 state（随 state_json 进 trace）",
        updates["reflection"]["overall"] == "warn"
        and updates["reflection"]["evidence"]["weak_point_count"] == 1,
        str(updates["reflection"]["overall"]),
    )
    check("节点共调用模型两次（评测 + 自评）", len(llm.prompts) == 2, str(len(llm.prompts)))

    prompt = llm.prompts[1] if len(llm.prompts) > 1 else ""
    check(
        "自评提示词带上了归并前的弱点清单（判覆盖的依据）",
        WEAK_POINT in prompt,
        "命中" if WEAK_POINT in prompt else prompt[:120],
    )
    check(
        "双向判据都进了提示词（聚焦组 + 发散组）",
        "聚焦组" in prompt and "发散组" in prompt and "得分 72" in prompt,
    )


# ---------------------------------------------------------------------------
# [5] 响应契约：反思结论能到前端
# ---------------------------------------------------------------------------
def verify_response_contract() -> None:
    print("\n[5] 响应契约：/submit 立即返回 + 轮询 /{run_id} 能拿到 reflection")

    from types import SimpleNamespace

    from fastapi import BackgroundTasks

    import app.routes.learning_workflow_routes as routes_module
    from app.schemas.learning_workflow_schemas import (
        LearningAnswerItem,
        LearningWorkflowSubmitRequest,
        LearningWorkflowSubmitResponse,
    )

    check(
        "submit 响应模型带 reflection 字段",
        "reflection" in LearningWorkflowSubmitResponse.model_fields,
    )
    check(
        "老 run（state 里没有 reflection）默认空 dict，不报错",
        LearningWorkflowSubmitResponse(
            run_id="r", status="completed", score=0, evaluation={}, weaknesses=[], review=""
        ).reflection
        == {},
    )

    reflection = {"overall": "warn", "weak_point_coverage": {"verdict": "missed", "missed": [WEAK_POINT]}}

    class _FakeLearningService:
        """第 25 轮起 `/submit` 是异步的：立即返回 `grading`，评分在后台跑。

        所以"reflection 能到前端"的路径变成**轮询 `GET /{run_id}`**（state 里带它），
        而不是 `/submit` 的响应体——这里两条都钉住。
        """

        def __init__(self, db: Any) -> None:
            pass

        def begin_submit(self, **kwargs: Any) -> dict[str, Any]:
            return {"run_id": "fake-run", "status": "grading", "state": {}}

        def get_run(self, user_id: int, run_id: str) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "completed",
                "score": 60,
                "evaluation": {},
                "weaknesses": [WEAK_POINT],
                "review": "点评",
                "reflection": reflection,
            }

        @staticmethod
        def grade_for_run_in_background(user_id: int, run_id: str, answers: list[Any]) -> None:
            """后台任务在验收里不真跑（`BackgroundTasks` 只是登记）。"""
            return None

    original_service = routes_module.LearningWorkflowService
    routes_module.LearningWorkflowService = _FakeLearningService  # type: ignore[assignment]
    try:
        response = routes_module.submit_learning_workflow_answer(
            run_id="fake-run",
            payload=LearningWorkflowSubmitRequest(answers=[LearningAnswerItem(question_id="q1", value="A")]),
            background_tasks=BackgroundTasks(),
            db=None,
            current_user=SimpleNamespace(id=1),
        )
        detail_response = routes_module.get_learning_workflow(
            run_id="fake-run",
            db=None,
            current_user=SimpleNamespace(id=1),
        )
    finally:
        routes_module.LearningWorkflowService = original_service  # type: ignore[assignment]

    submit_data = response.data
    submit_status = (
        submit_data.get("status") if isinstance(submit_data, dict) else getattr(submit_data, "status", None)
    )
    submit_score = (
        submit_data.get("score") if isinstance(submit_data, dict) else getattr(submit_data, "score", None)
    )
    check("提交后立即返回 grading（不等评分，避免前端超时）", submit_status == "grading", str(submit_status))
    check("立即返回时不会假装有分数（score=0）", submit_score == 0, str(submit_score))

    detail_data = detail_response.data
    detail_state = (
        detail_data.get("state") if isinstance(detail_data, dict) else getattr(detail_data, "state", None)
    )
    out = (detail_state or {}).get("reflection")
    check("轮询 /{run_id} 的 state 里带 reflection（前端展示依赖它）", out == reflection, str(out)[:90])


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="第 3 期反思验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vref{suffix}"
    user_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.commit()
        user_id = user.id
        print(f"临时数据：user={user_id}（{username}）")

        inputs = verify_collect_inputs(db, user_id)
        verify_normalization(inputs)
        verify_failure_contract(inputs)
        verify_node(db, user_id)
        verify_response_contract()

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
