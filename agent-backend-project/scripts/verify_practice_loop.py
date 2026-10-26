"""第 1 期第一切片闭环验收（可复跑）：出题 → 作答 → 评测 → 核对落库。

验证的是「练习记录实体化」这件事：
  出题成功 → `practice_sets` 1 行 + `questions` N 行
  提交评测 → `attempts` N 行 + 回写 `questions.answer` + 更新 `practice_sets.status/total_score`
  并能按 `knowledge_base_id` 查回这次练习历史

前提：**后端已在 --base-url 上运行**，且 LLM 可用（出题/评测会真的调模型）。
用法（在 agent-backend-project 目录下）：

    python scripts/verify_practice_loop.py
    python scripts/verify_practice_loop.py --question-count 3
    python scripts/verify_practice_loop.py --username 你的账号 --password 你的密码
    python scripts/verify_practice_loop.py --cleanup     # 跑完把本次产生的数据删掉

默认会注册一个 `verify_<时间戳>` 的临时账号、建一个临时知识库（本切片还没有知识库 CRUD 路由，
所以直接走 ORM 插一行），然后跑一次完整练习。
"""

import argparse
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import Attempt, KnowledgeBase, PracticeSet, Question, Session as SessionModel
from app.services.practice_service import PracticeService

DEFAULT_REQUEST_TEXT = "针对 Python GIL 与多线程的关系出 3 道题，覆盖概念、影响和规避方式"


def _pick_answer(question: dict) -> object:
    """按题型造一个作答：单选/多选取第一个选项，简答给一段话。"""
    options = question.get("options") or []
    if question.get("type") == "checkbox":
        return [options[0]["value"]] if options else []
    if question.get("type") == "radio":
        return options[0]["value"] if options else ""
    return "GIL 让同一时刻只有一个线程执行 Python 字节码，CPU 密集任务改用多进程。"


def _login_or_register(client: httpx.Client, username: str | None, password: str) -> str:
    """返回 access_token；username 为空时自动注册一个临时账号。"""
    if not username:
        username = f"verify_{int(time.time())}"
        resp = client.post("/auth/register", json={"username": username, "password": password})
        print(f"[1] 注册临时账号 {username}: {resp.status_code}")
        if resp.status_code not in (200, 201):
            raise SystemExit(f"注册失败: {resp.text[:400]}")
    else:
        print(f"[1] 使用已有账号 {username}")

    resp = client.post("/auth/login", json={"username": username, "password": password})
    if resp.status_code != 200:
        raise SystemExit(f"登录失败: {resp.status_code} {resp.text[:400]}")
    print("[2] 登录成功")
    return resp.json()["data"]["access_token"]


def _wait_graded(
    client: httpx.Client, headers: dict[str, str], run_id: str, submit_data: dict, *, tries: int = 60
) -> dict:
    """等后台评分跑完，返回带 score/weaknesses 的结果 dict。

    第 25 轮起 `/submit` **立即返回** `status=grading`（评分是 LLM 调用，同步等会被前端
    30 秒超时掐断）：真服务器上要轮询 `GET /learning-workflows/{run_id}`，
    `completed` 后从 `state` 里取分数/点评。已经是终态就直接返回。
    """
    if submit_data.get("status") != "grading":
        return submit_data
    detail: dict = {}
    for _ in range(tries):
        time.sleep(1.0)
        resp = client.get(f"/learning-workflows/{run_id}", headers=headers)
        if resp.status_code != 200:
            continue
        detail = resp.json().get("data") or {}
        status = detail.get("status")
        if status in ("completed", "failed"):
            break
    if detail.get("status") != "completed":
        print(f"    ✗ 评分没跑完（status={detail.get('status')}）")
        return detail
    state = detail.get("state") or {}
    return {"status": detail.get("status"), **{key: state.get(key) for key in
            ("score", "weaknesses", "review", "evaluation", "reflection")}}


