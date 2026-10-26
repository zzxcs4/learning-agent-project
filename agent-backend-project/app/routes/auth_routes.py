from typing import Annotated # 用于类型提示，表示带依赖注入的类型
from fastapi import APIRouter, Depends, status # FastAPI核心组件：路由、依赖注入、HTTP状态码
from sqlalchemy.orm import Session # SQLAlchemy的会话对象，用于数据库操作

from app.core import StandardResponse, common_error_responses, raise_error # 核心模块：通用响应、通用错误响应、自定义错误抛出
from app.core.rate_limit import rate_limit_dep # 限流依赖，用于防止滥用
from app.core.settings import settings # 项目配置，如JWT密钥、过期时间等
from app.db import User, get_db # 数据库模型：User表，以及获取数据库会话的依赖
from app.schemas import ( # Pydantic模型：定义请求和响应的数据结构
    LoginRequest, # 登录请求体
    RefreshRequest, # 刷新令牌请求体
    TokenResponse, # 令牌响应体
    UserCreateRequest, # 用户创建请求体 (注册)
    UserCreateResponse, # 用户创建响应体 (注册)
)
from app.security import create_access_token, create_refresh_token, decode_token # 安全模块：JWT令牌的创建和解码
from app.services import UserService # 服务层：处理用户相关的业务逻辑

# 创建API路由实例（不在这里设置 /auth 前缀，统一在 main.py 中挂载）
router = APIRouter()

# 数据库会话依赖，Annotated用于FastAPI的依赖注入
DBSession = Annotated[Session, Depends(get_db)]

# 注册接口限流：每分钟 5 次，防止恶意注册或爆破。
register_rate_limit = rate_limit_dep(limit=5, window_seconds=60, key_prefix="auth_register")
# 登录接口限流
login_rate_limit = rate_limit_dep(limit=10, window_seconds=60, key_prefix="auth_login")
# 刷新令牌接口限流
refresh_rate_limit = rate_limit_dep(limit=30, window_seconds=60, key_prefix="auth_refresh")

@router.post(
    "/register",
    response_model=StandardResponse, # 响应模型：统一成功响应结构
    status_code=status.HTTP_201_CREATED, # 成功创建资源的状态码
    responses=common_error_responses, # 引用通用的错误响应定义
    dependencies=[Depends(register_rate_limit)], # 应用注册接口的限流依赖
)
def register(payload: UserCreateRequest, db: DBSession) -> StandardResponse:
    """注册新用户"""
    # 直接复用用户创建逻辑，保持实现最简。
    user = UserService.create_user(db, payload)
    data = UserCreateResponse(message="注册成功", user=user)
    return StandardResponse(code=0, message="注册成功", data=data)


@router.post(
    "/login",
    response_model=StandardResponse, # 响应模型：统一成功响应结构
    responses=common_error_responses, # 引用通用的错误响应定义
    dependencies=[Depends(login_rate_limit)], # 应用登录接口的限流依赖
)
def login(payload: LoginRequest, db: DBSession) -> StandardResponse:
    """用户登录并获取JWT令牌"""
    # 验证用户凭据
    user = UserService.authenticate_user(db, payload.username, payload.password)
    # 生成访问令牌 (Access Token)
    access_token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_access_expire_minutes,
    )
    # 生成刷新令牌 (Refresh Token)
    refresh_token = create_refresh_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_refresh_expire_minutes,
    )
    # 返回令牌响应
    data = TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.jwt_access_expire_minutes * 60,
    )
    return StandardResponse(code=0, message="登录成功", data=data)


@router.post(
    "/refresh",
    response_model=StandardResponse, # 响应模型：统一成功响应结构
    responses=common_error_responses, # 引用通用的错误响应定义
    dependencies=[Depends(refresh_rate_limit)], # 应用刷新令牌接口的限流依赖
)
def refresh(payload: RefreshRequest, db: DBSession) -> StandardResponse:
    """通过刷新令牌获取新的访问令牌"""
    # 尝试解码刷新令牌
    try:
        token_payload = decode_token(
            payload.refresh_token,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
    except Exception:
        # 解码失败，刷新令牌无效或已过期
        raise_error(code=status.HTTP_401_UNAUTHORIZED, message="刷新令牌无效或已过期")

    # 检查令牌类型是否为刷新令牌
    if token_payload.get("typ") != "refresh":
        raise_error(code=status.HTTP_401_UNAUTHORIZED, message="刷新令牌无效或已过期")

    # 从令牌中获取用户ID并查询用户
    user_id = int(token_payload.get("sub", 0) or 0)
    user = db.get(User, user_id)
    if not user:
        # 用户不存在或已被删除
        raise_error(code=status.HTTP_401_UNAUTHORIZED, message="用户不存在或已被删除")

    # 为该用户生成新的访问令牌
    access_token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_access_expire_minutes,
    )
    # 同时签发新的刷新令牌
    refresh_token = create_refresh_token(
        user_id=user.id,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_refresh_expire_minutes,
    )
    # 返回新的访问令牌和刷新令牌
    data = TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.jwt_access_expire_minutes * 60,
    )
    return StandardResponse(code=0, message="令牌刷新成功", data=data)
