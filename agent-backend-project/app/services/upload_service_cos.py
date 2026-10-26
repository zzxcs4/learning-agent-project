"""COS 上传服务：当前运行逻辑（生产路径）。"""

import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile
from qcloud_cos.cos_exception import CosClientError, CosServiceError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.logging_config import main_logger
from app.core.settings import settings
from app.db.models import KnowledgeBase, Resource, User
from app.services.cos_storage_service import CosStorageService

# 图片后缀与 MIME 白名单。
ALLOWED_IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

# 文档后缀与 MIME 白名单。
ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

# 音频后缀与 MIME 白名单。
ALLOWED_AUDIO_EXTENSIONS = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}

# 所有允许上传的后缀集合（按后缀映射到标准 MIME）。
ALLOWED_EXTENSIONS = {
    **ALLOWED_IMAGE_EXTENSIONS,
    **ALLOWED_DOCUMENT_EXTENSIONS,
    **ALLOWED_AUDIO_EXTENSIONS,
}

# 能归档进知识库的后缀 = **只有文档**。
#
# 为什么必须挡住图片/音频（2026-10-20 用户反馈"知识卡片 png 也会被归于知识库"）：
# 图片/音频是**消息附件**（知识卡出图、头像、语音），它们不是可出题的学习资料。
# 一旦它们带上 `knowledge_base_id`，就会出现在库内文档列表与「未归档文件」里，
# 被当成"资料"计数、甚至进考纲提炼，用户看到的就是"知识库里混着 png"。
# 更糟的一种是**未归档列表**：那张列表的口径本来是"删知识库时被解绑的资料"，
# 而聊天图/知识卡天生 `knowledge_base_id IS NULL`，于是它们全被误标成"孤儿资料"。
KNOWLEDGE_BASE_DOCUMENT_EXTENSIONS = frozenset(ALLOWED_DOCUMENT_EXTENSIONS)

# 拒绝归档时的统一文案（图片与音频共用，避免两处口径漂移）
KNOWLEDGE_BASE_NON_DOCUMENT_MESSAGE = "知识库只收文档（PDF / Word / TXT），图片与音频不能归档到知识库"

# 上传大小上限：10MB。
MAX_FILE_SIZE = 50 * 1024 * 1024

# 资源类型枚举：与 resources.resource_type 对齐。
RESOURCE_TYPE_FILE = 0
RESOURCE_TYPE_IMAGE = 1
RESOURCE_TYPE_AUDIO = 2

# 存储场景枚举：与 resources.storage_scene 对齐。
STORAGE_SCENE_LONG = 0
STORAGE_SCENE_SHORT = 1
STORAGE_SCENE_EXTRACT_ONLY = 2
STORAGE_SCENE_PERMANENT = 3

# 支持的存储场景集合，用于参数校验。
SUPPORTED_STORAGE_SCENES = {
    STORAGE_SCENE_LONG,
    STORAGE_SCENE_SHORT,
    STORAGE_SCENE_EXTRACT_ONLY,
    STORAGE_SCENE_PERMANENT,
}

# 上传用途枚举：0 普通资源，1 用户头像。
UPLOAD_PURPOSE_GENERAL = 0
UPLOAD_PURPOSE_AVATAR = 1
SUPPORTED_UPLOAD_PURPOSES = {UPLOAD_PURPOSE_GENERAL, UPLOAD_PURPOSE_AVATAR}


