"""pytest 共享夹具：真 MySQL 会话 + 临时用户（自建自清）。

**为什么用真 MySQL 而不是 mock**：工具与工作流的行为取决于真实的行与约束（弱点 JSON 列、
练习历史排序、笔记来源唯一键、知识库作用域过滤），把 `db` mock 掉等于把被测对象换成了
假想实现。代价是清理必须可靠——teardown 按 `user_id` **自底向上**删派生行，再删用户本身。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import delete, select

# 与 test_smoke.py 同一约定：把项目根目录加入导入路径，避免 `No module named 'app'`。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import (
    Attempt,
    ChatMessage,
    KnowledgeBase,
    KnowledgeChunk,
    Note,
    PracticeSet,
    Question,
    Resource,
    Session,
    User,
    UserKnowledgeMastery,
    UserProfile,
    WorkflowRun,
)
from app.security import hash_password


@pytest.fixture()
def db():
    """真 MySQL 会话（用完关闭；不替测试做提交，提交由用例自己决定）。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def temp_user(db):
    """临时用户：测试结束连同它名下所有派生行一起删除。"""
    user = User(username=f"eval_{uuid.uuid4().hex[:10]}", password=hash_password("eval123456"))
    db.add(user)
    db.commit()
    try:
        yield user
    finally:
        _purge_user(db, user.id)


def _purge_user(db, user_id: int) -> None:
    """按 FK 依赖自底向上清理：子表 -> 父表 -> users。

    多删一次比留下脏数据好：临时用户是随机名，误删到别人的行的前提是 `user_id` 撞了
    别人的行——那说明夹具本身错了，应当立刻炸出来而不是静默通过。
    """
    practice_ids = select(PracticeSet.id).where(PracticeSet.user_id == user_id)
    resource_ids = select(Resource.id).where(Resource.user_id == user_id)
    try:
        db.execute(delete(Attempt).where(Attempt.user_id == user_id))
        db.execute(delete(Question).where(Question.practice_set_id.in_(practice_ids)))
        db.execute(delete(PracticeSet).where(PracticeSet.user_id == user_id))
        db.execute(delete(KnowledgeChunk).where(KnowledgeChunk.resource_id.in_(resource_ids)))
        db.execute(delete(Resource).where(Resource.user_id == user_id))
        db.execute(delete(Note).where(Note.user_id == user_id))
        db.execute(delete(WorkflowRun).where(WorkflowRun.user_id == user_id))
        db.execute(delete(ChatMessage).where(ChatMessage.user_id == user_id))
        db.execute(delete(Session).where(Session.user_id == user_id))
        # 掌握度引用 knowledge_bases（FK），必须排在 KnowledgeBase 之前
        db.execute(delete(UserKnowledgeMastery).where(UserKnowledgeMastery.user_id == user_id))
        db.execute(delete(UserProfile).where(UserProfile.user_id == user_id))
        db.execute(delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
        db.execute(delete(User).where(User.id == user_id))
        db.commit()
    except Exception:
        db.rollback()
        raise
