"""验收：出题参数卡的 HTTP 层端到端（`/agent/tasks/start` + `/learning-workflows/{run_id}/params`）。

`verify_quiz_params.py` 钉的是服务层口径（什么时候问、问什么、续跑与原地更新）。
这一套补它覆盖不到的一层：

- 直接「出题测评」入口（`agent_name=quiz_generate_workflow`）在参数没传时**不出题**，
  返回 `status=awaiting_params` + `data.params`，并且会话里真的出现一条参数卡消息；
- `POST /learning-workflows/{run_id}/params` 接上路由/鉴权/校验：
  未带令牌拦住、题数越界 422、别人的 run 404、已确认过的 run 400；
- 提交后响应里就是题目，会话里**同一条消息**从参数卡变成题目（前端回刷即见答题弹窗）；
- 只有唯一知识库的用户不再被问知识库（`resolved=true`）。

全程离线：进程内把 `get_chat_model` 换成假出题模型（不联网、不花额度）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_quiz_params_http.py
    python scripts/verify_quiz_params_http.py --keep     # 保留临时数据
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
    ChatMessage,
    KnowledgeBase,
    PracticeSet,
    Question,
    Session,
    User,
    UserKnowledgeMastery,
    UserProfile,
    WorkflowRun,
)
from app.main import app
from app.security import hash_password

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


class FakeQuizModel:
    """假出题模型：回放给定题目 JSON，并记下调用次数（证明没白花额度）。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def invoke(self, prompt: str) -> SimpleNamespace:
        PROMPTS.append(str(prompt))
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


def _wait_quiz_ready(client: Any, headers: dict[str, str], run_id: str, *, tries: int = 25) -> dict[str, Any]:
    """等后台出题跑完，返回 `GET /learning-workflows/{run_id}` 的 `data`。

    第 25 轮起 `/params` 是**异步**的：立即返回 `generating_quiz`，出题在 `BackgroundTasks`
    里跑（前端轮询运行状态）。`TestClient` 会在响应返回前把后台任务跑完，所以通常第一次
    GET 就绪；仍然写成轮询——真 ASGI 服务器上这就是真的异步，脚本不该赌实现细节。
    """
    detail: dict[str, Any] = {}
    for _ in range(tries):
        resp = client.get(f"/learning-workflows/{run_id}", headers=headers)
        detail = (resp.json() or {}).get("data") or {}
        if detail.get("status") in ("waiting_answer", "failed"):
            return detail
        time.sleep(0.2)
    return detail