class UploadService:
    """上传业务服务（COS 版本）。"""

    @staticmethod
    def save_file(
        db: Session,
        user: User,
        file: UploadFile,
        storage_scene: int = STORAGE_SCENE_LONG,
        upload_purpose: int = UPLOAD_PURPOSE_GENERAL,
        knowledge_base_id: int | None = None,
    ) -> tuple[Resource, bool]:
        """统一上传入口，返回 (resource, is_deduplicated)。

        `knowledge_base_id` 可选：传了就把这份资料归档到该知识库（随后会异步提炼
        `quiz_core` 出题知识库）；不传则资料不归属任何库，上传行为与之前完全一致。

        ⚠️ **只有文档能归档进知识库**：带了 `knowledge_base_id` 却上传图片/音频一律 400
        （见 `KNOWLEDGE_BASE_DOCUMENT_EXTENSIONS`）。挡在入口而不是"静默忽略"：
        用户选了个库上传头像/知识卡，静默忽略会让他以为资料进库了，之后在库里找不到。
        """
        # 1) 统一参数与文件校验，先拦截非法请求。
        file_ext, file_size = UploadService._validate_file(file=file)
        UploadService._validate_storage_scene(storage_scene=storage_scene)
        UploadService._validate_upload_purpose(upload_purpose=upload_purpose)
        UploadService._validate_knowledge_base_archive(
            file_ext=file_ext, knowledge_base_id=knowledge_base_id
        )
        UploadService._validate_knowledge_base(
            db=db, user_id=user.id, knowledge_base_id=knowledge_base_id
        )

        # 2) 计算内容哈希，作为用户维度去重核心键。
        md5_hash = UploadService._calculate_md5(file.file)
        resource_type = UploadService._resolve_resource_type(file_ext=file_ext)
        # 3) 基于用户/类型/场景生成对象键与数据库存储路径。
        object_key = UploadService._build_object_key(
            user_id=user.id,
            md5_hash=md5_hash,
            file_ext=file_ext,
            resource_type=resource_type,
            storage_scene=storage_scene,
        )
        storage_path = UploadService._build_storage_path(
            object_key=object_key, storage_scene=storage_scene
        )
        expire_time = UploadService._build_expire_time(
            storage_scene=storage_scene)

        # 4) 代码层预查重：命中则复用资源，只更新场景/用途/过期时间。
        existing = UploadService._get_existing_resource(
            db=db, user_id=user.id, file_hash=md5_hash)
        if existing:
            UploadService._refresh_existing_resource(
                db=db,
                resource=existing,
                user=user,
                storage_scene=storage_scene,
                upload_purpose=upload_purpose,
                expire_time=expire_time,
                knowledge_base_id=knowledge_base_id,
            )
            return existing, True

        # 5) 非“仅提取”场景才上传原文件到 COS。
        if storage_scene != STORAGE_SCENE_EXTRACT_ONLY:
            UploadService._upload_to_cos(
                file=file, file_size=file_size, object_key=object_key)

        # 6) 落库资源元数据，形成“数据库元数据 <-> COS 对象”映射。
        resource = Resource(
            resource_type=resource_type,
            storage_scene=storage_scene,
            upload_purpose=upload_purpose,
            file_name=file.filename,
            file_hash=md5_hash,
            storage_path=storage_path,
            knowledge_base_id=knowledge_base_id,
            user_id=user.id,
            expire_time=expire_time,
        )
        db.add(resource)
        # 仅头像用途的图片才写入 users.avatar，避免普通图片误覆盖头像。
        if resource_type == RESOURCE_TYPE_IMAGE and upload_purpose == UPLOAD_PURPOSE_AVATAR:
            user.avatar = storage_path

        try:
            db.commit()
        except IntegrityError:
            # 并发写入时由唯一索引兜底，回查后返回已存在资源，保持幂等体验。
            db.rollback()
            existing = UploadService._get_existing_resource(
                db=db, user_id=user.id, file_hash=md5_hash)
            if existing:
                return existing, True
            raise_error(code=409, message="资源写入冲突，请重试")

        db.refresh(resource)
        return resource, False

    @staticmethod
    def save_bytes(
        db: Session,
        user: User,
        file_bytes: bytes,
        file_name: str,
        content_type: str,
        storage_scene: int = STORAGE_SCENE_PERMANENT,
        knowledge_base_id: int | None = None,
    ) -> tuple[Resource, bool]:
        """把**内存里的字节**当资源存下来（模型生成的图/音频走这条路）。

        与 `save_file` 的分工：`save_file` 面向用户上传（读 `UploadFile` 的流、按后缀白名单
        校验 MIME）；本方法面向**系统生成物**（字节已在内存、类型由调用方声明）。两者共用
        同一套对象键/去重/落库口径，所以"同一张图重复生成"仍然只会有一条资源记录。

        为什么需要它：知识卡图片是 Seedream 返回的字节，没有 `UploadFile` 也没有文件名；
        硬套 `save_file` 得先拼一个 `UploadFile` 假对象，反而更容易失真（`filename` 会变成
        `None` 而触发 400）。这里直接走字节路径，参数由调用方保证。

        `file_name` 的扩展名必须是白名单内的图片/音频/文档后缀之一（决定对象键与资源类型）；
        后缀不在白名单内时按 `.png` 处理，**不抛错**——生成物的后缀是我们自己给的，
        拿不到时降级比让整条链路失败更合理。

        ⚠️ **生成物永远不会归档进知识库**：传了 `knowledge_base_id` 但内容不是文档
        （知识卡图片、语音）时**强制置 None** 并记一条 warning，不抛错——这条链路是
        "知识卡出图"，它失败会连带把整次讲解降级成纯文字（见 `DifficultyLearnerService`）。
        丢一个归属不会让用户少任何东西；而带进去的一张 png 会污染库内文档与未归档列表。
        """
        if not file_bytes:
            raise_error(code=400, message="文件内容为空")

        file_ext = Path(file_name).suffix.lower()
        if file_ext not in ALLOWED_EXTENSIONS:
            file_ext = ".png"
        safe_name = Path(file_name).name or f"generated{file_ext}"
        UploadService._validate_storage_scene(storage_scene=storage_scene)

        md5_hash = hashlib.md5(file_bytes).hexdigest()
        resource_type = UploadService._resolve_resource_type(file_ext=file_ext)
        if resource_type != RESOURCE_TYPE_FILE and knowledge_base_id is not None:
            main_logger.warning(
                "生成物不是文档，已忽略知识库归属: file_name=%s knowledge_base_id=%s",
                safe_name,
                knowledge_base_id,
            )
            knowledge_base_id = None
        object_key = UploadService._build_object_key(
            user_id=user.id,
            md5_hash=md5_hash,
            file_ext=file_ext,
            resource_type=resource_type,
            storage_scene=storage_scene,
        )
        storage_path = UploadService._build_storage_path(
            object_key=object_key, storage_scene=storage_scene
        )
        expire_time = UploadService._build_expire_time(storage_scene=storage_scene)

        # 内容相同就复用已有资源（与 save_file 同一去重键），不重复占 COS 空间
        existing = UploadService._get_existing_resource(
            db=db, user_id=user.id, file_hash=md5_hash
        )
        if existing:
            # 复用前**补一次上传**：对象键由内容哈希决定，所以这次 PUT 是幂等的。
            # 为什么不能省：资源行有可能还在、而 COS 对象已被人工/清理脚本删掉，
            # 那时"复用"会返回一条指向空地址的记录（历史消息里图片变占位图）。
            if storage_scene != STORAGE_SCENE_EXTRACT_ONLY:
                UploadService._put_bytes_to_cos(
                    data=file_bytes,
                    object_key=object_key,
                    content_type=content_type
                    or ALLOWED_EXTENSIONS.get(file_ext, "application/octet-stream"),
                )
            UploadService._refresh_existing_resource(
                db=db,
                resource=existing,
                user=user,
                storage_scene=storage_scene,
                upload_purpose=UPLOAD_PURPOSE_GENERAL,
                expire_time=expire_time,
                knowledge_base_id=knowledge_base_id,
            )
            return existing, True

        if storage_scene != STORAGE_SCENE_EXTRACT_ONLY:
            UploadService._put_bytes_to_cos(
                data=file_bytes,
                object_key=object_key,
                content_type=content_type or ALLOWED_EXTENSIONS.get(
                    file_ext, "application/octet-stream"
                ),
            )

        resource = Resource(
            resource_type=resource_type,
            storage_scene=storage_scene,
            upload_purpose=UPLOAD_PURPOSE_GENERAL,
            file_name=safe_name,
            file_hash=md5_hash,
            storage_path=storage_path,
            knowledge_base_id=knowledge_base_id,
            user_id=user.id,
            expire_time=expire_time,
        )
        db.add(resource)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = UploadService._get_existing_resource(
                db=db, user_id=user.id, file_hash=md5_hash
            )
            if existing:
                return existing, True
            raise_error(code=409, message="资源写入冲突，请重试")

        db.refresh(resource)
        return resource, False

    @staticmethod
    def _validate_file(file: UploadFile) -> tuple[str, int]:
        """校验文件名、后缀、MIME 与大小，返回 (后缀, 文件字节数)。"""
        if not file.filename:
            raise_error(code=400, message="文件名不能为空")
        file_ext = Path(file.filename).suffix.lower()
        if file_ext not in ALLOWED_EXTENSIONS:
            raise_error(code=400, message="不支持的文件类型")
        expected_content_type = ALLOWED_EXTENSIONS[file_ext]
        allowed_content_types = {
            expected_content_type, "application/octet-stream"}
        if file.content_type and file.content_type not in allowed_content_types:
            raise_error(code=400, message="文件扩展名与内容类型不匹配")
        file.file.seek(0, os.SEEK_END)
        file_size = file.file.tell()
        if file_size > MAX_FILE_SIZE:
            raise_error(
                code=413, message=f"文件大小不能超过 {MAX_FILE_SIZE // 1024 // 1024}MB")
        file.file.seek(0)
        return file_ext, file_size

    @staticmethod
    def _validate_storage_scene(storage_scene: int) -> None:
        """校验存储场景是否合法。"""
        if storage_scene not in SUPPORTED_STORAGE_SCENES:
            raise_error(code=400, message="不支持的存储场景")

    @staticmethod
    def _resolve_resource_type(file_ext: str) -> int:
        """根据扩展名解析资源类型。"""
        if file_ext in ALLOWED_IMAGE_EXTENSIONS:
            return RESOURCE_TYPE_IMAGE
        if file_ext in ALLOWED_AUDIO_EXTENSIONS:
            return RESOURCE_TYPE_AUDIO
        return RESOURCE_TYPE_FILE

    @staticmethod
    def _validate_upload_purpose(upload_purpose: int) -> None:
        """校验上传用途是否合法。"""
        if upload_purpose not in SUPPORTED_UPLOAD_PURPOSES:
            raise_error(code=400, message="不支持的上传用途")

    @staticmethod
    def _validate_knowledge_base(
        db: Session, user_id: int, knowledge_base_id: int | None
    ) -> None:
        """知识库归属校验：只能把资料归档到自己名下的库。"""
        if knowledge_base_id is None:
            return
        exists = db.scalar(
            select(KnowledgeBase.id).where(
                KnowledgeBase.id == knowledge_base_id,
                KnowledgeBase.user_id == user_id,
            )
        )
        if exists is None:
            raise_error(code=404, message="知识库不存在")

    @staticmethod
    def _validate_knowledge_base_archive(
        file_ext: str, knowledge_base_id: int | None
    ) -> None:
        """带库上传时**只放文档进库**：图片（png/jpg/gif）与音频一律 400。

        为什么按后缀而不是按 `resource_type` 判：这里还没有落库的资源行，后缀是唯一的
        入参（`_resolve_resource_type` 也只看后缀），两处口径因此天然一致。
        """
        if knowledge_base_id is None:
            return
        if file_ext in KNOWLEDGE_BASE_DOCUMENT_EXTENSIONS:
            return
        raise_error(code=400, message=KNOWLEDGE_BASE_NON_DOCUMENT_MESSAGE)

    @staticmethod
    def _build_object_key(
        user_id: int, md5_hash: str, file_ext: str, resource_type: int, storage_scene: int
    ) -> str:
        """构建 COS 对象键。"""
        # “仅提取”场景不需要真实对象键，保留虚拟路径用于追踪。
        if storage_scene == STORAGE_SCENE_EXTRACT_ONLY:
            return f"virtual/user_{user_id}/{md5_hash}{file_ext}"
        resource_folder = {
            RESOURCE_TYPE_FILE: "files",
            RESOURCE_TYPE_IMAGE: "images",
            RESOURCE_TYPE_AUDIO: "audios",
        }[resource_type]
        return f"user_{user_id}/{resource_folder}/{md5_hash}{file_ext}"

    @staticmethod
    def _build_storage_path(object_key: str, storage_scene: int) -> str:
        """构建写入数据库的 storage_path。"""
        if storage_scene == STORAGE_SCENE_EXTRACT_ONLY:
            return object_key
        return f"cos://{settings.cos_bucket}/{object_key}"

    @staticmethod
    def _build_expire_time(storage_scene: int) -> datetime | None:
        """根据存储场景生成过期时间。"""
        # 注意：datetime.utcnow() 得到的是 UTC 时间，如果直接存数据库会导致时间提前（因为与北京时间差8小时）。
        # 部分系统不支持 Asia/Shanghai，兼容写法采用 UTC+8 偏移，无需依赖 zoneinfo
        now = datetime.utcnow() + timedelta(hours=8)
        if storage_scene in {STORAGE_SCENE_SHORT, STORAGE_SCENE_EXTRACT_ONLY}:
            return now + timedelta(hours=2)
        if storage_scene == STORAGE_SCENE_LONG:
            return now + timedelta(days=30)
        return None

    @staticmethod
    def _upload_to_cos(file: UploadFile, file_size: int, object_key: str) -> None:
        """上传文件到 COS（存储桶需预先在控制台创建）。"""
        client = CosStorageService.get_client()
        try:
            CosStorageService.put_object(
                client=client,
                bucket_name=settings.cos_bucket,
                object_name=object_key,
                data=file.file,
                length=file_size,
                content_type=file.content_type or "application/octet-stream",
            )
        except CosServiceError:
            raise_error(code=500, message="COS 上传失败")
        except CosClientError:
            raise_error(code=500, message="对象存储服务不可用")
        file.file.seek(0)

    @staticmethod
    def _put_bytes_to_cos(data: bytes, object_key: str, content_type: str) -> None:
        """上传内存字节到 COS（`_upload_to_cos` 的字节版，异常口径与它一致）。"""
        client = CosStorageService.get_client()
        try:
            CosStorageService.put_object(
                client=client,
                bucket_name=settings.cos_bucket,
                object_name=object_key,
                data=data,
                length=len(data),
                content_type=content_type,
            )
        except CosServiceError:
            raise_error(code=500, message="COS 上传失败")
        except CosClientError:
            raise_error(code=500, message="对象存储服务不可用")

    @staticmethod
    def _get_existing_resource(db: Session, user_id: int, file_hash: str) -> Resource | None:
        """按 (user_id, file_hash) 查询已存在资源。"""
        stmt = select(Resource).where(Resource.user_id ==
                                      user_id, Resource.file_hash == file_hash)
        return db.scalar(stmt)

    @staticmethod
    def _refresh_existing_resource(
        db: Session,
        resource: Resource,
        user: User,
        storage_scene: int,
        upload_purpose: int,
        expire_time: datetime | None,
        knowledge_base_id: int | None = None,
    ) -> None:
        """更新已存在资源的场景/用途/过期时间，并处理图片头像同步。

        传了 `knowledge_base_id` 就改归档到该库（用户明确选了另一个库时，"移动"比"忽略"更符合直觉）；
        不传时不动原有归属，避免头像/聊天图这类上传把学习资料的归档冲掉。

        **非文档（图片/音频）永远不进库**：去重命中的可能是一条历史遗留的、
        已经带上库归属的图片行，这里再改一次归属等于把它"再塞回"知识库，
        所以对非文档直接不动 `knowledge_base_id`（入口处 `_validate_knowledge_base_archive`
        已经挡住了新增路径，这里是第二道闸）。

        移动库时**把原库的考纲标记为待重新提炼**：它的 `quiz_core` 里还留着这份资料的章节，
        而资料已经搬走了（不清理会变成"考纲指向别处"的 stale 数据）。
        """
        previous_knowledge_base_id = resource.knowledge_base_id
        resource.storage_scene = storage_scene
        resource.upload_purpose = upload_purpose
        resource.expire_time = expire_time
        if knowledge_base_id is not None and resource.resource_type == RESOURCE_TYPE_FILE:
            resource.knowledge_base_id = knowledge_base_id
        if resource.resource_type == RESOURCE_TYPE_IMAGE and upload_purpose == UPLOAD_PURPOSE_AVATAR:
            user.avatar = resource.storage_path
        # 只在"真的换了库"时打标，避免同一库重传把就绪状态打回 pending
        if previous_knowledge_base_id and previous_knowledge_base_id != resource.knowledge_base_id:
            from app.db.models import KnowledgeBase

            previous = db.get(KnowledgeBase, previous_knowledge_base_id)
            if previous is not None:
                previous.quiz_status = "pending"
        db.commit()
        db.refresh(resource)

    @staticmethod
    def _calculate_md5(file_stream: BinaryIO) -> str:
        """流式计算 MD5 并重置游标。"""
        md5 = hashlib.md5()
        for chunk in iter(lambda: file_stream.read(4096), b""):
            md5.update(chunk)
        file_stream.seek(0)
        return md5.hexdigest()
