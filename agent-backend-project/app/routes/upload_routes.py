
"""上传路由：对外暴露统一资源上传接口。"""

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, UploadFile, status
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import User, get_db
from app.schemas import UploadResponse
from app.services.cos_storage_service import CosStorageService
from app.services.kb_pipeline_service import INGEST_READY, KbPipelineService
from app.services.upload_service_cos import (
    RESOURCE_TYPE_FILE,
    UPLOAD_PURPOSE_AVATAR,
    UploadService,
)

# 上传路由组，不单独设置前缀，由 main.py 统一挂载。
router = APIRouter()


@router.post(
    "/upload/file",
    summary="通用文件上传接口",
    status_code=status.HTTP_201_CREATED,
    response_model=StandardResponse,
    responses=common_error_responses,
)
def upload_file(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    # 存储场景：0长过期，1短过期，2仅提取，3永久。
    storage_scene: Annotated[int, Form()] = 0,
    # 上传用途：0普通资源，1用户头像。仅头像用途会更新 users.avatar。
    upload_purpose: Annotated[int, Form()] = 0,
    # 目标知识库：可选。传了就把资料归档到该库，并在入库后异步提炼出题知识库（quiz_core）。
    knowledge_base_id: Annotated[int | None, Form()] = None,
    file: UploadFile = File(...),
) -> StandardResponse:
    """
    支持文件/图片/音频上传，自动完成类型识别、去重与资源记录管理。

    若带上 `knowledge_base_id`，文件会**异步**完成两件事：向量入库与「考纲」提炼
    （`KbPipelineService`）。上传接口只负责存文件 + 建记录 + 丢任务，因此**不会**因为
    embedding / LLM 慢而卡住；进度可通过知识库接口的 `ingest_status` / `quiz_status` 查询。
    """
    # 业务逻辑全部在 service 层，路由仅做参数接入与响应组装。
    resource, is_deduplicated = UploadService.save_file(
        db=db,
        user=current_user,
        file=file,
        storage_scene=storage_scene,
        upload_purpose=upload_purpose,
        knowledge_base_id=knowledge_base_id,
    )

    # 文档类型才需要入库；图片和音频跳过。
    if resource.resource_type == RESOURCE_TYPE_FILE:
        file.file.seek(0)  # 将文件指针重置到开头
        file_bytes = file.file.read()  # 请求线程里读完（很便宜），后台任务直接用内存字节
        background_tasks.add_task(
            KbPipelineService.process_resource,
            resource.id,
            file_bytes,
            # 同一份资料重复上传且已入库 → 只重新提炼考纲（可能换了库），不必重跑 embedding
            skip_ingest=is_deduplicated and resource.ingest_status == INGEST_READY,
        )

    avatar_url: str | None = None
    if upload_purpose == UPLOAD_PURPOSE_AVATAR:
        avatar_url = CosStorageService.get_presigned_url(resource.storage_path)
    data = UploadResponse(
        message="文件上传成功",
        resource_id=resource.id,
        file_path=resource.storage_path,
        file_hash=resource.file_hash,
        resource_type=resource.resource_type,
        storage_scene=resource.storage_scene,
        upload_purpose=resource.upload_purpose,
        expire_time=resource.expire_time,
        is_deduplicated=is_deduplicated,
        avatar_url=avatar_url,
    )
    return StandardResponse(code=0, message="文件上传成功", data=data)