def _quiz_payload(count: int) -> dict[str, Any]:
    return {
        "title": "HTTP 参数卡验收",
        "description": "假模型题目",
        "question_count": count,
        "questions": [
            {
                "id": f"q{index}",
                "type": "radio",
                "stem": f"第 {index} 题：GIL 的作用是什么？",
                "options": [{"value": "A", "label": "同一时刻只有一个线程执行字节码"}],
                "required": True,
                "points": round(100 / count, 2),
                "difficulty": 3,
            }
            for index in range(1, count + 1)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="出题参数卡 HTTP 层验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vanp{suffix}"  # users.username 只有 20 字符
    other_name = f"vanpo{suffix}"
    user_id = other_id = 0

    # 离线：`invoke_json` 里 `llm or get_chat_model(provider=...)` 是模型的唯一解析点
    import app.agent.workflows.common as workflow_common

    saved_get_chat_model = workflow_common.get_chat_model
    workflow_common.get_chat_model = lambda provider="deepseek": FakeQuizModel(  # type: ignore[assignment]
        _quiz_payload(6)
    )

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

            session = Session(user_id=user_id, session_model=0, title="HTTP参数卡验收会话")
            other_session = Session(user_id=other_id, session_model=0, title="别人的会话")
            kb = KnowledgeBase(user_id=user_id, name="HTTP参数卡验收库A")
            kb_b = KnowledgeBase(user_id=user_id, name="HTTP参数卡验收库B")
            db.add_all([session, other_session, kb, kb_b])
            db.commit()
            db.refresh(session)
            db.refresh(other_session)
            db.refresh(kb)
            db.refresh(kb_b)
            print(f"临时用户 id={user_id} / id={other_id}；会话 {session.id}；知识库 {kb.id}/{kb_b.id}")

            start_body = {
                "session_id": session.id,
                "session_model": 0,
                "request_text": "给我出点题练练",
                "agent_name": "quiz_generate_workflow",
            }

            # ---- 鉴权 ----
            no_token = client.post("/agent/tasks/start", json=start_body)
            check("未带令牌被拦住", no_token.status_code in (401, 403),
                  f"status={no_token.status_code}")

            login = client.post(
                "/auth/login", json={"username": username, "password": TEMP_PASSWORD}
            )
            check("登录成功", login.status_code == 200, f"status={login.status_code}")
            headers = {"Authorization": f"Bearer {login.json()['data']['access_token']}"}

            # ---- 直接出题测评：参数没传 → 先出参数卡，不出题 ----
            calls_before = len(PROMPTS)
            started = client.post("/agent/tasks/start", json=start_body, headers=headers)
            check("启动接口 200", started.status_code == 200,
                  f"status={started.status_code} body={started.text[:160]}")
            start_data = (started.json() or {}).get("data") or {}
            check("路由命中工作流", start_data.get("workflow_type") == "learning_quiz",
                  str(start_data.get("workflow_type")))
            check("状态是 awaiting_params（没有直接出题）",
                  start_data.get("status") == "awaiting_params", str(start_data.get("status")))
            # 注意：`/agent/tasks/start` 的响应是**两层** data——外层是路由信息，
            # 业务数据（quiz / params）在 `data.data` 里（前端 AgentTaskResult 也是这么读的）
            nested = start_data.get("data") or {}
            params = nested.get("params") or {}
            check("响应里带参数卡规格", bool(params.get("fields")) and bool(params.get("missing")),
                  str(params.get("missing")))
            check("参数不齐时不调用出题模型（不白花额度）", len(PROMPTS) == calls_before,
                  f"calls={len(PROMPTS) - calls_before}")
            run_id = str(start_data.get("run_id") or "")
            check("响应带 run_id（前端提交参数要用）", bool(run_id), run_id)

            # ---- 会话里真的出现了参数卡消息（前端回刷就能渲染） ----
            messages = client.get(
                f"/sessions/{session.id}/messages",
                params={"page": 1, "page_size": 20, "session_model": 0},
                headers=headers,
            )
            check("消息列表接口 200", messages.status_code == 200, f"status={messages.status_code}")
            records = ((messages.json() or {}).get("data") or {}).get("messages") or []
            card = next(
                (row for row in records if row.get("request_id") == f"{run_id}:quiz"), None
            )
            check("会话里写出了参数卡消息", card is not None, str(len(records)))
            card_payload = (card or {}).get("response_payload") or {}
            check("参数卡消息 payload.status = awaiting_params",
                  card_payload.get("status") == "awaiting_params", str(card_payload.get("status")))
            check("参数卡消息带规格（前端据此渲染控件）",
                  bool((card_payload.get("params") or {}).get("fields")))
            check("参数卡消息没有题目（还没出题）",
                  not ((card_payload.get("quiz") or {}).get("questions")),
                  str(card_payload.get("quiz"))[:80])

            # ---- 提交参数：校验拦住非法值 ----
            unauth = client.post(
                f"/learning-workflows/{run_id}/params",
                json={"question_count": 6, "knowledge_base_id": kb.id, "divergence": 2,
                      "difficulty": "进阶"},
            )
            check("确认参数未带令牌被拦住", unauth.status_code in (401, 403),
                  f"status={unauth.status_code}")
            too_few = client.post(
                f"/learning-workflows/{run_id}/params", json={"question_count": 4}, headers=headers
            )
            check("题数 4 → schema 拦住（422）", too_few.status_code == 422,
                  f"status={too_few.status_code}")
            too_many = client.post(
                f"/learning-workflows/{run_id}/params", json={"question_count": 21}, headers=headers
            )
            check("题数 21 → schema 拦住（422）", too_many.status_code == 422,
                  f"status={too_many.status_code}")
            bad_div = client.post(
                f"/learning-workflows/{run_id}/params", json={"divergence": 9}, headers=headers
            )
            check("发散度 9 → schema 拦住（422）", bad_div.status_code == 422,
                  f"status={bad_div.status_code}")
            stranger_kb = client.post(
                f"/learning-workflows/{run_id}/params",
                json={"knowledge_base_id": 99999999},
                headers=headers,
            )
            check("不存在的知识库 → 404", stranger_kb.status_code == 404,
                  f"status={stranger_kb.status_code}")

            # ---- 提交合法参数：出题 + 原地更新那条消息 ----
            configured = client.post(
                f"/learning-workflows/{run_id}/params",
                json={"question_count": 6, "knowledge_base_id": kb.id, "divergence": 2,
                      "difficulty": "高级"},
                headers=headers,
            )
            check("确认参数 200", configured.status_code == 200,
                  f"status={configured.status_code} body={configured.text[:160]}")
            config_data = (configured.json() or {}).get("data") or {}
            check("确认后立即返回 generating_quiz（同步等出题会被前端 30s 超时掐断）",
                  config_data.get("status") == "generating_quiz",
                  str(config_data.get("status")))
            ready = _wait_quiz_ready(client, headers, run_id)
            ready_state = ready.get("state") or {}
            quiz = ready_state.get("quiz") or {}
            check("轮询到 waiting_answer", ready.get("status") == "waiting_answer",
                  str(ready.get("status")))
            check("后台出题把题目写进运行状态（6 题）", len(quiz.get("questions") or []) == 6,
                  f"题目数={len(quiz.get('questions') or [])}")
            check("运行状态里知识库就是确认时选的那个", ready_state.get("knowledge_base_id") == kb.id,
                  str(ready_state.get("knowledge_base_id")))
            check("确认后才调用出题模型", len(PROMPTS) > calls_before, f"calls={len(PROMPTS)}")
            check("提示词带本次难度（高级）", "## 本次难度：高级" in (PROMPTS[-1] if PROMPTS else ""))

            messages2 = client.get(
                f"/sessions/{session.id}/messages",
                params={"page": 1, "page_size": 20, "session_model": 0},
                headers=headers,
            )
            records2 = ((messages2.json() or {}).get("data") or {}).get("messages") or []
            quiz_rows = [row for row in records2 if row.get("request_id") == f"{run_id}:quiz"]
            check("参数卡与题目共用同一条消息（不新增气泡）", len(quiz_rows) == 1,
                  f"条数={len(quiz_rows)}")
            final_payload = (quiz_rows[0].get("response_payload") or {}) if quiz_rows else {}
            check("那条消息已经变成题目（status=waiting_answer + 6 题）",
                  final_payload.get("status") == "waiting_answer"
                  and len((final_payload.get("quiz") or {}).get("questions") or []) == 6,
                  f"status={final_payload.get('status')}")

            # ---- 幂等/状态校验：已确认过的 run 不能再确认 ----
            twice = client.post(
                f"/learning-workflows/{run_id}/params", json={"question_count": 6}, headers=headers
            )
            check("已确认过的 run 再确认 → 400", twice.status_code == 400,
                  f"status={twice.status_code}")
            missing_run = client.post(
                "/learning-workflows/not-a-run/params", json={"question_count": 6}, headers=headers
            )
            check("不存在的 run → 404", missing_run.status_code == 404,
                  f"status={missing_run.status_code}")

            # ---- 越权：别人的会话不能拿来出题（会话归属校验在服务层） ----
            other_login = client.post(
                "/auth/login", json={"username": other_name, "password": TEMP_PASSWORD}
            )
            other_headers = {
                "Authorization": f"Bearer {other_login.json()['data']['access_token']}"
            }
            cross = client.post(
                "/agent/tasks/start",
                json={**start_body, "session_id": session.id},
                headers=other_headers,
            )
            check("别人登录后拿当前用户的会话出题 → 404", cross.status_code == 404,
                  f"status={cross.status_code}")
            own = client.post(
                "/agent/tasks/start",
                json={**start_body, "session_id": other_session.id},
                headers=headers,
            )
            check("自己的令牌拿别人的会话出题 → 404", own.status_code == 404,
                  f"status={own.status_code}")

            # ---- 唯一知识库：不再问知识库 ----
            solo = User(username=f"vanps{suffix}", password=hash_password(TEMP_PASSWORD))
            db.add(solo)
            db.commit()
            db.refresh(solo)
            solo_session = Session(user_id=solo.id, session_model=0, title="唯一库会话")
            solo_kb = KnowledgeBase(user_id=solo.id, name="唯一库")
            db.add_all([solo_session, solo_kb])
            db.commit()
            db.refresh(solo_session)
            db.refresh(solo_kb)
            solo_login = client.post(
                "/auth/login", json={"username": solo.username, "password": TEMP_PASSWORD}
            )
            solo_headers = {
                "Authorization": f"Bearer {solo_login.json()['data']['access_token']}"
            }
            solo_start = client.post(
                "/agent/tasks/start",
                json={**start_body, "session_id": solo_session.id},
                headers=solo_headers,
            )
            solo_data = (solo_start.json() or {}).get("data") or {}
            solo_params = (solo_data.get("data") or {}).get("params") or {}
            solo_kb_field = next(
                (field for field in solo_params.get("fields") or []
                 if field.get("key") == "knowledge_base_id"),
                {},
            )
            check("唯一知识库自动使用（resolved=true）", solo_kb_field.get("resolved") is True,
                  str(solo_kb_field))
            check("唯一库不算 missing",
                  "knowledge_base_id" not in (solo_params.get("missing") or []),
                  str(solo_params.get("missing")))
            check("知识库候选恒带「不使用知识库」（value=0）",
                  any(option.get("value") == 0 for option in solo_kb_field.get("options") or []),
                  str(solo_kb_field.get("options")))
            # 唯一库已定 → 确认参数时前端**不会**提交这个 key（它是 settled 行）。
            # 提交体里 `knowledge_base_id` 缺席/为 None 都必须沿用已定值（第 24 轮修的坑：
            # 旧实现把 None 当成"用户选了不用库"，于是练习丢了知识库归属）。
            solo_run_id = str(solo_data.get("run_id") or "")
            solo_configured = client.post(
                f"/learning-workflows/{solo_run_id}/params",
                json={"question_count": 6, "divergence": 3, "difficulty": "进阶"},
                headers=solo_headers,
            )
            solo_final = _wait_quiz_ready(client, solo_headers, solo_run_id)
            solo_found, solo_kb_value = _run_practice_knowledge_base(db, solo_run_id)
            check("唯一库自动使用时确认参数不会丢库（练习仍归属该库）",
                  solo_configured.status_code == 200
                  and solo_final.get("status") == "waiting_answer"
                  and solo_found and solo_kb_value == solo_kb.id,
                  f"status={solo_configured.status_code} 运行={solo_final.get('status')} "
                  f"落库={solo_found} kb={solo_kb_value}")

            # ---- 明确「不使用知识库」的出题（第 24 轮）：0 是"已定"，不再追问 ----
            none_start = client.post(
                "/agent/tasks/start",
                json={**start_body, "session_id": session.id, "knowledge_base_id": 0},
                headers=headers,
            )
            none_data = (none_start.json() or {}).get("data") or {}
            none_inner = none_data.get("data") or {}
            none_params = none_inner.get("params") or {}
            none_missing = none_params.get("missing") or []
            check("直接传 0（明确不用库）→ 不再问知识库",
                  none_start.status_code == 200
                  and none_data.get("status") == "awaiting_params"
                  and "knowledge_base_id" not in none_missing
                  and set(none_missing) == {"question_count", "divergence", "difficulty"},
                  f"status={none_start.status_code} missing={none_missing}")
            none_kb_field = next(
                (field for field in none_params.get("fields") or []
                 if field.get("key") == "knowledge_base_id"),
                {},
            )
            check("「不使用知识库」在规格里是已定值 0",
                  none_kb_field.get("resolved") is True and none_kb_field.get("value") == 0,
                  f"resolved={none_kb_field.get('resolved')} value={none_kb_field.get('value')}")
            none_run_id = str(none_data.get("run_id") or "")
            none_configured = client.post(
                f"/learning-workflows/{none_run_id}/params",
                json={"question_count": 6, "knowledge_base_id": 0, "divergence": 3,
                      "difficulty": "进阶"},
                headers=headers,
            )
            none_final = _wait_quiz_ready(client, headers, none_run_id)
            none_ready_quiz = (none_final.get("state") or {}).get("quiz") or {}
            none_found, none_kb_value = _run_practice_knowledge_base(db, none_run_id)
            check("提交 0 能出题且练习不归属任何库（NULL 而不是 0）",
                  none_configured.status_code == 200
                  and len(none_ready_quiz.get("questions") or []) == 6
                  and none_found and none_kb_value is None,
                  f"status={none_configured.status_code} 运行={none_final.get('status')} "
                  f"落库={none_found} kb={none_kb_value}")

            # ---- 直接传全参数的调用方零回归（老脚本/老客户端） ----
            full_start = client.post(
                "/learning-workflows/start",
                json={
                    "session_id": session.id,
                    "request_text": "直接出 10 道题",
                    "question_count": 10,
                    "knowledge_base_id": kb.id,
                    "divergence": 5,
                    "difficulty": "入门",
                },
                headers=headers,
            )
            full_data = (full_start.json() or {}).get("data") or {}
            check("教学保留入口：参数齐了直接出题（无参数卡）",
                  full_start.status_code == 200
                  and full_data.get("status") == "waiting_answer"
                  and not full_data.get("params"),
                  f"status={full_start.status_code}")

            if not args.keep:
                _cleanup(db, user_ids=[user_id, other_id, solo.id])
                print("[9] 已清理本次产生的数据")
    finally:
        workflow_common.get_chat_model = saved_get_chat_model  # type: ignore[assignment]
        db.close()

    print(f"\n出题参数卡 HTTP 验收：{PASSED} 通过 / {FAILED} 失败")
    return 1 if FAILED else 0


def _run_practice_knowledge_base(db, run_id: str) -> tuple[bool, int | None]:
    """读某次运行落库的练习及其知识库归属，返回 `(练习在不在, 库 id)`。

    走**运行状态**取 `practice_set_id`：HTTP 的 `/{run_id}/params` 响应只回
    `run_id/status/quiz/params`，没有练习 id（接口形状不需要为验收改）。

    必须**先 `rollback()`**：本脚本的 `db` 会话在 TestClient 之前就读过数据，那个未结束的
    事务带着 REPEATABLE READ 的旧快照，"看不见"经 HTTP 写进来的运行与练习（交接文档坑 66）。
    所以这里只取列值，不返回 ORM 对象——旧快照里的对象是过期的。
    """
    if not run_id:
        return False, None
    db.rollback()
    state = db.scalar(select(WorkflowRun.state_json).where(WorkflowRun.run_id == run_id))
    practice_id = int((state or {}).get("practice_set_id") or 0)
    if not practice_id:
        return False, None
    row = db.execute(
        select(PracticeSet.knowledge_base_id).where(PracticeSet.id == practice_id)
    ).first()
    if row is None:
        return False, None
    return True, row[0]


def _cleanup(db, *, user_ids: list[int]) -> None:
    """按外键顺序删掉本次验收的行（消息 → 运行 → 题目/练习 → 掌握度 → 库 → 会话 → 用户）。

    开头先 `rollback()`：本会话在 TestClient 之前读过数据（`db.refresh`）会留下一个
    **未结束的事务**，MySQL 默认 REPEATABLE READ 下它带着旧快照——后续的 `select`
    会"看不见"经 HTTP 写进来的 practice_sets，于是删库时撞 1451 外键
    （第 22 轮清理漏删 workflow_runs 是同一类坑）。
    """
    db.rollback()
    db.execute(delete(ChatMessage).where(ChatMessage.user_id.in_(user_ids)))
    db.execute(delete(WorkflowRun).where(WorkflowRun.user_id.in_(user_ids)))
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
    db.execute(delete(UserProfile).where(UserProfile.user_id.in_(user_ids)))
    kb_ids = list(
        db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.user_id.in_(user_ids)))
    )
    if kb_ids:
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(kb_ids)))
    db.execute(delete(Session).where(Session.user_id.in_(user_ids)))
    db.execute(delete(User).where(User.id.in_(user_ids)))
    db.commit()


if __name__ == "__main__":
    sys.exit(main())
