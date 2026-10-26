"""验收：**同一份资料重新入库**时不会留下"失联切片"（MySQL 的 vector_id 指向已删的向量点）。

背景（这是个实测复现过的真 bug，不是理论担心）：

`_upsert_chunks` 的覆盖语义是按 `(user_id, file_name)` **先删旧点、再写新点**，而新点拿的是
**新的 uuid**。于是同一份资料二次入库后：

- Qdrant 上旧点已被清掉；
- MySQL 的 `knowledge_chunks` 里那批老行仍指向旧 point id —— 而 `save_chunks` 只重写切片行，
  从不回头清 Qdrant。

后果：这些切片在检索里**永远回查不到原文**（`fetch_chunks_by_vector_ids` 查不到就丢弃），
等于"资料看着入库成功、实际检索不到内容"。修法：`RagService.ingest_from_bytes` 在
**新切片提交成功之后**，用入库前读到的旧 vector_id 把旧点删掉（顺序不能反，见该函数注释）。

这个脚本怎么验：**不调 embedding**（monkeypatch 掉 `rag_service.ingest_file` 返回固定结果），
但 Qdrant 是真写的——否则"旧向量到底删没删"根本验不到。外呼一次 DashScope 都不需要。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_reingest_stale_vectors.py
    python scripts/verify_reingest_stale_vectors.py --keep   # 不清理，方便手工看数据
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource, User
from app.rag.core import COLLECTION_KNOWLEDGE, EMBEDDING_DIM, _get_qdrant_client
from app.security import hash_password
from app.services import rag_service
from app.rag.core import IngestResult

FILE_NAME = "验收-重复入库.pdf"


def _fake_ingest(chunks: list[str], user_id: int, file_name: str):
    """造一个假的 `ingest_file`：**跳过解析与 embedding，但照常走 `_upsert_chunks` 真写 Qdrant**。

    为什么必须真写 Qdrant：这个 bug 的本质就是"Qdrant 上的点被换了、MySQL 没跟上"，
    如果这里把 Qdrant 也 mock 掉，"旧向量到底删没删、新向量在不在"就完全验不到了。
    真实 `_upsert_chunks` 会按 `(user_id, file_name)` 先删旧点再写新点——这正是要覆盖的语义。
    """

    def _inner(*, file_path, user_id=None, original_file_name=None, resource_id=None):  # noqa: ANN001, ANN202
        from app.rag.core import _upsert_chunks

        name = original_file_name or file_name
        count, vector_ids = _upsert_chunks(
            chunks=list(chunks),
            vectors=[[0.0] * EMBEDDING_DIM for _ in chunks],
            user_id=user_id,
            resource_id=resource_id,
            file_name=name,
        )
        return IngestResult(
            file_name=name,
            chunk_count=count,
            chunks=list(chunks),
            vector_ids=vector_ids,
            text="\n\n".join(chunks),
        )

    return _inner


def _alive(vector_ids: list[str]) -> set[str]:
    if not vector_ids:
        return set()
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        return set()
    return {
        str(r.id)
        for r in client.retrieve(
            collection_name=COLLECTION_KNOWLEDGE, ids=vector_ids, with_payload=False
        )
    }


def _payloads(vector_ids: list[str]) -> list[dict]:
    """取回这些点的 payload（用来断言 resource_id 真的写进去了）。"""
    if not vector_ids:
        return []
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        return []
    return [
        dict(r.payload or {})
        for r in client.retrieve(
            collection_name=COLLECTION_KNOWLEDGE, ids=vector_ids, with_payload=True
        )
    ]


def _ensure_collection() -> None:
    client = _get_qdrant_client()
    if not client.collection_exists(COLLECTION_KNOWLEDGE):
        from qdrant_client.models import Distance, VectorParams

        client.create_collection(
            collection_name=COLLECTION_KNOWLEDGE,
            vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="重复入库的失联切片验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    passed = failed = 0
    suffix = str(int(time.time()))[-8:]
    user_id = resource_id = None
    old_ids: list[str] = []
    new_ids: list[str] = []
    newest_ids: list[str] = []
    extra_ids: list[str] = []
    client = _get_qdrant_client()

    def check(name: str, cond: bool, extra: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name} {extra}")
        else:
            failed += 1
            print(f"  FAIL {name} {extra}")

    real_ingest = rag_service.ingest_file
    try:
        user = User(username=f"vrg{suffix}", password=hash_password("verify_pass_123"))
        db.add(user)
        db.flush()
        user_id = user.id
        resource = Resource(
            resource_type=0,
            storage_scene=0,
            upload_purpose=0,
            file_name=FILE_NAME,
            file_hash=f"rg{suffix}".ljust(32, "0")[:32],
            storage_path="",  # 无 COS 路径，本轮不碰对象存储
            user_id=user_id,
            ingest_status="ready",
        )
        db.add(resource)
        db.flush()
        resource_id = resource.id
        db.commit()
        print(f"临时用户 id={user_id} 资料 id={resource_id}")
        _ensure_collection()

        # ---------------------------------------------------------------- 第 1 次入库
        print("\n[1] 第 1 次入库：3 片")
        first_chunks = ["第一版片段甲", "第一版片段乙", "第一版片段丙"]
        rag_service.ingest_file = _fake_ingest(first_chunks, user_id, FILE_NAME)
        result1 = rag_service.RagService.ingest_from_bytes(
            db=db,
            file_bytes=b"%PDF-1.4 first",
            file_name=FILE_NAME,
            user_id=user_id,
            resource_id=resource_id,
        )
        old_ids = list(result1.vector_ids)
        check("切片行数 = 3",
              db.scalar(select(func.count()).select_from(KnowledgeChunk)
                        .where(KnowledgeChunk.resource_id == resource_id)) == 3)
        check("Qdrant 上旧点都在", _alive(old_ids) == set(old_ids), f"{len(old_ids)} 个")
        check("正文已落 content_text", bool(db.get(Resource, resource_id).content_text))

        # ---------------------------------------------------------------- 第 2 次入库（同一份资料）
        print("\n[2] 同一份资料重新入库：2 片（不同内容、不同 vector_id）")
        second_chunks = ["第二版片段甲", "第二版片段乙"]
        rag_service.ingest_file = _fake_ingest(second_chunks, user_id, FILE_NAME)
        result2 = rag_service.RagService.ingest_from_bytes(
            db=db,
            file_bytes=b"%PDF-1.4 second",
            file_name=FILE_NAME,
            user_id=user_id,
            resource_id=resource_id,
        )
        new_ids = list(result2.vector_ids)

        print("\n[2b] 新点的 payload 必须带 resource_id（删除已改为按它精确删，这是前提）")
        payloads = _payloads(new_ids)
        check("每个新点都有 payload", len(payloads) == len(new_ids), f"{len(payloads)}/{len(new_ids)}")
        check("payload.resource_id 与资料一致",
              all(p.get("resource_id") == resource_id for p in payloads),
              str([p.get("resource_id") for p in payloads]))
        check("payload 仍带 user_id / file_name / chunk_index（检索结果要显示文件名）",
              all({"user_id", "file_name", "chunk_index"} <= set(p) for p in payloads),
              str(sorted(payloads[0]) if payloads else []))

        print("\n[3] 关键断言：旧的不留、新的都在")
        check("新旧 vector_id 没有重叠", set(old_ids).isdisjoint(new_ids),
              f"old={len(old_ids)} new={len(new_ids)}")
        check("旧向量已被清掉（这就是修复点）", _alive(old_ids) == set(),
              f"仍存活 {len(_alive(old_ids))}/{len(old_ids)}")
        check("新向量都在 Qdrant 上", _alive(new_ids) == set(new_ids),
              f"{len(_alive(new_ids))}/{len(new_ids)}")
        check("切片行数是新的那批（没有被追加）",
              db.scalar(select(func.count()).select_from(KnowledgeChunk)
                        .where(KnowledgeChunk.resource_id == resource_id)) == 2)

        print("\n[4] 所以检索能回查到原文（这正是修复前会失败的地方）")
        from app.rag import fetch_chunks_by_vector_ids

        db.rollback()  # 换一个干净快照，确保读的是库里真值
        mapped = fetch_chunks_by_vector_ids(db=db, vector_ids=new_ids)
        check("新 vector_id 全部能回查到 MySQL 切片",
              len(mapped) == len(new_ids), f"{len(mapped)}/{len(new_ids)}")
        stale = fetch_chunks_by_vector_ids(db=db, vector_ids=old_ids)
        check("旧 vector_id 已查不到（不会再被当成有效切片）", stale == {}, str(list(stale)))

        # ---------------------------------------------------------------- 反例守卫
        print("\n[5] 回归守卫：首次入库（此前没有切片）不应报错、也不应误删")
        resource2 = Resource(
            resource_type=0, storage_scene=0, upload_purpose=0,
            file_name="验收-首次入库.txt",
            file_hash=f"rg2{suffix}".ljust(32, "0")[:32],
            storage_path="", user_id=user_id, ingest_status="ready",
        )
        db.add(resource2)
        db.flush()
        rag_service.ingest_file = _fake_ingest(["全新资料片段"], user_id, "验收-首次入库.txt")
        result3 = rag_service.RagService.ingest_from_bytes(
            db=db, file_bytes=b"fresh", file_name="验收-首次入库.txt",
            user_id=user_id, resource_id=resource2.id,
        )
        check("首次入库正常拿到 1 片", result3.chunk_count == 1)
        check("首次入库的向量仍在", _alive(list(result3.vector_ids)) == set(result3.vector_ids))
        check("第 1 份资料的向量没被误删", _alive(new_ids) == set(new_ids))
        extra_ids = list(result3.vector_ids)
        db.execute(sql_delete(KnowledgeChunk).where(KnowledgeChunk.resource_id == resource2.id))
        db.execute(sql_delete(Resource).where(Resource.id == resource2.id))
        db.commit()

        # ------------------------------------------- 同名不同资料：改动前这里会互相删向量
        print("\n[6] 关键回归：同一用户有两份**同名**资料，重新入库其中一份不能动另一份")
        other = Resource(
            resource_type=0, storage_scene=0, upload_purpose=0,
            file_name=FILE_NAME,  # 故意同名
            file_hash=f"rg3{suffix}".ljust(32, "0")[:32],
            storage_path="", user_id=user_id, ingest_status="ready",
        )
        db.add(other)
        db.flush()
        other_id = other.id
        db.commit()
        rag_service.ingest_file = _fake_ingest(["另一份同名资料"], user_id, FILE_NAME)
        result4 = rag_service.RagService.ingest_from_bytes(
            db=db, file_bytes=b"%PDF-1.4 other", file_name=FILE_NAME,
            user_id=user_id, resource_id=other_id,
        )
        other_ids = list(result4.vector_ids)
        check("同名资料已各自入库", _alive(other_ids) == set(other_ids), f"{len(other_ids)} 个")
        check("第 1 份的向量也还在（旧实现会在这里被别人删掉）",
              _alive(new_ids) == set(new_ids), f"{len(_alive(new_ids))}/{len(new_ids)}")

        print("\n[7] 重新入库「第 1 份」，同名的那份必须毫发无伤")
        rag_service.ingest_file = _fake_ingest(["第一版改后片段甲", "第一版改后片段乙"], user_id, FILE_NAME)
        result5 = rag_service.RagService.ingest_from_bytes(
            db=db, file_bytes=b"%PDF-1.4 first-again", file_name=FILE_NAME,
            user_id=user_id, resource_id=resource_id,
        )
        newest_ids = list(result5.vector_ids)
        check("第 1 份的新向量都在", _alive(newest_ids) == set(newest_ids), f"{len(newest_ids)} 个")
        check("第 1 份被替换掉的向量已清", _alive(new_ids) == set(), f"仍存活 {len(_alive(new_ids))}")
        check("★ 同名那份资料的向量**没被删**", _alive(other_ids) == set(other_ids),
              f"{len(_alive(other_ids))}/{len(other_ids)}")
        check("同名那份的切片行也还在",
              db.scalar(select(func.count()).select_from(KnowledgeChunk)
                        .where(KnowledgeChunk.resource_id == other_id)) == 1)

        # 收尾这份临时的同名资料
        db.execute(sql_delete(KnowledgeChunk).where(KnowledgeChunk.resource_id == other_id))
        db.execute(sql_delete(Resource).where(Resource.id == other_id))
        db.commit()
        extra_ids = extra_ids + other_ids

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        rag_service.ingest_file = real_ingest
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id} resource={resource_id}")
        else:
            # ⚠️ 顺序：**先删向量点，再删 MySQL 行**。
            # 反过来的话，一旦中途失败（MySQL 已删、向量没删），就留下一个再也没人认领的孤儿点
            # ——本脚本第一版就是这么写的，实测留下过一个 `user_id=47` 的孤儿。
            leftover = list(old_ids) + list(new_ids) + list(newest_ids) + list(extra_ids)
            if leftover:
                try:
                    from qdrant_client.models import PointIdsList

                    client.delete(
                        collection_name=COLLECTION_KNOWLEDGE,
                        points_selector=PointIdsList(points=leftover),
                        wait=True,
                    )
                    print(f"已清理临时向量点 {len(leftover)} 个")
                except Exception as exc:  # noqa: BLE001
                    print(f"临时向量点清理失败（可用 cleanup_orphan_vectors.py 兜底）：{exc}")
            try:
                if resource_id:
                    db.execute(sql_delete(KnowledgeChunk)
                               .where(KnowledgeChunk.resource_id == resource_id))
                    db.execute(sql_delete(Resource).where(Resource.id == resource_id))
                if user_id:
                    db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id} resource={resource_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
