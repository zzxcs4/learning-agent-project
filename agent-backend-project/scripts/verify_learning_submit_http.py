"""验收：学习测评"出题 → 提交答案 → 后台评分"的 HTTP 层（第 25/26 轮异步化后的口径）。

复跑之前说明契约（本轮起有修正，第 27 轮）：

- `/submit` **立即返回** `status=grading`（评分是 LLM 调用，同步等会被前端 30s 超时掐断）；
  前端轮询 `GET /learning-workflows/{run_id}` 直到 `completed` 才展示分数/点评。
- 出题与评分全程走**脚本化假模型**（零 token），可随时重跑。
- **评分后消息卡片保留**（本轮修正）：`response_payload` 仍带着
  `{type:"learning_quiz", status:"completed", quiz}`，前端可以随时把结果弹窗重新打开，
  而不是把整个 review 摊成一条纯文本 AI 回复、关掉就无法再看。
- 练习记录（`practice_sets`）与 `workflow_runs` 通过新增的 `run_id` 关联，
  练习详情响应补 `review` / `reflection`，供"练习记录里回看总评价与反思"。
- A2A 接力在后台评分后触发：有薄弱点 → `note_archiver` 写一条「AI 归档」笔记。
  这里会先调一次 `register_worker_handlers()` 复现"进程内已跑过 agent loop"（GUI 里
  题目就是 loop 的 `delegate_quiz` 出的），因为"loop 注册 + 工作流自动派发"这个**交叉
  场景**才是自动归档真正出过问题的地方：worker 层曾把 `note_archiver` 覆盖成要求
  `ctx["gateway"]` 的版本，而自动接力的 context 里没有 gateway。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_learning_submit_http.py
    python scripts/verify_learning_submit_http.py --keep   # 保留临时数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import (
    Attempt,
    ChatMessage,
    KnowledgeBase,
    Note,
    PracticeSet,
    Question,
    Session as SessionModel,
    User,
    UserKnowledgeMastery,
    UserProfile,
    WorkflowRun,
)
from app.security import hash_password

# GBK 控制台下 ✅/❌ 会抛 UnicodeEncodeError（交接文档坑 47）
sys.stdout.reconfigure(encoding="utf-8")

TEMP_PASSWORD = "verify_pass_123"
PROMPTS: list[str] = []
passed = 0
failed = 0

# 反思结论用白名单判据（否则会收成 unknown，见 reflection_service）
REFLECTION_PAYLOAD = {
    "weak_point_coverage": {"verdict": "good", "covered": ["GIL 与多线程的关系"], "missed": []},
    "chapter_rotation": {"verdict": "diverse", "chapters": ["索引入门", "事务隔离"]},
    "duplication": {"verdict": "fresh", "duplicates": []},
    "difficulty": {"verdict": "appropriate", "evidence": "得分居中"},
    "suggestions": ["下一轮补一道并发原语题"],
}
WEAKNESSES = ["GIL 与多线程的关系", "事务隔离级别"]


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


class ScriptedModel:
    """按调用顺序回放 [出题, 评分+反思] 的假模型；多出的调用回放最后一条。"""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: Any) -> Any:
        from types import SimpleNamespace

        self.prompts.append(str(prompt))
        index = min(len(self.prompts) - 1, len(self.responses) - 1)
        return SimpleNamespace(content=self.responses[index])


def _quiz_json() -> str:
    return json.dumps(
        {
            "title": "HTTP验收：GIL 与并发",
            "description": "请完成以下题目。",
            "question_count": 5,
            "questions": [
                {
                    "id": "q1",
                    "type": "radio",
                    "stem": "GIL 的核心作用是什么？",
                    "options": [
                        {"value": "A", "label": "保证同一时刻只有一个线程执行字节码"},
                        {"value": "B", "label": "让所有线程并行"},
                    ],
                    "required": True,
                    "points": 20,
                    "difficulty": 3,
                },
                {"id": "q2", "type": "textarea", "stem": "说明 GIL 对 CPU 密集任务的影响", "required": True, "points": 20, "difficulty": 3},
                {"id": "q3", "type": "textarea", "stem": "如何规避 GIL？", "required": True, "points": 20, "difficulty": 2},
                {"id": "q4", "type": "radio", "stem": "IO 密集任务在 GIL 下表现如何？", "options": [{"value": "A", "label": "基本不受影响"}, {"value": "B", "label": "同样串行"}], "required": True, "points": 20, "difficulty": 2},
                {"id": "q5", "type": "textarea", "stem": "简述多进程方案为何能绕开 GIL", "required": True, "points": 20, "difficulty": 3},
            ],
        },
        ensure_ascii=False,
    )


def _evaluation_json() -> str:
    items = [
        {"question_id": "q1", "score": 20, "is_correct": True, "correct_answer": "A", "analysis": "答对：GIL 让字节码串行执行。"},
        {"question_id": "q2", "score": 20, "is_correct": True, "correct_answer": "改用多进程。", "analysis": "说清楚了 CPU 密集场景要走多进程。"},
        {"question_id": "q3", "score": 20, "is_correct": True, "correct_answer": "多进程/无 GIL 实现。", "analysis": "规避方式正确。"},
        {"question_id": "q4", "score": 20, "is_correct": True, "correct_answer": "A", "analysis": "IO 密集不受 GIL 影响。"},
        {"question_id": "q5", "score": 20, "is_correct": True, "correct_answer": "每个进程有独立 GIL。", "analysis": "解释到位。"},
    ]
    return json.dumps(
        {
            "score": 100,
            "items": items,
            "weaknesses": WEAKNESSES,
            "review": "整体掌握良好，注意并发原语的边界语义。",
            "display_text": "总分：100分。整体评价：掌握良好。",
        },
        ensure_ascii=False,
    )


def _reflection_json() -> str:
    """反思结论：四个判据都给白名单内的好结论，overall 应推导为 pass。"""
    return json.dumps(
        {
            "weak_point_coverage": {"verdict": "good", "covered": [WEAKNESSES[0]], "missed": []},
            "chapter_rotation": {"verdict": "diverse", "chapters": ["索引入门", "事务隔离"]},
            "duplication": {"verdict": "fresh", "duplicates": []},
            "difficulty": {"verdict": "appropriate", "evidence": "得分居中，难度合适"},
            "suggestions": ["下一轮补一道并发原语题"],
        },
        ensure_ascii=False,
    )


def _note_markdown() -> str:
    """归档模型输出：四个必备小节都写上，让归档链路能走完并落库。"""
    return (
        "## 测评概览\n"
        "本次练习 5 题，得分 100/100。\n\n"
        "## 薄弱点总结\n"
        f"{'、'.join(WEAKNESSES)}。\n\n"
        "## 错题复盘\n"
        "本次没有错题。\n\n"
        "## 复习建议\n"
        "下一轮补一道并发原语题。\n"
    )


def _answers() -> list[dict[str, Any]]:
    return [
        {"question_id": "q1", "value": "A"},
        {"question_id": "q2", "value": "CPU 密集任务会因 GIL 失去并行，要改用多进程。"},
        {"question_id": "q3", "value": "多进程 / 无 GIL 的运行时。"},
        {"question_id": "q4", "value": "A"},
        {"question_id": "q5", "value": "每个进程有独立的 GIL，因此可真正并行。"},
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="学习测评出题-提交-评分 HTTP 验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vansub{suffix}"  # users.username 只有 20 字符
    user_id = 0
    session_id = 0
    kb_id: int | None = None

    # 离线：`invoke_json`/`archive` 里 `llm or get_chat_model(provider=...)` 是模型的唯一解析点
    import app.agent.workflows.common as workflow_common

    saved_get_chat_model = workflow_common.get_chat_model
    # 三个响应对应三次模型调用：出题 → 评分 → 反思
    fake_model = ScriptedModel([_quiz_json(), _evaluation_json(), _reflection_json()])
    workflow_common.get_chat_model = lambda provider="deepseek": fake_model  # type: ignore[assignment]

    # 归档解析模型的地方**不是** workflow_common，而是 note_archive_service 里那个模块级
    # 绑定（`from app.agent.llm import get_chat_model`）。不 patch 它，这个"零 token"脚本
    # 会真的去打线上模型（既烧 token 又让断言依赖网络输出）。
    import app.services.note_archive_service as note_archive_service

    saved_note_get_chat_model = note_archive_service.get_chat_model
    archive_model = ScriptedModel([_note_markdown()])
    note_archive_service.get_chat_model = lambda provider="deepseek": archive_model  # type: ignore[assignment]

    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            user = User(username=username, password=hash_password(TEMP_PASSWORD))
            db.add(user)
            db.commit()
            db.refresh(user)
            user_id = user.id

            session = SessionModel(user_id=user_id, session_model=0, title="HTTP验收会话")
            db.add(session)
            db.commit()
            db.refresh(session)
            session_id = session.id

            kib = KnowledgeBase(user_id=user_id, name=f"vansub库{suffix}", description="临时验收库")
            db.add(kib)
            db.commit()
            db.refresh(kib)
            kb_id = kib.id
            print(f"临时数据：user={user_id} session={session_id} kb={kb_id}")

            # ---- 无令牌：直接挡在路由外 ----
            no_token = client.post(
                f"/learning-workflows/{'x' * 64}/submit", json={"answers": _answers()}
            )
            check("未带令牌被拦住", no_token.status_code in (401, 403), f"status={no_token.status_code}")

            login = client.post("/auth/login", json={"username": username, "password": TEMP_PASSWORD})
            check("登录成功", login.status_code == 200, f"status={login.status_code}")
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

            # ---- 出题：参数齐 → 当场出题（sync），假模型调用第 1 次 ----
            start = client.post(
                "/learning-workflows/start",
                json={
                    "session_id": session_id,
                    "request_text": "针对 GIL 与并发出一套题",
                    "question_count": 5,
                    "knowledge_base_id": kb_id,
                    "divergence": 3,
                    "difficulty": "进阶",
                },
                headers=headers,
            )
            check("出题请求 200", start.status_code == 200, f"status={start.status_code} body={start.text[:120]}")
            start_data = (start.json() or {}).get("data") or {}
            run_id = str(start_data.get("run_id") or "")
            check("出题返回 run_id", len(run_id) == 32, run_id[:16])
            check("出题状态 waiting_answer", start_data.get("status") == "waiting_answer", str(start_data.get("status")))
            check("题目已生成（5 题）", len((start_data.get("quiz") or {}).get("questions") or []) == 5)
            check("出题调用了模型（第 1 次）", len(fake_model.prompts) == 1, f"calls={len(fake_model.prompts)}")

            # ---- 空答案列表：schema 拦成 422 ----
            empty = client.post(
                f"/learning-workflows/{run_id}/submit", json={"answers": []}, headers=headers
            )
            check("空答案列表 → 422（不是 200 空跑）", empty.status_code == 422, f"status={empty.status_code}")

            # ---- 复现真实 GUI 前提：进程内已经跑过 agent loop ----
            # GUI 里这套题是 loop 的 `delegate_quiz` 出的，而 loop 入口会调
            # `register_worker_handlers()`——它曾经把 `note_archiver` 这个键**覆盖**成
            # 一个要求 `ctx["gateway"]` 的 lambda，于是评分后的自动接力（context 里没有
            # gateway）必然 KeyError → 笔记静默不写。只直连 `/submit` 的脚本永远测不到
            # 这一步，所以这里显式模拟"loop 已经跑过"，并断言这个键**没被换掉**。
            from app.agent.workers import register_worker_handlers
            from app.services.a2a_handoff_service import (
                A2AHandoffService,
                _handle_note_archiver,
            )

            register_worker_handlers()
            check(
                "委派注册不会替换 note_archiver handler（唯一注册点）",
                A2AHandoffService._handlers.get("note_archiver") is _handle_note_archiver,
                str(getattr(A2AHandoffService._handlers.get("note_archiver"), "__name__", "lambda")),
            )

            # ---- 提交：同步立即返回 grading，后台评分（第 25/26 轮异步化契约）----
            calls_before_submit = len(fake_model.prompts)
            submit = client.post(
                f"/learning-workflows/{run_id}/submit",
                json={"answers": _answers()},
                headers=headers,
            )
            check("提交接口 200", submit.status_code == 200, f"status={submit.status_code} body={submit.text[:120]}")
            submit_data = (submit.json() or {}).get("data") or {}
            check("立即返回 status=grading（不等评分）", submit_data.get("status") == "grading", str(submit_data.get("status")))
            check("立即返回不假装有分数（score=0）", submit_data.get("score") == 0, str(submit_data.get("score")))
            check(
                "review 是'正在生成评分'占位",
                "正在生成评分" in str(submit_data.get("review") or ""),
                str(submit_data.get("review"))[:40],
            )

            # ---- TestClient 会先把后台任务跑完，再轮询（真服务器要等 20–40s）----
            detail: dict[str, Any] = {}
            for _ in range(30):
                time.sleep(0.2)
                poll = client.get(f"/learning-workflows/{run_id}", headers=headers)
                if poll.status_code != 200:
                    continue
                detail = (poll.json() or {}).get("data") or {}
                if detail.get("status") in ("completed", "failed"):
                    break
            check("轮询拿到 completed", detail.get("status") == "completed", str(detail.get("status")))
            state = detail.get("state") or {}
            check("分数 100", state.get("score") == 100, str(state.get("score")))
            check("逐题点评 5 条（evaluation.items）", len((state.get("evaluation") or {}).get("items") or []) == 5)
            check(
                "薄弱点精确命中",
                state.get("weaknesses") == WEAKNESSES,
                str(state.get("weaknesses")),
            )
            check("总评非空", bool(state.get("review")), str(state.get("review"))[:40])
            check(
                "反思写进 state（随轮询给前端）",
                (state.get("reflection") or {}).get("overall") == "pass",
                str((state.get("reflection") or {}).get("overall")),
            )
            check(
                "后台评分共调用模型 2 次（评分 + 反思）",
                len(fake_model.prompts) - calls_before_submit == 2,
                f"{len(fake_model.prompts) - calls_before_submit} calls",
            )

            # ---- 消息契约：一条卡片消息，保留 payload（本轮修正：不再清空成纯文本回复）----
            db.rollback()  # 坑 66：消息/笔记是后台任务用另一条连接写的，先 rollback 才能读到
            quiz_msg = db.scalar(
                select(ChatMessage).where(
                    ChatMessage.user_id == user_id,
                    ChatMessage.session_id == session_id,
                    ChatMessage.request_id == f"{run_id}:quiz",
                )
            )
            check("出题/评分共用一条消息（request_id={run_id}:quiz）", quiz_msg is not None)
            if quiz_msg is not None:
                payload = quiz_msg.response_payload or {}
                check(
                    "消息保留学习测评 payload（可随时重开结果弹窗）",
                    payload.get("type") == "learning_quiz" and payload.get("status") == "completed",
                    str(payload.get("status")),
                )
                check("payload 保留题目本体", bool((payload.get("quiz") or {}).get("questions")))
                # 第 27 轮：整段总评**不进聊天回复**（只在结果弹窗/练习记录里看）
                check(
                    "聊天回复不是整段总评（只留完成话术）",
                    "整体掌握良好" not in str(quiz_msg.response_text or ""),
                    str(quiz_msg.response_text)[:40],
                )
                check("消息与 run 关联（run_id 一致）", str(payload.get("run_id") or "") == run_id)

            # ---- A2A：有薄弱点 → 自动写一条 AI 归档笔记 ----
            note_count = db.scalar(select(func.count(Note.id)).where(Note.user_id == user_id))
            check("评分后自动归档写了一条笔记（note_archiver）", int(note_count or 0) > 0, f"notes={note_count}")
            # 归档不只是"有一行 notes"：它必须**属于这一场** run，并被回写进 run 状态
            # （`NoteService._mark_workflow_note_completed` 写 note_id/note_status）。
            # 这才是"自动接力真的派发过"的持久证据——派发失败时这里是空的。
            archived = db.scalar(
                select(Note).where(Note.user_id == user_id, Note.source_run_id == run_id)
            )
            check("归档笔记属于本场 run（source_run_id 对齐）", archived is not None, f"run_id={run_id}")
            run_row = db.scalar(select(WorkflowRun).where(WorkflowRun.run_id == run_id))
            check(
                "归档结果回写进 run 状态（note_status=completed）",
                (dict(run_row.state_json or {}) if run_row else {}).get("note_status") == "completed",
                str((dict(run_row.state_json or {}) if run_row else {}).get("note_status")),
            )

            # ---- 练习记录：总分落库，且经 run_id 能关联到 run 的 review/reflection ----
            db.rollback()  # 坑 66：REPEATABLE READ 下同一连接读不到 TestClient 写的新数据
            ps = db.scalar(
                select(PracticeSet).where(
                    PracticeSet.user_id == user_id,
                    PracticeSet.knowledge_base_id == kb_id,
                )
            )
            check("practice_sets 落库且总分 100", ps is not None and ps.total_score == 100, f"total={ps.total_score if ps else None}")
            check("practice_sets 关联 run_id（练习记录回看/归档定位）",
                  ps is not None and ps.run_id == run_id, f"run_id={ps.run_id if ps else None}")

            # ---- 重复提交：状态已离开 waiting_answer → 400 ----
            re_submit = client.post(
                f"/learning-workflows/{run_id}/submit", json={"answers": _answers()}, headers=headers
            )
            check("重复提交 → 400", re_submit.status_code == 400, f"status={re_submit.status_code} body={re_submit.text[:80]}")

            print(f"\n结果：{passed} 通过 / {failed} 失败")
            return 1 if failed else 0

    finally:
        workflow_common.get_chat_model = saved_get_chat_model  # type: ignore[assignment]
        note_archive_service.get_chat_model = saved_note_get_chat_model  # type: ignore[assignment]
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    # 外键顺序不能乱（坑 71/73）：ChatMessage/Note → WorkflowRun → Attempt/Question
                    # → PracticeSet → UserKnowledgeMastery/UserProfile → KnowledgeBase → Session → User
                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    db.execute(sql_delete(Note).where(Note.user_id == user_id))
                    db.execute(sql_delete(WorkflowRun).where(WorkflowRun.user_id == user_id))
                    attempt_ids = list(
                        db.scalars(
                            select(Question.id).where(
                                Question.practice_set_id.in_(
                                    select(PracticeSet.id).where(PracticeSet.user_id == user_id)
                                )
                            )
                        )
                    )
                    if attempt_ids:
                        db.execute(sql_delete(Attempt).where(Attempt.question_id.in_(attempt_ids)))
                        db.execute(sql_delete(Question).where(Question.id.in_(attempt_ids)))
                    db.execute(sql_delete(PracticeSet).where(PracticeSet.user_id == user_id))
                    db.execute(sql_delete(UserKnowledgeMastery).where(UserKnowledgeMastery.user_id == user_id))
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    kb_ids = list(db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.user_id == user_id)))
                    if kb_ids:
                        db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
                    db.execute(sql_delete(SessionModel).where(SessionModel.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    from app.main import app

    raise SystemExit(main())
