"""决定性探针：同一个 provider/模型，直接发请求，看是否本身就不稳定。

背景：probe_quiz_generation.py 三次得到四种结果（JSON 截断 / 空输出），
需要区分「provider 本身不稳」与「出题提示词太长导致输出被截断」。

- 测试 A：极短请求（约 10 token 输出）→ 若也失败，说明 provider 不稳；
- 测试 B：要求输出一段长 JSON（模拟出题那种量）→ 看是否被 max_tokens 截断。

跑法：python scripts/probe_llm_stability.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.settings import settings
from app.db.database import SessionLocal
from app.db.models import (
    ChatMessage,
    KnowledgeBase,
    PracticeSet,
    Question,
    Session as SessionModel,
    User,
    WorkflowRun,
)
from app.security import hash_password
from app.services.learning_workflow_service import LearningWorkflowService

TEMP_PASSWORD = "verify_pass_123"


def main() -> int:
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.agent.llm import get_chat_model

    print(f"model={settings.deepseek_model} max_tokens={settings.deepseek_max_tokens}")
    model = get_chat_model(provider="deepseek")

    print("\n=== 测试 A：极短请求（3 次）===")
    for attempt in range(1, 4):
        started = time.perf_counter()
        try:
            message = model.invoke(
                [HumanMessage(content="只回复四个字：模型在线")]
            )
            text = str(getattr(message, "content", ""))
            usage = getattr(message, "usage_metadata", None) or {}
            print(
                f"  A{attempt}: 耗时 {time.perf_counter() - started:.1f}s | 长度={len(text)} "
                f"| usage={usage} | 内容={text[:30]!r}"
            )
        except Exception as exc:
            print(f"  A{attempt}: 异常 {type(exc).__name__}: {exc}")

    print("\n=== 测试 B：要求输出较长 JSON（模拟出题）===")
    system = "你是出题助手。只输出 JSON，不要任何解释或 Markdown 代码块。"
    human = (
        "针对「聚簇索引与二级索引的区别」出 3 道单选题。"
        '严格输出：{"questions":[{"id":"q1","stem":"题干","options":[{"key":"A","text":"选项"}],'
        '"answer":"A","explanation":"解析"}]}'
    )
    started = time.perf_counter()
    try:
        message = model.invoke([SystemMessage(content=system), HumanMessage(content=human)])
        text = str(getattr(message, "content", ""))
        usage = getattr(message, "usage_metadata", None) or {}
        print(f"  耗时 {time.perf_counter() - started:.1f}s | 长度={len(text)} | usage={usage}")
        print(f"  结尾 100 字符：{text[-100:]!r}")
        print(f"  能否 json.loads：{'能' if _parses(text) else '不能'}")
    except Exception as exc:
        print(f"  异常 {type(exc).__name__}: {exc}")

    print("\n=== 测试 C：真实出题链路（1 次）===")
    return _probe_workflow()


def _parses(text: str) -> bool:
    import json

    try:
        json.loads(text)
        return True
    except Exception:
        return False


def _probe_workflow() -> int:
    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vkals{suffix}"
    user_id = kb_id = session_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id
        kb = KnowledgeBase(user_id=user_id, name=f"临时稳定性探针-{suffix}")
        db.add(kb)
        db.flush()
        kb_id = kb.id
        session = SessionModel(user_id=user_id, title="临时探针会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id

        started = time.perf_counter()
        try:
            state = LearningWorkflowService(db).start(
                user_id=user_id,
                session_id=session_id,
                request_text="针对聚簇索引与二级索引的区别出 3 道题",
                question_count=3,
                knowledge_base_id=kb_id,
                context_mode="outline",
                divergence=3,
                # 第 23 轮：难度不传会先出参数卡而不是出题（探针要的是真出题）
                difficulty="进阶",
                provider="deepseek",
            )
            quiz = state.get("quiz") or {}
            print(
                f"  ✅ 成功：耗时 {time.perf_counter() - started:.1f}s，"
                f"题数={len(quiz.get('questions') or [])}"
            )
            return 0
        except Exception as exc:
            print(f"  ❌ 失败：耗时 {time.perf_counter() - started:.1f}s | {type(exc).__name__}: {exc}")
            return 1
    finally:
        try:
            if user_id:
                practice_ids = list(
                    db.scalars(select(PracticeSet.id).where(PracticeSet.user_id == user_id))
                )
                if practice_ids:
                    db.execute(
                        sql_delete(Question).where(Question.practice_set_id.in_(practice_ids))
                    )
                    db.execute(sql_delete(PracticeSet).where(PracticeSet.id.in_(practice_ids)))
                db.execute(sql_delete(WorkflowRun).where(WorkflowRun.user_id == user_id))
                db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                db.execute(sql_delete(SessionModel).where(SessionModel.user_id == user_id))
                db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"  已清理临时数据：user={user_id}")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"  清理失败：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
