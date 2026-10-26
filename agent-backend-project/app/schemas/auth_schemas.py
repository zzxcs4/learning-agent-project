"""认证模块Pydantic数据模型定义。"""

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    """用户登录请求体模型。"""
    username: str = Field(min_length=3, max_length=20)
    password: str = Field(min_length=8, max_length=72)


class TokenResponse(BaseModel):
    """JWT令牌响应模型，包含访问令牌和刷新令牌。"""

    access_token: str   # 访问令牌
    refresh_token: str  # 刷新令牌
    token_type: str = "bearer" # 令牌类型，默认为 "bearer"
    expires_in: int  # 访问令牌的有效期（秒）


class RefreshRequest(BaseModel):
    """刷新令牌请求体模型。"""

    # 兼容常见误写（refresh token），并保留标准字段 refresh_token
    model_config = ConfigDict(populate_by_name=True)
    refresh_token: str = Field(alias="refresh token")
