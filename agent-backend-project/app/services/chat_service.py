"""聊天消息查询与会话删除服务。"""

from qcloud_cos.cos_exception import CosServiceError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.logging_config import main_logger
from app.db.models import ChatMessage, KnowledgeChunk, Resource, Session as SessionModel
from app.schemas.chat_schemas import ChatMessageItem
from app.services.cos_storage_service import CosStorageService

# 允许回传到前端的附件类型白名单。
ALLOWED_SEGMENT_TYPES = {"file", "image", "audio"}

# 附件预签名 URL 有效期：默认 1 小时对"翻历史消息"太短（滚两页就过期变占位图），
# 拉长到 7 天；真正的安全边界是"每次读消息都重新签一次"，而不是 URL 本身的长短。
SEGMENT_URL_EXPIRE_SECONDS = 7 * 24 * 3600


class ChatService:
    """聊天相关业务服务。"""

    @staticmethod
    def get_session_messages(
        db: Session,
        user_id: int,
        session_id: int,
        page: int,
        page_size: int,
        session_model: int | None = None,
    ) -> tuple[list[ChatMessageItem], bool]:
        """
        分页查询会话消息。

        说明：
        - request_text/response_text 始终作为正文字段返回；
        - request_segments/response_segments 仅返回附件段（file/image/audio）；
        - 查询按 created_at 倒序分页，再在单页内反转为升序，减少前端插入抖动。
        """
        ChatService._ensure_session_belongs_to_user(
            db=db, session_id=session_id, user_id=user_id, session_model=session_model
        )

        total_stmt = select(func.count(ChatMessage.id)).where(
            ChatMessage.session_id == session_id,
            ChatMessage.user_id == user_id,
        )
        total = db.scalar(total_stmt) or 0

        stmt = (
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id, ChatMessage.user_id == user_id)
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = list(db.scalars(stmt))
        has_more = page * page_size < total

        # 单页按升序返回，减少前端渲染抖动；全局时序仍由前端 created_at 二次排序兜底。
        rows.reverse()
        messages = [
            ChatMessageItem(
                id=row.id,
                request_id=row.request_id,
                status=row.status,
                request_text=row.request_text,
                response_text=row.response_text,
                request_segments=ChatService._build_segments(
                    db, row.request_segments),
                response_segments=ChatService._build_segments(
                    db, row.response_segments),
                response_payload=row.response_payload,
                sources=ChatService._build_reference_sources(db, row.reference_sources),
                created_at=row.created_at,
            )
            for row in rows
        ]
        return messages, has_more

    @staticmethod
    def _build_reference_sources(db: Session, raw_sources: list[dict] | None) -> list[dict]:
        """根据轻量引用 {chunk_id, score} 回查知识块，拼成前端展示结构。"""
        if not raw_sources:
            return []

        chunk_ids = [
            int(item["chunk_id"])
            for item in raw_sources
            if isinstance(item, dict) and item.get("chunk_id")
        ]
        if not chunk_ids:
            return raw_sources

        rows = db.execute(
            select(KnowledgeChunk, Resource.file_name)
            .join(Resource, KnowledgeChunk.resource_id == Resource.id)
            .where(KnowledgeChunk.id.in_(chunk_ids))
        ).all()
        chunk_map = {chunk.id: (chunk, file_name) for chunk, file_name in rows}

        sources: list[dict] = []
        for item in raw_sources:
            if not isinstance(item, dict):
                continue
            chunk_id = item.get("chunk_id")
            chunk_pair = chunk_map.get(chunk_id)
            if not chunk_pair:
                sources.append({
                    "chunk_id": chunk_id,
                    "score": item.get("score", 0),
                    "file_name": "",
                    "chunk_index": None,
                    "text": "该参考片段已被删除",
                })
                continue

            chunk, file_name = chunk_pair
            sources.append({
                "chunk_id": chunk.id,
                "resource_id": chunk.resource_id,
                "chunk_index": chunk.chunk_index,
                "file_name": file_name,
                "score": item.get("score", 0),
                "text": chunk.text,
            })
        return sources

    @staticmethod
    def delete_session(
        db: Session, user_id: int, session_id: int, session_model: int | None = None
    ) -> tuple[int, int]:
        """
        删除会话与其关联消息，并回收当前会话附件资源。

        返回：
        - deleted_messages: 删除消息数
        - deleted_resources: 删除资源对象数（当前会话出现的 resource_id）
        """
        ChatService._ensure_session_belongs_to_user(
            db=db, session_id=session_id, user_id=user_id, session_model=session_model
        )

        msg_stmt = select(ChatMessage).where(
            ChatMessage.session_id == session_id, ChatMessage.user_id == user_id
        )
        messages = list(db.scalars(msg_stmt))
        message_count = len(messages)

        resource_ids = ChatService._collect_resource_ids(messages=messages)
        resource_paths_to_delete: list[str] = []
        deleted_resource_count = 0

        # 当前会话出现的 resource_id 直接删除。
        if resource_ids:
            resource_stmt = select(Resource).where(
                Resource.user_id == user_id,
                Resource.id.in_(resource_ids),
            )
            resources_to_delete = list(db.scalars(resource_stmt))
            resource_paths_to_delete = [
                item.storage_path for item in resources_to_delete]
            deleted_resource_count = len(resources_to_delete)
            for resource in resources_to_delete:
                db.delete(resource)

        # 删除聊天消息
        db.execute(
            delete(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.user_id == user_id
            )
        )
        # 删除会话消息
        db.execute(
            delete(SessionModel).where(
                SessionModel.id == session_id, SessionModel.user_id == user_id
            )
        )
        # 提交数据库事务
        db.commit()
        # 删除对象存储文件
        ChatService._delete_storage_objects(resource_paths_to_delete)
        return message_count, deleted_resource_count

    @staticmethod
    def _build_segments(db: Session, raw_segments: list[dict] | None) -> list[dict]:
        """
        仅返回附件段信息（file/image/audio），过滤无效项，并**现签附件访问地址**。

        注意：
        - 该函数不会拼接或改写 request_text/response_text；
        - 非 dict 项或未知 type 会被忽略；
        - 带 `storage_path`（`cos://bucket/key`）的段会重新生成预签名 URL：知识卡这类
          **系统生成**的附件落库时只存路径，URL 1 小时就过期，历史消息不能靠旧 URL 存活。
        """
        if not raw_segments:
            return []

        result: list[dict] = []
        for item in raw_segments:
            if not isinstance(item, dict):
                continue
            segment_type = str(item.get("type", "")).strip().lower()
            if segment_type not in ALLOWED_SEGMENT_TYPES:
                continue
            result.append(ChatService._sign_segment_url(item))
        return result

    @staticmethod
    def _sign_segment_url(item: dict) -> dict:
        """按 `storage_path` 现签一个新的访问地址；签不出来就沿用库里那个（不抛错）。

        用户上传的附件走 COS 默认域名会被强制下载，但 `<img>` / `<audio>` 这类**元素级**
        加载不看 `Content-Disposition`，所以预签名 URL 对图片/音频够用（文档预览才需要
        后端转发，见 `resource_download` 路由）。
        """
        storage_path = str(item.get("storage_path") or "").strip()
        if not storage_path:
            return item
        fresh_url = CosStorageService.get_presigned_url(
            storage_path, expires_seconds=SEGMENT_URL_EXPIRE_SECONDS
        )
        if not fresh_url:
            return item
        return {**item, "url": fresh_url}

    @staticmethod
    def _collect_resource_ids(messages: list[ChatMessage]) -> set[int]:
        """
        从消息附件段提取 resource_id 集合。

        仅提取正整数 resource_id，用于删除会话时做资源回收判定。
        """
        resource_ids: set[int] = set()
        for message in messages:
            # 这里只取 resource_id，不需要现签 URL（也避免删除会话时白跑一遍签名）
            for segment in ChatService._valid_segments(message.request_segments):
                resource_id = segment.get("resource_id")
                if isinstance(resource_id, int) and resource_id > 0:
                    resource_ids.add(resource_id)
            for segment in ChatService._valid_segments(message.response_segments):
                resource_id = segment.get("resource_id")
                if isinstance(resource_id, int) and resource_id > 0:
                    resource_ids.add(resource_id)
        return resource_ids

    @staticmethod
    def _valid_segments(raw_segments: list[dict] | None) -> list[dict]:
        """只做"类型白名单"过滤，不签名（删除会话/回收资源时够用）。"""
        if not raw_segments:
            return []
        return [
            item
            for item in raw_segments
            if isinstance(item, dict)
            and str(item.get("type", "")).strip().lower() in ALLOWED_SEGMENT_TYPES
        ]

    @staticmethod
    def _ensure_session_belongs_to_user(
        db: Session, session_id: int, user_id: int, session_model: int | None = None
    ) -> None:
        """会话存在性与归属校验，不通过时抛 404。"""
        stmt = select(SessionModel.id).where(
            SessionModel.id == session_id, SessionModel.user_id == user_id
        )
        if session_model is not None:
            stmt = stmt.where(SessionModel.session_model == session_model)
        found_id = db.scalar(stmt)
        if not found_id:
            raise_error(code=404, message="会话不存在")

    @staticmethod
    def _delete_storage_objects(storage_paths: list[str]) -> int:
        """
        删除 COS 对象，返回成功删除数量。

        说明：
        - 仅识别 cos://bucket/key 路径；
        - 删除失败仅记录日志，不回滚已提交的数据库事务。
        """
        if not storage_paths:
            return 0

        client = CosStorageService.get_client()
        deleted = 0
        for storage_path in storage_paths:
            if not CosStorageService.is_cos_storage_path(storage_path):
                continue
            try:
                CosStorageService.delete_object_by_storage_path(
                    client=client, storage_path=storage_path, ignore_not_found=True
                )
                deleted += 1
            except CosServiceError as exc:
                main_logger.warning(
                    f"删除 COS 对象失败: {storage_path}, error={exc}")
            except Exception as exc:
                main_logger.warning(f"删除对象存储文件异常: {storage_path}, error={exc}")
        return deleted
