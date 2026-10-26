"""最小复现：真模型下既有「出题」工作流能否跑通（判断 max_tokens 是否不够）。

背景：真模型冒烟里 loop 委派 delegate_quiz 连续 3 次失败，模型转述的错误是
「JSON 字符串未闭合」与「模型没有返回内容」——这两条都像**输出被 max_tokens 截断**。
本探针直接调用既有的 LearningWorkflowService（不经 loop），把真实异常打出来。

只跑 1 次，报错的 traceback 会明确指向解析失败还是别的。

跑法：python scripts/probe_quiz_generation.py
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
    print(f"model={settings.deepseek_model} max_tokens={settings.deepseek_max_tokens}")
    print(f"temperature={settings.deepseek_temperature} base_url={settings.deepseek_base_url}")

    # 抓原始输出：区分「被 max_tokens 截断」与「JSON 本身畸形」需要看尾巴，
    # 只看异常信息两者长得一样（都是 Expecting ',' delimiter）。
    # 注入点是 `LearningWorkflowService(llm=...)`：不传时出题节点才自己建模型。
    from app.agent.llm import get_chat_model

    raws: list[str] = []
    inner_model = get_chat_model(provider="deepseek")

    class _Capture:
        def __init__(self, inner: Any):
            self._inner = inner

        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            message = self._inner.invoke(*args, **kwargs)
            raws.append(str(getattr(message, "content", "")))
            return message

        def stream(self, *args: Any, **kwargs: Any) -> Any:
            acc = ""
            for chunk in self._inner.stream(*args, **kwargs):
                acc += str(getattr(chunk, "content", "") or "")
                yield chunk
            raws.append(acc)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vkalq{suffix}"
    user_id = kb_id = session_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id
        kb = KnowledgeBase(user_id=user_id, name=f"临时出题探针-{suffix}")
        db.add(kb)
        db.flush()
        kb_id = kb.id
        session = SessionModel(user_id=user_id, title="临时出题会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id
        print(f"临时 user={user_id} kb={kb_id} session={session_id}\n")

        started = time.perf_counter()
        try:
            state = LearningWorkflowService(db, llm=_Capture(inner_model)).start(
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
            elapsed = time.perf_counter() - started
            quiz = state.get("quiz") or {}
            questions = quiz.get("questions") or []
            print(f"✅ 出题成功：耗时 {elapsed:.1f}s，题数={len(questions)}，run_id={state.get('run_id')}")
            for index, item in enumerate(questions[:3], start=1):
                stem = str(item.get("stem") or item.get("question") or "")[:70]
                print(f"   {index}. {stem}")
            return 0
        except Exception as exc:
            elapsed = time.perf_counter() - started
            print(f"❌ 出题失败：耗时 {elapsed:.1f}s")
            print(f"   类型：{type(exc).__name__}")
            print(f"   内容：{exc}")
            if raws:
                raw = raws[-1]
                print(f"\n   [原始输出] 长度={len(raw)} 字符")
                print(f"   结尾 120 字符：{raw[-120:]!r}")
                print(
                    "   判断："
                    + (
                        "疑似被 max_tokens 截断（结尾没有闭合括号，且长度接近上限）"
                        if not raw.rstrip().endswith(("}", "```"))
                        else "结尾有闭合括号 → 更像 JSON 语法畸形而非截断"
                    )
                )
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
                print(f"\n已清理临时数据：user={user_id}")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"清理失败：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
