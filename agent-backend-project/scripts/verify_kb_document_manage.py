"""验收：库内文档的**管理**操作（重命名 / 移动分组 / 删除），全部在临时数据上做。

为什么单开一个脚本：重命名与删除会**改数据和删文件**，不能拿真实资料试。这里自建一套
临时用户 + 临时知识库 + 临时资料（切片是造的，不碰 Qdrant 索引，删除前也不依赖它），
跑完把临时数据清干净。

关键取舍：删"复制的资料"时传 `delete_file=False`——它的 `storage_path` 指向的是**源资料
共用的那个 COS 对象**，真删就会把真实文件删掉。原文件删除链路单独用一个临时上传的小对象验证。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_document_manage.py
    python scripts/verify_kb_document_manage.py --keep   # 不清理，方便手工看数据
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource, User
from app.security import hash_password
from app.services.cos_storage_service import CosStorageService
from app.services.knowledge_base_service import KnowledgeBaseService

TEMP_PASSWORD = "verify_pass_123"


def _make_resource(db, *, kb_id: int, user_id: int, name: str, chunks: list[str]) -> Resource:
    """造一份带切片的临时资料。

    `vector_id` 用**真 UUID**：Qdrant 的 `points` 只接受 UUID/整数，一个非法 id 会让整批
    删除请求 400（实测过）。这里保持和 `app/rag/core.py` 的 `uuid.uuid4()` 一致，
    才能真正验证"删除时连向量一起清"。切片文本用假的，只验证增删改查与分组。
    """
    resource = Resource(
        resource_type=0,
        storage_scene=0,
        upload_purpose=0,
        file_name=name,
        file_hash=f"mg{int(time.time() * 1000) % 10**10}".ljust(32, "0")[:32],
        storage_path="",  # 无 COS 路径：删除时不会去碰对象存储
        knowledge_base_id=kb_id,
        ingest_status="ready",
        user_id=user_id,
    )
    db.add(resource)
    db.flush()
    for index, text in enumerate(chunks):
        db.add(
            KnowledgeChunk(
                resource_id=resource.id,
                chunk_index=index,
                text=text,
                char_count=len(text),
                vector_id=str(uuid.uuid4()),
            )
        )
    db.commit()
    db.refresh(resource)
    return resource


def main() -> int:
    parser = argparse.ArgumentParser(description="库内文档管理验收")
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
    username = f"vkmg{suffix}"  # 注意 users.username 只有 20 字符
    user_id = kb_id = None
    cos_client = None
    cos_probe_path = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id
        knowledge_base = KnowledgeBase(user_id=user_id, name=f"临时管理验收库-{suffix}")
        db.add(knowledge_base)
        db.flush()
        kb_id = knowledge_base.id
        print(f"临时用户 id={user_id}（{username}） 知识库 id={kb_id}")

        alpha = _make_resource(
            db, kb_id=kb_id, user_id=user_id, name="索引原理.md",
            chunks=["聚簇索引的叶子节点存整行数据。", "二级索引的叶子节点存主键值。"],
        )
        beta = _make_resource(
            db, kb_id=kb_id, user_id=user_id, name="事务隔离级别.md",
            chunks=["REPEATABLE READ 下快照读不加锁。", "间隙锁用于防止幻读。"],
        )

        print("\n[1] 重命名（功能 1）")
        updated = KnowledgeBaseService.update_resource(
            db, user_id=user_id, resource_id=alpha.id, changes={"file_name": "B+树与索引.md"}
        )
        check("改名生效", updated["file_name"] == "B+树与索引.md", updated["file_name"])
        check("正文不受影响", updated["text_chars"] > 0)
        check("切片数不变", updated["chunk_count"] == 2, f"{updated['chunk_count']} 片")
        check(
            "库里能搜到新名字",
            any(
                row["id"] == alpha.id
                for row in KnowledgeBaseService.list_resources(
                    db, user_id=user_id, knowledge_base_id=kb_id, keyword="B+树"
                )
            ),
        )
        # 搜正文（切片里的真词），验证 keyword 走的是"文件名 OR 正文"
        body_hits = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, keyword="间隙锁"
        )
        check("按正文能搜到", len(body_hits) == 1 and body_hits[0]["id"] == beta.id,
              f"命中 {len(body_hits)} 份")
        if body_hits:
            check("正文命中带 match_field=content", body_hits[0]["match_field"] == "content")
            check("正文命中带片段", any("间隙锁" in s for s in body_hits[0]["match_snippets"]),
                  str(body_hits[0]["match_snippets"][:1]))
        # LIKE 通配符必须转义：搜 % 不能命中全库
        wildcard_hits = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, keyword="%"
        )
        check("搜 % 不会命中全库", wildcard_hits == [], f"命中 {len(wildcard_hits)} 份")
        try:
            KnowledgeBaseService.update_resource(
                db, user_id=user_id, resource_id=alpha.id, changes={"file_name": "   "}
            )
            check("空文件名应报错", False)
        except Exception as exc:  # noqa: BLE001
            check("空文件名应报错", "不能为空" in str(exc), type(exc).__name__)

        print("\n[2] 分组（功能 2）")
        KnowledgeBaseService.update_resource(
            db, user_id=user_id, resource_id=alpha.id, changes={"group_name": "数据库"}
        )
        KnowledgeBaseService.update_resource(
            db, user_id=user_id, resource_id=beta.id, changes={"group_name": "数据库"}
        )
        groups = KnowledgeBaseService.list_groups(db, user_id=user_id, knowledge_base_id=kb_id)
        db_group = next((row for row in groups if row["name"] == "数据库"), None)
        check("分组出现", db_group is not None, str(groups))
        check("组内文档数正确", bool(db_group) and db_group["resource_count"] == 2)
        in_group = KnowledgeBaseService.list_resources(
            db, user_id=user_id, knowledge_base_id=kb_id, group_name="数据库"
        )
        check("按分组过滤只返回组内文档", len(in_group) == 2, f"{len(in_group)} 份")
        check(
            "搜不存在的分组返回空",
            KnowledgeBaseService.list_resources(
                db, user_id=user_id, knowledge_base_id=kb_id, group_name="不存在的组"
            )
            == [],
        )

        print("\n[3] 改名 / 改分组不影响切片（出题与答疑仍能检索到）")
        check(
            "切片行数没被动过",
            db.scalar(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.resource_id == alpha.id)
            )
            == 2,
        )

        print("\n[4] 删除（功能 3）：切片一并删除，且不误删共用对象")
        # 先抓标量：commit 之后 ORM 实例会过期，已删的 alpha 再读 .id 会抛 ObjectDeletedError
        alpha_id, beta_id = alpha.id, beta.id
        before_chunks = db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.resource_id == alpha.id)
        )
        result = KnowledgeBaseService.delete_resource(
            db, user_id=user_id, resource_id=alpha.id, delete_file=False
        )
        check("返回删除切片数", result["deleted_chunk_count"] == before_chunks, str(result))
        check("未删原文件（本次明确要求保留）", result["deleted_file"] is False)
        remain = db.scalar(
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(KnowledgeChunk.resource_id == alpha.id)
        )
        check("切片已清空", remain == 0)
        try:
            KnowledgeBaseService.get_resource_detail(db, user_id=user_id, resource_id=alpha.id)
            check("删除后详情应 404", False)
        except Exception as exc:  # noqa: BLE001
            check("删除后详情应 404", "不存在" in str(exc), type(exc).__name__)
        others = KnowledgeBaseService.list_resources(db, user_id=user_id, knowledge_base_id=kb_id)
        check("同库其它文档不受影响", len(others) == 1 and others[0]["id"] == beta.id)

        print("\n[5] 向量删除的健壮性：一个非法 point id 不能拖垮整批")
        from app.rag import delete_vectors_by_ids

        mixed = [str(uuid.uuid4()), "mg-not-a-uuid"]
        check("混合非法 id 时仍提交合法的那一个", delete_vectors_by_ids(mixed) == 1)
        check("全是非法 id 时返回 0（不抛错）", delete_vectors_by_ids(["mg-1", "mg-2"]) == 0)
        check("空列表返回 0", delete_vectors_by_ids([]) == 0)

        print("\n[6] 删除原文件链路（用专门的临时对象，绝不动真实文件）")
        try:
            from app.core.settings import settings as app_settings

            cos_client = CosStorageService.get_client()
            bucket_name = app_settings.cos_bucket
            object_name = f"verify/mg-{suffix}.txt"
            body = b"temporary object for delete verification"
            CosStorageService.put_object(
                cos_client, bucket_name, object_name, body, len(body), "text/plain"
            )
            cos_probe_path = f"cos://{bucket_name}/{object_name}"
            probe = _make_resource(
                db, kb_id=kb_id, user_id=user_id, name="临时删除验证.txt", chunks=["临时内容"]
            )
            probe.storage_path = cos_probe_path
            db.commit()
            result2 = KnowledgeBaseService.delete_resource(
                db, user_id=user_id, resource_id=probe.id, delete_file=True
            )
            check("原文件被删除", result2["deleted_file"] is True, str(result2))
        except Exception as exc:  # noqa: BLE001 - 对象存储不可用不该让整套验收失败
            print(f"  SKIP 原文件删除链路（COS 不可用或调用失败）：{exc}")
            cos_probe_path = None

        print("\n[7] 删资料必须同时摘掉它的考纲章节（并保留其它资料的章节）")
        # 只动 quiz_core 这个 JSON 列，不碰向量与切片，所以整段是离线的。
        # ⚠️ alpha 在 [4] 已被删除，它的 ORM 实例已过期——只能用删除前抓好的标量 alpha_id。
        from app.services.quiz_core_service import _merge_quiz_core

        delta = _make_resource(
            db, kb_id=kb_id, user_id=user_id, name="待删除的第三份.md", chunks=["这份要被删掉。"]
        )
        knowledge_base.quiz_core = {
            "title": "临时考纲",
            "sections": [
                {
                    "heading": "alpha 的章节",
                    "topics": ["聚簇索引"],
                    "source_ref": {"resource_id": alpha_id, "chunk_from": 0, "chunk_to": 1},
                    "asked_count": 3,
                },
                {
                    "heading": "beta 的章节",
                    "topics": ["间隙锁"],
                    "source_ref": {"resource_id": beta_id, "chunk_from": 0, "chunk_to": 0},
                    "asked_count": 0,
                },
                {
                    "heading": "待删资料的章节",
                    "topics": ["这份要没"],
                    "source_ref": {"resource_id": delta.id, "chunk_from": 0, "chunk_to": 0},
                    "asked_count": 5,
                },
                {"heading": "没有原文定位的章节", "topics": ["无来源"]},
            ],
        }
        db.commit()

        # 删之前先确认标注口径：alpha 已在 [4] 删掉 → missing；其余还在 → ok / unknown
        detail = KnowledgeBaseService.get_detail(db, user_id=user_id, knowledge_base_id=kb_id)
        sections = ((detail.get("quiz_core") or {}).get("sections")) or []
        statuses = [section.get("source_status") for section in sections]
        check(
            "详情接口给每个章节补了 source_status",
            statuses == ["missing", "ok", "ok", "unknown"],
            str(statuses),
        )
        db.refresh(knowledge_base)
        stored = (knowledge_base.quiz_core or {}).get("sections") or []
        check(
            "标注只加在响应里，没有写回库",
            all("source_status" not in section for section in stored),
            str([sorted(section.keys()) for section in stored]),
        )

        result3 = KnowledgeBaseService.delete_resource(
            db, user_id=user_id, resource_id=delta.id, delete_file=False
        )
        check("删除结果报告摘掉的章节数", result3["removed_quiz_sections"] == 1, str(result3))
        remaining = KnowledgeBaseService.get_detail(
            db, user_id=user_id, knowledge_base_id=kb_id
        )["quiz_core"]["sections"]
        headings = [section.get("heading") for section in remaining]
        check("待删资料的章节已被摘掉", "待删资料的章节" not in headings, str(headings))
        check("其它资料的章节照旧保留", headings == ["alpha 的章节", "beta 的章节", "没有原文定位的章节"], str(headings))
        check(
            "保留章节的记账（asked_count）没被顺手清掉",
            remaining[0]["asked_count"] == 3,
            str([section.get("asked_count") for section in remaining]),
        )
        # 重传拿到的是新 resource_id，永远补不回旧章节——这正是"必须删时清"的理由
        check(
            "alpha 的章节（资料已删）在摘 delta 时不动它",
            remaining[0]["source_ref"]["resource_id"] == alpha_id,
            str(remaining[0]["source_ref"]),
        )

        print("\n[7b] 同名章节跨资料合并后，删锚定的那份会连合并进来的考点一起丢（已知取舍）")
        merged_sample = _merge_quiz_core(
            {
                "title": "t",
                "sections": [
                    {
                        "heading": "索引入门",
                        "topics": ["聚簇索引"],
                        "source_ref": {"resource_id": alpha_id, "chunk_from": 0, "chunk_to": 0},
                        "asked_count": 2,
                    }
                ],
            },
            [
                {
                    "sections": [
                        {"heading": "索引入门", "topics": ["最左前缀"], "source_ref": {"resource_id": beta_id}}
                    ],
                    "frequently_tested": [],
                    "followup_paths": [],
                }
            ],
            resource_id=beta_id,
            title="t",
        )
        merged_section = (merged_sample.get("sections") or [])[0]
        check(
            "同名章节合并后 source_ref 只留首次创建者（alpha）",
            merged_section["source_ref"]["resource_id"] == alpha_id,
            str(merged_section["source_ref"]),
        )
        check(
            "合并进来的考点名词在同一个章节里",
            set(merged_section["topics"]) == {"聚簇索引", "最左前缀"},
            str(merged_section["topics"]),
        )
        pruned_sample = KnowledgeBaseService.prune_resource_sections(
            merged_sample, resource_id=alpha_id
        )
        check(
            "★ 删 alpha 会连 beta 合并进来的考点一起丢（数据结构无法区分来源，取舍见文档）",
            pruned_sample is not None and pruned_sample.get("sections") == [],
            str(pruned_sample),
        )

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} kb={kb_id}")
        else:
            try:
                if kb_id:
                    resource_ids = list(
                        db.scalars(select(Resource.id).where(Resource.knowledge_base_id == kb_id))
                    )
                    if resource_ids:
                        db.execute(
                            sql_delete(KnowledgeChunk).where(
                                KnowledgeChunk.resource_id.in_(resource_ids)
                            )
                        )
                        db.execute(sql_delete(Resource).where(Resource.id.in_(resource_ids)))
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
                if user_id:
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id} kb={kb_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
            # 万一临时对象没被删掉，这里兜底再删一次
            if cos_probe_path and cos_client is not None:
                try:
                    CosStorageService.delete_object_by_storage_path(cos_client, cos_probe_path)
                    print("兜底删除了临时 COS 对象")
                except Exception:  # noqa: BLE001
                    pass
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
