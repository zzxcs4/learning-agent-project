"""RAG 业务服务层：对接上传链路，上传时直接摄入并备份原文到 MySQL。"""

import tempfile  # 创建临时文件
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.models import Resource
from app.rag import IngestResult, delete_vectors_by_ids, ingest_file, save_chunks
from app.rag import take_vector_ids_by_resource
from app.rag.core import MAX_CONTENT_CHARS


class RagService:
    """RAG 知识库摄入服务。"""

    @staticmethod
    def ingest_from_bytes(
        db: Session,
        file_bytes: bytes,
        file_name: str,
        user_id: int,
        resource_id: int,
    ) -> IngestResult:
        """上传时直接复用内存中的文件字节，零额外网络 I/O。

        流程：写临时文件 → ingest_file（写 Qdrant） → save_chunks（备份 MySQL）
        → 顺手把解析全文写进 `resources.content_text`（知识库阅读器用）→ commit
        → **按 id 清理被替换掉的旧向量**。
        Qdrant 写成功但 MySQL 写失败时事务回滚。

        旧点为什么由这里删、而不是在 `_upsert_chunks` 里按 `(user_id, file_name)` 删：
        payload 里没有 `resource_id` 时，`file_name` 是"这份资料"的代理键，而同一用户可以**同名
        不同资料**（两个知识库里都有 `简历.pdf`）→ 一次入库会删掉别人的向量。所以删除改成
        "按 `resource_id` 从 MySQL 读出这批点、逐个 id 精确删"，`resource_id` 现在也写进了 payload。

        顺序**必须是"先提交新切片、再删旧向量"**——反过来一旦 MySQL 提交失败，
        就会留下"切片行在、向量没了"，比原来更糟（原文彻底检索不到）。
        """
        suffix = Path(file_name).suffix.lower()

        # 把内存中的字节数据临时写到磁盘 ，让解析库能读取。
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            tmp_path = Path(tmp.name)

        try:
            # 必须在 save_chunks 之前读：它会删掉旧切片行，之后这些 id 就找不回来了。
            replaced_vector_ids = take_vector_ids_by_resource(db, resource_id=resource_id)

            result = ingest_file(
                file_path=tmp_path,
                user_id=user_id,
                resource_id=resource_id,
                original_file_name=file_name,
            )
            try:
                save_chunks(
                    db=db,
                    resource_id=resource_id,
                    chunks=result.chunks,
                    vector_ids=result.vector_ids,
                )
                _store_content_text(db, resource_id=resource_id, text=result.text)
                db.commit()
            except Exception:
                db.rollback()
                raise

            # 新切片已落库并提交，此时删旧向量才安全。失败不影响入库结果：
            # 这些点已经没有任何 MySQL 行指向它们，只是占存储（可用 cleanup_orphan_vectors.py 兜底）。
            if replaced_vector_ids:
                removed = delete_vectors_by_ids(replaced_vector_ids)
                main_logger.info(
                    f"重新入库后清理旧向量 resource_id={resource_id}: "
                    f"{removed}/{len(replaced_vector_ids)} 个"
                )
            return result
        finally:
            tmp_path.unlink(missing_ok=True)


def _store_content_text(db: Session, *, resource_id: int, text: str) -> None:
    """把解析全文落到 `resources.content_text`（截断到 MAX_CONTENT_CHARS）。

    为什么要存一份：`knowledge_chunks` 之间带重叠、且被递归切片切断，
    拼回来会损失排版；用户要"像语雀那样看原文"，就得有一份完整文本可用。
    """
    resource = db.get(Resource, resource_id)
    if resource is None:
        return
    body = (text or "").strip()
    if not body:
        resource.content_text = None
        resource.content_chars = None
        return
    if len(body) > MAX_CONTENT_CHARS:
        body = body[:MAX_CONTENT_CHARS]
    resource.content_text = body
    resource.content_chars = len(body)