def main() -> int:
    parser = argparse.ArgumentParser(description="第 1 期第一切片闭环验收")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="后端地址")
    parser.add_argument("--username", default=None, help="已有账号（不传则自动注册临时账号）")
    parser.add_argument("--password", default="verify_pass_123", help="账号密码（默认 verify_pass_123）")
    parser.add_argument("--question-count", type=int, default=5, help="出题数量（schema 下限 5）")
    parser.add_argument("--request-text", default=DEFAULT_REQUEST_TEXT, help="出题要求")
    parser.add_argument("--timeout", type=float, default=180.0, help="单请求超时秒数")
    parser.add_argument("--cleanup", action="store_true", help="跑完删除本次产生的数据")
    args = parser.parse_args()

    stamp = int(time.time())
    created: dict[str, object] = {}
    ok = False

    with httpx.Client(base_url=args.base_url, timeout=args.timeout) as client:
        try:
            token = _login_or_register(client, args.username, args.password)
        except httpx.ConnectError:
            raise SystemExit(
                f"连不上 {args.base_url}。先在 agent-backend-project 下启动后端：\n"
                "  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
            )
        headers = {"Authorization": f"Bearer {token}"}

        resp = client.post(
            "/sessions", json={"session_model": 0, "title": f"验收会话_{stamp}"}, headers=headers
        )
        if resp.status_code != 200:
            raise SystemExit(f"建会话失败: {resp.status_code} {resp.text[:400]}")
        session_id = resp.json()["data"]["session"]["id"]
        print(f"[3] 学习会话 session_id={session_id}")

        db = SessionLocal()
        try:
            user_id = db.scalar(select(SessionModel.user_id).where(SessionModel.id == session_id))
            knowledge_base = KnowledgeBase(
                user_id=user_id,
                name=f"验收知识库_{stamp}",
                description="verify_practice_loop.py 临时知识库（本切片无知识库 CRUD 路由）",
            )
            db.add(knowledge_base)
            db.commit()
            db.refresh(knowledge_base)
            knowledge_base_id = knowledge_base.id
        finally:
            db.close()
        created["knowledge_base_id"] = knowledge_base_id
        print(f"[4] 临时知识库 knowledge_base_id={knowledge_base_id}（user_id={user_id}）")

        resp = client.post(
            "/learning-workflows/start",
            json={
                "session_id": session_id,
                "request_text": args.request_text,
                "question_count": args.question_count,
                "knowledge_base_id": knowledge_base_id,
                # 第 23 轮：发散度/难度没传会先出参数卡（"只问缺的"），所以这里显式给全
                "divergence": 3,
                "difficulty": "进阶",
            },
            headers=headers,
        )
        print(f"[5] 出题: HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:800])
            return 1
        start_data = resp.json()["data"]
        run_id = start_data["run_id"]
        questions = (start_data.get("quiz") or {}).get("questions") or []
        # 参数给全了就不该出现参数卡；若出现说明"只问缺的"口径回归了，直接报错更醒目
        if start_data.get("status") == "awaiting_params" or start_data.get("params"):
            print("    参数卡不该出现（本次已传全参数）:", start_data.get("params"))
            return 1
        print(f"    run_id={run_id} 题目数={len(questions)}")
        for item in questions:
            print(f"    - {item.get('id')} [{item.get('type')}] {str(item.get('stem'))[:50]}")

        answers = [{"question_id": q["id"], "value": _pick_answer(q)} for q in questions]
        resp = client.post(
            f"/learning-workflows/{run_id}/submit", json={"answers": answers}, headers=headers
        )
        print(f"[6] 提交+评测: HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(resp.text[:800])
            return 1
        submit_data = resp.json()["data"]
        # 第 25 轮起提交是**异步**的：立即返回 grading，评分在后台跑（同步等会被前端 30s 超时掐断）
        print(f"    submit 立即返回 status={submit_data.get('status')}")
        submit_data = _wait_graded(client, headers, run_id, submit_data)
        print(f"    score={submit_data.get('score')}")
        print(f"    weaknesses={submit_data.get('weaknesses')}")

    # ---- 落库核对 ----
    db = SessionLocal()
    try:
        practice_set = db.scalar(
            select(PracticeSet)
            .where(PracticeSet.user_id == user_id, PracticeSet.knowledge_base_id == knowledge_base_id)
            .order_by(PracticeSet.id.desc())
        )
        if practice_set is None:
            print("[7] ✗ 没有找到 practice_sets 行——出题落库没生效")
            return 1
        created["practice_set_id"] = practice_set.id

        stored_questions = PracticeService.list_questions(db, practice_set_id=practice_set.id)
        question_ids = [q.id for q in stored_questions]
        attempt_count = db.scalar(
            select(func.count(Attempt.id)).where(Attempt.question_id.in_(question_ids))
        ) or 0
        answered = sum(1 for q in stored_questions if q.answer)

        print(f"[7] practice_sets: id={practice_set.id} status={practice_set.status} "
              f"total_score={practice_set.total_score} question_count={practice_set.question_count}")
        print(f"    questions 行数={len(stored_questions)} 题号={[q.question_code for q in stored_questions]}")
        print(f"    attempts 行数={attempt_count}（应为题目数 {len(questions)}）")
        print(f"    已回写参考答案的题目数={answered}（应为 {len(stored_questions)}）")
        for row in db.scalars(
            select(Attempt).where(Attempt.question_id.in_(question_ids)).order_by(Attempt.id)
        ).all():
            print(f"    - attempt {row.id}: question_id={row.question_id} score={row.score} "
                  f"is_correct={row.is_correct} analysis={str(row.analysis)[:30]}")

        history, total = PracticeService.list_practice_history(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id
        )
        print(f"[8] 按 knowledge_base_id={knowledge_base_id} 查练习历史: total={total}")
        for row in history[:5]:
            print(f"    - practice_set {row.id}《{row.title}》status={row.status} total_score={row.total_score}")
        _, other_total = PracticeService.list_practice_history(
            db, user_id=user_id, knowledge_base_id=999999
        )
        print(f"[9] 不存在的 knowledge_base_id=999999 查历史: total={other_total}（应为 0）")

        ok = (
            practice_set.status == "completed"
            and len(stored_questions) == len(questions)
            and attempt_count == len(questions)
            and answered == len(stored_questions)
            and total >= 1
            and other_total == 0
        )

        if args.cleanup:
            for attempt in db.scalars(select(Attempt).where(Attempt.question_id.in_(question_ids))).all():
                db.delete(attempt)
            for question in stored_questions:
                db.delete(question)
            db.flush()
            for row in db.scalars(
                select(PracticeSet).where(PracticeSet.knowledge_base_id == knowledge_base_id)
            ).all():
                db.delete(row)
            db.flush()
            kb_row = db.get(KnowledgeBase, knowledge_base_id)
            if kb_row is not None:
                db.delete(kb_row)
            db.commit()
            print("[10] --cleanup：本次产生的 attempts / questions / practice_sets / knowledge_bases 已删除")
    finally:
        db.close()

    print("\n验收结果:", "PASS" if ok else "FAIL")
    if not ok:
        print("排查提示：若 attempts 行数为 0，通常是评测模型返回的 items 里 question_id 对不上题号"
              "（应为 q1/q2…）；看后端日志里 '答题记录落库失败' 或 '已落库 0 条答题记录'。")
    else:
        print("想自己看数据就查（库 agent_project_database）：")
        print("  SELECT id,knowledge_base_id,status,total_score,question_count FROM practice_sets;")
        print("  SELECT id,practice_set_id,question_code,question_type,points FROM questions;")
        print("  SELECT id,question_id,score,is_correct FROM attempts;")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
