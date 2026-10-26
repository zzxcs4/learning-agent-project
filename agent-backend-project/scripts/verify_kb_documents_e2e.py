"""HTTP 端到端验收（自建临时账号 + 临时知识库 + 临时资料，跑完清理）。

为什么要这个包装：`verify_kb_documents_http.py` 要一个**已存在知识库**的账号才能验证
列表/详情/预览。用真实账号需要密码（也不该拿你的密码跑测试），所以这里自建一套：

1. 用 ORM 建临时用户 + 临时知识库；
2. 把现有某份资料的切片**复制**成一份临时资料（同一批 `knowledge_chunks` 文本，
   新的 resource_id / vector_id），这样能真实验证"切片拼回正文 + PDF 流式转发"；
3. 调 `verify_kb_documents_http.main()` 用 HTTP 打全部接口；
4. 删掉临时资料、切片、知识库、用户（顺序按外键依赖）。

用法（在 agent-backend-project 目录下）：

    python scripts/verify_kb_documents_e2e.py
    python scripts/verify_kb_documents_e2e.py --source-resource-id 2
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.db.database import SessionLocal
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource, User
from app.security import hash_password

import scripts.verify_kb_documents_http as http_verify  # noqa: E402

TEMP_PASSWORD = "verify_pass_123"


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库文档 HTTP 端到端验收")
    parser.add_argument("--source-resource-id", type=int, default=None, help="复制哪份资料的切片")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    # users.username 是 varchar(20)，临时账号名必须压在这个长度内
    username = f"vkbe{suffix}"
    user_id = kb_id = resource_id = None
    try:
        source = (
            db.get(Resource, args.source_resource_id)
            if args.source_resource_id
            else db.scalar(
                select(Resource)
                .join(KnowledgeChunk, KnowledgeChunk.resource_id == Resource.id)
                .order_by(Resource.id.asc())
            )
        )
        if source is None:
            print("库里没有可作为素材的资料，无法做端到端验收")
            return 1

        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.flush()
        user_id = user.id

        knowledge_base = KnowledgeBase(user_id=user_id, name=f"临时验收库-{suffix}")
        db.add(knowledge_base)
        db.flush()
        kb_id = knowledge_base.id

        resource = Resource(
            resource_type=source.resource_type,
            storage_scene=source.storage_scene,
            upload_purpose=0,
            file_name=source.file_name,
            file_hash=f"verify{suffix}".ljust(32, "0")[:32],            storage_path=source.storage_path,
            knowledge_base_id=kb_id,
            ingest_status=source.ingest_status,
            user_id=user_id,
        )
        db.add(resource)
        db.flush()
        resource_id = resource.id

        source_chunks = list(
            db.scalars(
                select(KnowledgeChunk)
                .where(KnowledgeChunk.resource_id == source.id)
                .order_by(KnowledgeChunk.chunk_index.asc())
            )
        )
        for chunk in source_chunks:
            db.add(
                KnowledgeChunk(
                    resource_id=resource_id,
                    chunk_index=chunk.chunk_index,
                    text=chunk.text,
                    char_count=chunk.char_count,
                    vector_id=f"verify-{suffix}-{chunk.chunk_index}",
                )
            )
        db.commit()
        print(
            f"临时账号={username} 知识库={kb_id} 资料={resource_id}"
            f"（复制了 {len(source_chunks)} 片切片；content_text 故意留空，用来验证切片拼回）"
        )

        sys.argv = ["verify_kb_documents_http", "--username", username,
                    "--password", TEMP_PASSWORD, "--kb-id", str(kb_id)]
        return http_verify.main()
    finally:
        # 清理：先子表后父表（本项目没有 ORM relationship，顺序要显式）
        try:
            if resource_id:
                db.execute(sql_delete(KnowledgeChunk).where(KnowledgeChunk.resource_id == resource_id))
            if resource_id:
                db.execute(sql_delete(Resource).where(Resource.id == resource_id))
            if kb_id:
                db.execute(sql_delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
            if user_id:
                db.execute(sql_delete(User).where(User.id == user_id))
            db.commit()
            print(f"已清理临时数据：user={user_id} kb={kb_id} resource={resource_id}")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
