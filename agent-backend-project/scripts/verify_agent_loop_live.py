"""真模型冒烟：验证"真模型下 loop 会不会正确选工具/委派"。

**为什么必须有这一套**：`verify_agent_loop*.py` 全程离线（假模型），只证明了**编排逻辑**正确，
证明不了"模型看着这份提示词与这些工具描述，会不会做出正确选择"。那是这一期唯一没被覆盖的风险。

**这一套会真的调用 LLM（花钱、联网）**，所以：
- 默认 **dry-run**，只打印将要发的问题，加 `--yes` 才真跑；
- 只跑 3 个问题，`max_tokens` 走项目既有配置；
- 断言"关键的调用发生了"，而不是"选了哪几个工具"——真模型的选择有合理波动，
  把断言写死成唯一答案只会变成 flaky 测试；
- 临时账号 + 临时知识库自建自清（踩坑记录第 21、26、27 条）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_agent_loop_live.py            # dry-run：只打印计划
    python scripts/verify_agent_loop_live.py --yes      # 真跑（会调用 LLM）
    python scripts/verify_agent_loop_live.py --yes --keep
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.learning_partner import LearningPartnerService
from app.core.settings import settings
from app.db.database import SessionLocal
from app.db.models import (
    ChatMessage,
    KnowledgeBase,
    KnowledgeChunk,
    Note,
    PracticeSet,
    Question,
    Resource,
    Session as SessionModel,
    User,
    UserKnowledgeMastery,
    UserProfile,
    WorkflowRun,
)
from app.security import hash_password

TEMP_PASSWORD = "verify_pass_123"

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


# 三个探针问题的判据刻意**宽松**：真模型的工具选择有合理波动，
# 把断言写死成唯一答案只会产出 flaky 测试。我们只断言"关键动作发生了"。
PROBES: list[dict[str, Any]] = [
    {
        "name": "① 简单概念问答：可以检索资料，但**不该委派 worker**",
        # 注意：这里**不是**"不该调任何工具"。这是个 RAG 系统，模型去检索用户的库
        # 是正确行为（第一次写探针时我把它误判成"乱调工具"，实际是探针期望错了）。
        # 真正该约束的是：一句概念问题不需要惊动出题/评测/归档这些 worker。
        "text": "什么是聚簇索引？用两句话说明白就行。",
        "expect_any_tool": False,
        "expect_worker": (),
        "forbid_worker": True,
    },
    {
        "name": "② 查画像：应当调用查掌握度或薄弱点的工具",
        "text": "帮我看看我现在的掌握度和薄弱点是什么情况。",
        "expect_any_tool": True,
        "expect_tool_in": ("get_mastery", "get_weak_points", "list_practice_history"),
        "expect_worker": (),
    },
    {
        "name": "③ 多步委派：应当委派 worker（出题/讲解/归档 任一）",
        # 这条是这一期最关键的验收：多步意图必须真的走"委派关系"，
        # 而不是模型自己用原子工具糊一遍。
        "text": "先看我哪里弱，然后针对弱点出一套练习题，最后把这次安排归档成笔记。",
        "expect_any_tool": True,
        "expect_worker": ("delegate_quiz", "delegate_explain", "delegate_archive"),
    },
]


def main() -> int:
    parser = argparse.ArgumentParser(description="第 2 期 loop 的真模型冒烟")
    parser.add_argument("--yes", action="store_true", help="确认真调用 LLM（不加则只 dry-run）")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    print("=" * 78)
    print("真模型冒烟：验证 loop 在真模型下的工具/委派选择")
    print(f"model={settings.deepseek_model}  base_url={settings.deepseek_base_url}")
    print(f"api_key 已配置：{bool(settings.deepseek_api_key)}")
    print("=" * 78)
    for probe in PROBES:
        print(f"\n{probe['name']}\n  问：{probe['text']}")

    if not args.yes:
        print(
            "\n（dry-run）以上 3 个问题会真的调用 LLM。确认后加 --yes 执行：\n"
            "    python scripts/verify_agent_loop_live.py --yes\n"
        )
        return 0

    if not settings.deepseek_api_key:
        print("\n❌ 未配置 DEEPSEEK_API_KEY，无法真跑")
        return 1

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vkalv{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = session_id = None

    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        kb = KnowledgeBase(user_id=user_id, name=f"临时真跑库-{suffix}")
        db.add(kb)
        db.flush()
        kb_id = kb.id

        # 造一份带切片的资料，让 search_material 有东西可检索
        resource = Resource(
            resource_type=0,
            storage_scene=0,
            upload_purpose=0,
            file_name="索引与事务.md",
            file_hash=f"lv{int(time.time() * 1000) % 10**10}".ljust(32, "0")[:32],
            storage_path="",
            knowledge_base_id=kb_id,
            ingest_status="ready",
            user_id=user_id,
            content_text="聚簇索引的叶子节点存放整行数据，二级索引的叶子节点存放主键值。",
        )
        db.add(resource)
        db.flush()

        db.add(
            KnowledgeChunk(
                resource_id=resource.id,
                chunk_index=0,
                text="聚簇索引的叶子节点存放整行数据；二级索引的叶子节点存放主键值，需要回表。",
                char_count=40,
                # vector_id 非空：用真 UUID（模型列约定），但**不往 Qdrant 写点**——
                # 本脚本验的是"模型会不会选工具"，不是检索质量；少写一份向量就少一处要清理的残留。
                vector_id=str(uuid.uuid4()),
            )
        )
        db.add(
            UserProfile(
                user_id=user_id,
                weak_points=[
                    {
                        "point": "聚簇索引与二级索引的区别",
                        "knowledge_base_id": kb_id,
                        "times_seen": 3,
                        "axis": "knowledge",
                    }
                ],
            )
        )
        session = SessionModel(user_id=user_id, title="临时真跑会话", session_model=0)
        db.add(session)
        db.commit()
        session_id = session.id
        print(f"\n临时用户 id={user_id}（{username}） 知识库 id={kb_id} 会话 id={session_id}")

        for probe in PROBES:
            print(f"\n{probe['name']}")
            started = time.perf_counter()
            service = LearningPartnerService(db, provider="deepseek", session_model=0)
            generator = service.run_events(
                user_id=user_id,
                session_id=session_id,
                request_text=probe["text"],
                knowledge_base_id=kb_id,
            )
            events: list[dict[str, Any]] = []
            result = None
            while True:
                try:
                    events.append(next(generator))
                except StopIteration as stop:
                    result = stop.value
                    break
            elapsed = time.perf_counter() - started

            tool_names = [
                event["name"] for event in events if event["type"] == "tool_call"
            ]
            worker_names = [name for name in tool_names if name.startswith("delegate_")]
            print(f"  耗时 {elapsed:.1f}s | 步数={len(result.steps)} | 工具调用={tool_names}")
            print(f"  stop_reason={result.stop_reason}")
            print(f"  回答：{(result.answer or '')[:160].replace(chr(10), ' ')}")
            if probe.get("expect_worker"):
                full = (result.answer or "").replace("\n", " ")
                print(f"  【③ 全文】{full[:700]}")

            tool_result = [
                event for event in events if event["type"] == "tool_result"
            ]
            for event in tool_result:
                print(
                    f"    [{event['name']}] ok={event['ok']} "
                    f"{event.get('elapsed_ms')}ms :: {str(event.get('result_preview'))[:150]}"
                )

            if probe.get("expect_any_tool"):
                check(f"{probe['name']} —— 发生了工具调用", bool(tool_names), str(tool_names))
            elif probe.get("forbid_worker"):
                check(
                    f"{probe['name']} —— 未委派 worker",
                    not worker_names,
                    f"却委派了 {worker_names}",
                )
            else:
                check(
                    f"{probe['name']} —— 未乱调工具",
                    not tool_names,
                    f"却调了 {tool_names}",
                )

            expected_tools = probe.get("expect_tool_in") or ()
            if expected_tools:
                check(
                    f"{probe['name']} —— 调到了相关工具",
                    any(name in expected_tools for name in tool_names),
                    f"期望之一 {expected_tools}，实际 {tool_names}",
                )

            expected_workers = probe.get("expect_worker") or ()
            if expected_workers:
                check(
                    f"{probe['name']} —— 委派了 worker",
                    any(name in expected_workers for name in worker_names),
                    f"期望之一 {expected_workers}，实际 {worker_names}",
                )

            check(f"{probe['name']} —— 给出了最终回答", bool((result.answer or "").strip()))
            check(
                f"{probe['name']} —— 没有跑满步数（说明正常收敛）",
                result.stop_reason != "max_steps",
                result.stop_reason,
            )

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    # 删除顺序必须满足外键：workflow_runs.session_id → sessions，
                    # 所以工作流运行记录要在删会话之前清掉（委派成功后会真的建 run）。
                    db.execute(sql_delete(WorkflowRun).where(WorkflowRun.user_id == user_id))
                    # 再删有外键依赖的行，最后删父表（本项目没有 ORM relationship）
                    practice_ids = list(
                        db.scalars(select(PracticeSet.id).where(PracticeSet.user_id == user_id))
                    )
                    if practice_ids:
                        db.execute(
                            sql_delete(Question).where(Question.practice_set_id.in_(practice_ids))
                        )
                        db.execute(sql_delete(PracticeSet).where(PracticeSet.id.in_(practice_ids)))
                    db.execute(sql_delete(Note).where(Note.user_id == user_id))
                    db.execute(sql_delete(ChatMessage).where(ChatMessage.user_id == user_id))
                    db.execute(
                        sql_delete(UserKnowledgeMastery).where(
                            UserKnowledgeMastery.user_id == user_id
                        )
                    )
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    resource_ids = list(
                        db.scalars(select(Resource.id).where(Resource.user_id == user_id))
                    )
                    if resource_ids:
                        db.execute(
                            sql_delete(KnowledgeChunk).where(
                                KnowledgeChunk.resource_id.in_(resource_ids)
                            )
                        )
                        db.execute(sql_delete(Resource).where(Resource.id.in_(resource_ids)))
                    db.execute(
                        sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id)
                    )
                    db.execute(sql_delete(SessionModel).where(SessionModel.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
