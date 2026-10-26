"""验收：**删除知识库**之后，其它相关数据各自变成什么样（删库全链路，离线可复跑）。

为什么单开一个脚本：这是全仓**唯一一条没有任何断言覆盖**的破坏性链路——
`KnowledgeBaseService.delete` 会解绑资料与练习、硬删掌握度、把弱点改成"通用"、
最后删库行，还要在 `IntegrityError` 时回 409。文档阅读那条链路有 35+23+58+17 条断言，
删库这条此前一条都没有（见交接文档 §M.3①）。

对照口径来自 `TechSpar-领域删除行为-设计参照.md`（它删"领域"时的行为记录）。
我们的取舍与它**基本一致**（写侧最小、历史事实保留），只有一处相反：
**它的掌握度保留，我们是硬删**。本脚本把这个分歧也钉成断言，
这样将来要改口径时，先红的一定是这里。

本脚本对自己的定位是**现状契约**，不是"理想行为"：断言写的是"现在是什么样、
以及为什么"。任何一条要改，都得先回答"那掌握度/孤儿资料怎么办"。

离线：不调 LLM、不调 embedding（`_embed_query` 被 monkeypatch），
但 Qdrant 是**真写**的——否则"向量到底留没留"根本验不到。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_delete.py
    python scripts/verify_kb_delete.py --keep   # 不清理，方便手工看数据
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.agent.tools.practice import list_practice_history
from app.agent.tools.profile import get_mastery, get_weak_points
from app.db.database import SessionLocal
from app.db.models import (
    KnowledgeBase,
    KnowledgeChunk,
    PracticeSet,
    Resource,
    User,
    UserKnowledgeMastery,
    UserProfile,
)
from app.rag.core import COLLECTION_KNOWLEDGE, EMBEDDING_DIM, _get_qdrant_client, _upsert_chunks
from app.security import hash_password
from app.services.knowledge_base_service import KnowledgeBaseService

TEMP_PASSWORD = "verify_pass_123"

# 单位向量：Qdrant 用 COSINE，查询向量与入库向量都取它 → 相似度恒为 1，不受打分阈值影响
UNIT_QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIM - 1)


def _hit_count(payload: str) -> int:
    """`search_material` 返回的是 JSON 字符串，解析出命中数（解析不了就是 0）。"""
    try:
        return int(json.loads(payload).get("count") or 0)
    except (TypeError, ValueError):
        return 0


def _seed_resource(db, *, kb_id: int, user_id: int, name: str, chunks: list[str]) -> tuple[Resource, list[str]]:
    """造一份带真实切片 + 真实 Qdrant 向量的资料（不碰 COS：storage_path 留空）。"""
    resource = Resource(
        resource_type=0,
        storage_scene=0,
        upload_purpose=0,
        file_name=name,
        file_hash=f"kbd{int(time.time() * 1000) % 10**10}".ljust(32, "0")[:32],
        storage_path="",
        knowledge_base_id=kb_id,
        ingest_status="ready",
        user_id=user_id,
    )
    db.add(resource)
    db.flush()

    count, vector_ids = _upsert_chunks(
        chunks=list(chunks),
        vectors=[UNIT_QUERY_VECTOR for _ in chunks],
        user_id=user_id,
        resource_id=resource.id,
        file_name=name,
    )
    for index, text in enumerate(chunks):
        db.add(
            KnowledgeChunk(
                resource_id=resource.id,
                chunk_index=index,
                text=text,
                char_count=len(text),
                vector_id=vector_ids[index],
            )
        )
    db.commit()
    db.refresh(resource)
    return resource, list(vector_ids)


def _alive(vector_ids: list[str]) -> set[str]:
    if not vector_ids:
        return set()
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        return set()
    return {
        str(row.id)
        for row in client.retrieve(
            collection_name=COLLECTION_KNOWLEDGE, ids=vector_ids, with_payload=False
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="删除知识库的连带行为验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    passed = failed = 0

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name} {extra}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    suffix = str(int(time.time()))[-8:]
    username = f"vkbd{suffix}"  # users.username 只有 20 字符
    user_id = kb_id = practice_id = resource_id = None
    vector_ids: list[str] = []

    # 离线：查询向量固定成单位向量（入库向量也是它），避免外呼 DashScope
    import app.rag.retriever as retriever_module

    real_embed_query = retriever_module._embed_query
    retriever_module._embed_query = lambda _query: list(UNIT_QUERY_VECTOR)

    try:
        # ------------------------------------------------------------------ 造数据
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        knowledge_base = KnowledgeBase(user_id=user_id, name=f"临时删库验收库-{suffix}")
        db.add(knowledge_base)
        db.flush()
        kb_id = knowledge_base.id

        resource, vector_ids = _seed_resource(
            db,
            kb_id=kb_id,
            user_id=user_id,
            name="删库验收资料.md",
            chunks=["聚簇索引的叶子节点存整行数据。", "二级索引的叶子节点存主键值。"],
        )
        resource_id = resource.id

        # 考纲（库内 JSON，删库应随之消失）
        knowledge_base.quiz_core = {
            "title": "临时考纲",
            "sections": [
                {
                    "heading": "索引",
                    "topics": ["聚簇索引"],
                    "source_ref": {"resource_id": resource.id, "chunk_from": 0, "chunk_to": 1},
                }
            ],
        }

        practice = PracticeSet(
            user_id=user_id,
            knowledge_base_id=kb_id,
            title="删库验收练习",
            status="completed",
            total_score=80.0,
            question_count=2,
        )
        db.add(practice)
        db.flush()
        practice_id = practice.id

        # 掌握度（删库应**硬删**——这是与 TechSpar 相反的那一处）
        db.add(
            UserKnowledgeMastery(
                user_id=user_id,
                knowledge_base_id=kb_id,
                mastery_score=72.5,
                practice_count=3,
            )
        )

        # 画像：一条指向本库的弱点 + 一条本来就"通用"的弱点，用来看解绑是否精确
        db.add(
            UserProfile(
                user_id=user_id,
                weak_points=[
                    {
                        "point": "回表代价说不清",
                        "knowledge_base_id": kb_id,
                        "times_seen": 2,
                        "improved": False,
                        "archived": False,
                    },
                    {
                        "point": "通用弱点：表达不够结构化",
                        "knowledge_base_id": None,
                        "times_seen": 1,
                        "improved": False,
                        "archived": False,
                    },
                ],
            )
        )
        db.commit()
        print(f"临时用户 id={user_id}（{username}） 知识库 id={kb_id} 资料 id={resource.id}")

        # 删之前先确认"能被检索到"，否则后面的对比没有意义
        before = retriever_module.search_similar_chunks(
            query="索引", user_id=user_id, db=db, top_k=3
        )
        check("删库前：答疑能检索到这份资料的切片", len(before) >= 1, f"命中 {len(before)} 片")

        # ------------------------------------------------------------------ 真删
        print("\n[1] 删库返回值（前端靠它告诉用户发生了什么）")
        result = KnowledgeBaseService.delete(db, user_id=user_id, knowledge_base_id=kb_id)
        check("解绑的资料数正确", result["released_resource_count"] == 1, str(result))
        check("解绑的练习数正确", result["released_practice_count"] == 1, str(result))
        check("报告的删除掌握度数正确", result["removed_mastery_count"] == 1, str(result))
        # 保留策略随响应返回：调用方不该靠读源码猜"什么被留、什么被删"
        check(
            "★ 返回值带保留策略（四项口径齐全）",
            {part.strip() for part in result["retention_policy"].split(";")} == {
                "resources_unbound",
                "practice_history_kept",
                "weak_points_generalized",
                "mastery_removed",
            },
            result["retention_policy"],
        )
        check(
            "字段清单与前端 TS 约定一致",
            set(result) == {
                "knowledge_base_id", "released_resource_count", "released_practice_count",
                "removed_mastery_count", "retention_policy",
            },
            str(sorted(result)),
        )

        print("\n[2] 挂载点没了：库不可读")
        try:
            KnowledgeBaseService.get_detail(db, user_id=user_id, knowledge_base_id=kb_id)
            check("删库后详情应 404", False)
        except Exception as exc:  # noqa: BLE001
            check("删库后详情应 404", "知识库不存在" in str(exc), type(exc).__name__)

        print("\n[3] 资料与切片**保留**（对应参照文档「知识文件留在磁盘」）")
        db.rollback()  # 结束事务，否则读不到接口刚提交的状态（坑 #8）
        resource_row = db.get(Resource, resource.id)
        check("资料行还在", resource_row is not None)
        check("已解绑（knowledge_base_id 置 NULL）", resource_row.knowledge_base_id is None,
              str(resource_row.knowledge_base_id if resource_row else None))
        chunk_rows = db.scalar(
            select(func.count()).select_from(KnowledgeChunk)
            .where(KnowledgeChunk.resource_id == resource.id)
        )
        check("切片行还在（答疑仍要读到原文）", chunk_rows == 2, f"{chunk_rows} 片")
        check("Qdrant 向量点还在（没被删库顺手清掉）",
              _alive(vector_ids) == set(vector_ids), f"{len(_alive(vector_ids))}/{len(vector_ids)}")

        print("\n[4] 弱点：指向本库的改成「通用」，本来就通用的不动")
        db.rollback()
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        points = list(profile.weak_points or [])
        check("弱点条数不变（只解绑不删除）", len(points) == 2, str(len(points)))
        check("指向本库的弱点已解绑", points[0]["knowledge_base_id"] is None,
              str(points[0].get("knowledge_base_id")))
        check("本来就通用的弱点没被动过", points[1]["knowledge_base_id"] is None)
        weak_payload = get_weak_points.invoke(
            {"knowledge_base_id": None, "ctx": {"db": db, "user_id": user_id}}
        )
        check("弱点仍对模型可见（继续参与注入，同参照文档 §3）",
              "回表代价说不清" in weak_payload, weak_payload[:80])

        print("\n[5] 练习历史保留（同参照文档「训练会话保留」）")
        db.rollback()
        practice_row = db.get(PracticeSet, practice_id)
        check("练习行还在", practice_row is not None)
        check("已解绑（knowledge_base_id 置 NULL）", practice_row.knowledge_base_id is None,
              str(practice_row.knowledge_base_id if practice_row else None))
        history_payload = list_practice_history.invoke(
            {"knowledge_base_id": None, "limit": 10, "ctx": {"db": db, "user_id": user_id}}
        )
        check("练习历史仍可查（含这场）", "删库验收练习" in history_payload, history_payload[:80])

        print("\n[6] 掌握度：**硬删**——与 TechSpar 相反的那一处，这里钉住现状")
        db.rollback()
        remaining_mastery = db.scalar(
            select(func.count()).select_from(UserKnowledgeMastery)
            .where(UserKnowledgeMastery.user_id == user_id)
        )
        check("掌握度行已删除", remaining_mastery == 0, f"剩 {remaining_mastery} 行")
        mastery_payload = get_mastery.invoke(
            {"knowledge_base_id": kb_id, "ctx": {"db": db, "user_id": user_id}}
        )
        check("按已删库查掌握度为「无数据」（不是 0 分）",
              "无" in mastery_payload or "[]" in mastery_payload or "暂无" in mastery_payload,
              mastery_payload[:80])

        print("\n[7] ★ 解绑后的资料：答疑**照样检索得到**，但用户没有任何入口（缺口②现状）")
        after = retriever_module.search_similar_chunks(
            query="索引", user_id=user_id, db=db, top_k=3
        )
        check("★ 不带库范围时仍检索得到（孤儿资料事实上仍在使用）",
              len(after) >= 1, f"命中 {len(after)} 片")
        check("命中片段带得出解绑后的 resource_id",
              bool(after) and after[0].get("resource_id") == resource.id,
              str(after[0].get("resource_id") if after else None))
        visible = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id
        ) if False else None  # 库已不存在，列表接口本身就会 404（下面显式断言）
        del visible
        try:
            KnowledgeBaseService.list_resources(db, user_id=user_id, knowledge_base_id=kb_id)
            check("库内文档列表按库过滤 → 已删库直接 404", False)
        except Exception as exc:  # noqa: BLE001
            check("库内文档列表按库过滤 → 已删库直接 404", "知识库不存在" in str(exc), type(exc).__name__)
        # 用户侧的唯一入口是"按库列出文档"，而它必须传 knowledge_base_id。
        # 所以只要资料不属于任何库，就没有任何列表能列出它 —— 库侧也确实查不到它了。
        unbound = list(
            db.scalars(
                select(Resource).where(
                    Resource.user_id == user_id, Resource.knowledge_base_id.is_(None)
                )
            )
        )
        check(
            "资料确实不属于任何库（而文档列表必须按库查）→ 用户侧无可见入口",
            [row.id for row in unbound] == [resource.id],
            f"未归档资料 {[row.id for row in unbound]}",
        )

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        retriever_module._embed_query = real_embed_query
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} kb={kb_id} resource={resource_id}")
        else:
            # ⚠️ 顺序：**先删向量点，再删 MySQL 行**（坑 #27），否则中途失败会留下没人认领的孤儿点
            if vector_ids:
                try:
                    from qdrant_client.models import PointIdsList

                    _get_qdrant_client().delete(
                        collection_name=COLLECTION_KNOWLEDGE,
                        points_selector=PointIdsList(points=vector_ids),
                        wait=True,
                    )
                    print(f"已清理临时向量点 {len(vector_ids)} 个")
                except Exception as exc:  # noqa: BLE001
                    print(f"临时向量点清理失败（可用 cleanup_orphan_vectors.py 兜底）：{exc}")
            try:
                if user_id:
                    # 库在删库步骤已经没了；这里清残留（资料/切片/练习/画像/掌握度/用户）
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
                    db.execute(sql_delete(PracticeSet).where(PracticeSet.user_id == user_id))
                    db.execute(
                        sql_delete(UserKnowledgeMastery).where(
                            UserKnowledgeMastery.user_id == user_id
                        )
                    )
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id} kb={kb_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
