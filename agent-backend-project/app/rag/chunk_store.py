"""knowledge_chunks 表的读写工具；同时提供 MySQL 侧的向量删除配套函数。"""

import uuid

from qdrant_client.models import PointIdsList
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.models import KnowledgeChunk


def save_chunks(
    db: Session,
    resource_id: int,
    chunks: list[str],
    vector_ids: list[str],
) -> int:
    """批量写入切片：先删旧记录再插入，仅 flush 不 commit。"""
    if not chunks or not vector_ids:
        return 0
    if len(chunks) != len(vector_ids):
        raise ValueError("chunks 与 vector_ids 长度不一致")

    db.query(KnowledgeChunk).filter(
        KnowledgeChunk.resource_id == resource_id
        #不自动同步受影响对象，较少性能开销
    ).delete(synchronize_session=False)


    db.add_all([
        KnowledgeChunk(
            resource_id=resource_id,
            chunk_index=index,
            text=text,
            char_count=len(text),
            vector_id=vid,
        )
        for index, (text, vid) in enumerate(zip(chunks, vector_ids))
    ])
    # flush：将文件内容，写入内存缓冲区、同步到磁盘临时缓存，但没点保存
    db.flush()
    print(f"[RAG][MySQL] 写入 {len(chunks)} 条 knowledge_chunks，resource_id={resource_id}")
    return len(chunks)


def delete_chunks_by_resource(db: Session, resource_id: int) -> int:
    """删除指定资源的全部切片记录。"""
    deleted = db.query(KnowledgeChunk).filter(
        KnowledgeChunk.resource_id == resource_id
    ).delete(synchronize_session=False)
    db.flush()
    return deleted


def fetch_chunks_by_vector_ids(
    db: Session,
    vector_ids: list[str],
) -> dict[str, KnowledgeChunk]:
    """根据 vector_id 列表批量回查，返回 {vector_id: KnowledgeChunk}。"""
    if not vector_ids:
        return {}

    rows = db.scalars(
        select(KnowledgeChunk).where(KnowledgeChunk.vector_id.in_(vector_ids))
    ).all()
    return {row.vector_id: row for row in rows}


def fetch_chunks_by_resource(db: Session, resource_id: int) -> list[KnowledgeChunk]:
    """按 `chunk_index` 升序取回某资源的全部切片。

    `knowledge_chunks` 是切片原文的权威存储，所以出题知识库提炼直接读它，
    不必把用户上传的文件重新解析一遍。
    """
    return list(
        db.scalars(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.resource_id == resource_id)
            .order_by(KnowledgeChunk.chunk_index.asc())
        )
    )


def take_vector_ids_by_resource(db: Session, resource_id: int) -> list[str]:
    """读出该资源**当前**的 vector_id 列表（用于重新入库后清理被替换的旧向量）。

    为什么必须"重新入库前先读、提交后再删"：Qdrant 的 payload 里没有 `resource_id`，
    一份资料的向量只能靠 `knowledge_chunks.vector_id` 定位；而 `save_chunks` 会把旧切片行
    删掉，所以这些 id 在写新切片之后就再也找不回来了——必须先取出来暂存。

    **顺序反过来会丢数据**：先删旧向量、MySQL 提交再失败，就会留下"切片行在、向量没了"。
    所以调用方必须在新切片**提交成功之后**才拿这份 id 去删。
    """
    return [
        str(vid)
        for vid in db.scalars(
            select(KnowledgeChunk.vector_id).where(KnowledgeChunk.resource_id == resource_id)
        )
        if vid
    ]


def delete_vectors_by_ids(vector_ids: list[str]) -> int:
    """删除 Qdrant 里这些 `vector_id` 对应的向量点，返回实际提交删除的 id 数（失败返回 0）。

    **为什么必须单独做这件事**：Qdrant 的 payload 里只存了 `user_id` / `file_name` /
    `chunk_index`，**没有 `resource_id`**，所以删一份资料时无法用过滤条件
    圈出它的向量，只能拿 `knowledge_chunks.vector_id`（就是 Point id）逐个删。

    **只提交合法 UUID**：Qdrant 的 `points` 只接受 UUID 或整数，**一个非法 id 会让整批请求
    400**（实测报 `data did not match any variant of untagged enum PointsSelector`），
    结果是一条都删不掉。生产数据的 id 来自 `uuid.uuid4()` 一直是合法的，但历史数据或手工
    造的数据可能不是，所以在本地过滤掉、只记日志，别让一个脏 id 拖垮整批删除。

    失败不抛异常：删资料的主流程不该因为向量库抽风而整体失败——MySQL 切片已删掉，
    剩下的孤儿向量不会再被检索到（检索走 vector_id 回查 MySQL，查不到就丢弃）。
    """
    if not vector_ids:
        return 0
    valid: list[str] = []
    invalid = 0
    for vid in vector_ids:
        try:
            uuid.UUID(str(vid))
            valid.append(str(vid))
        except (ValueError, AttributeError, TypeError):
            invalid += 1
    if invalid:
        main_logger.warning(f"跳过 {invalid} 个非法 Qdrant point id（不是 UUID），这些向量无法按 id 删除")
    if not valid:
        return 0
    try:
        # 局部导入：Qdrant 客户端较重，而本模块大部分函数只碰 MySQL
        from app.rag.core import COLLECTION_KNOWLEDGE, _get_qdrant_client

        client = _get_qdrant_client()
        if not client.collection_exists(COLLECTION_KNOWLEDGE):
            return 0
        # 显式用 PointIdsList：裸 list 也能用，但显式类型不会随 SDK 版本变化而改变序列化形状
        client.delete(
            collection_name=COLLECTION_KNOWLEDGE,
            points_selector=PointIdsList(points=valid),
            wait=True,
        )
        return len(valid)
    except Exception as exc:  # noqa: BLE001 - 向量删除失败不影响资料删除主流程
        main_logger.warning(f"Qdrant 向量删除失败（{len(valid)} 个点）：{exc}")
        return 0
