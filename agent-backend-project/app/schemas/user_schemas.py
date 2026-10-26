from pydantic import BaseModel, ConfigDict, Field


class UserCreateRequest(BaseModel):
    # 在校验层约束用户名和密码长度，避免非法请求进入业务层。
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=8, max_length=72)
    avatar: str | None = None # 注册时可以关联一个预上传的头像路径


class UserUpdateRequest(BaseModel):
    username: str | None = Field(min_length=3, max_length=20, default=None)
    password: str | None = Field(min_length=8, max_length=72, default=None)


class UserResponse(BaseModel):
    # ConfigDict 配置将数据库模型字段直接映射到 Pydantic 字段。
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    avatar: str | None = None


class UserListResponse(BaseModel):
    users: list[UserResponse]


class UserCreateResponse(BaseModel):
    message: str
    user: UserResponse


class UserDeleteResponse(BaseModel):
    message: str
