from typing import Annotated

from fastapi import APIRouter, Depends, Path, status
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.schemas import (
    UserCreateRequest,
    UserCreateResponse,
    UserDeleteResponse,
    UserListResponse,
    UserResponse,
    UserUpdateRequest,
)
from app.services import UserService
from app.services.cos_storage_service import CosStorageService

# 用户相关接口统一鉴权
router = APIRouter(dependencies=[Depends(get_current_user)])
# 统一声明数据库会话依赖类型，避免每个接口重复写 Depends(get_db)。
DBSession = Annotated[Session, Depends(get_db)]

@router.post(
    "",
    response_model=StandardResponse,
    status_code=status.HTTP_201_CREATED,
    responses=common_error_responses,
)
def create_user(payload: UserCreateRequest, db: DBSession) -> StandardResponse:
    # 路由层只负责请求入参、依赖注入和响应封装，业务逻辑全部交给 service。
    user = UserService.create_user(db, payload)
    data = UserCreateResponse(message="用户创建成功", user=user)
    return StandardResponse(code=0, message="用户创建成功", data=data)


@router.get("", response_model=StandardResponse, responses=common_error_responses)
def list_users(db: DBSession) -> StandardResponse:
    # 统一包装为 { "users": [...] }，便于前端扩展分页信息。
    users = UserService.list_users(db)
    data = UserListResponse(users=users)
    return StandardResponse(code=0, message="用户列表获取成功", data=data)


@router.get("/{user_id}", response_model=StandardResponse, responses=common_error_responses)
def get_user(
    user_id: Annotated[int, Path(gt=0, description="用户ID，必须大于0")],
    db: DBSession,
) -> StandardResponse:
    user_resp = UserService.get_user(db, user_id)
    if user_resp.avatar:
        presigned = CosStorageService.get_presigned_url(user_resp.avatar)
        if presigned:
            user_resp.avatar = presigned
    return StandardResponse(code=0, message="用户信息获取成功", data=user_resp)


@router.put("/{user_id}", response_model=StandardResponse, responses=common_error_responses)
def update_user(
    user_id: Annotated[int, Path(gt=0, description="用户ID，必须大于0")],
    payload: UserUpdateRequest,
    db: DBSession,
) -> StandardResponse:
    # 更新逻辑（唯一性、哈希、空请求校验）由 service 层集中处理。
    data = UserService.update_user(db, user_id, payload)
    return StandardResponse(code=0, message="用户信息更新成功", data=data)


@router.delete("/{user_id}", response_model=StandardResponse, responses=common_error_responses)
def delete_user(
    user_id: Annotated[int, Path(gt=0, description="用户ID，必须大于0")],
    db: DBSession,
) -> StandardResponse:
    # 删除成功后返回统一文案，前端可直接用于提示。
    UserService.delete_user(db, user_id)
    data = UserDeleteResponse(message="用户删除成功")
    return StandardResponse(code=0, message="用户删除成功", data=data)
