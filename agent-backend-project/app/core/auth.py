
from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core import raise_error
from app.core.settings import settings
from app.db import get_db
from app.security import decode_token

# 让 Swagger 自动出现 "Authorize" 按钮
_bearer_scheme = HTTPBearer()

def get_current_user(
    # 从请求头中获取 Authorization 字段，格式为 "Bearer <token>"
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> "User": # 使用字符串前向引用，避免在文件顶部导入 User
    """鉴权依赖：校验 access token 并返回当前用户。"""
    # 在函数内部导入 User 模型，打破循环依赖
    from app.db.models import User

    token = credentials.credentials
    try:
        token_payload = decode_token(
            token, secret=settings.jwt_secret, algorithm=settings.jwt_algorithm
        )
    except Exception:
        raise_error(code=401, message="访问令牌无效或已过期")

    if token_payload.get("typ") != "access":
        raise_error(code=401, message="访问令牌无效或已过期")

    user_id = int(token_payload.get("sub", 0) or 0)
    user = db.get(User, user_id)
    if not user:
        raise_error(code=401, message="用户不存在或已被删除")
    return user

# 可选鉴权依赖：如果提供了有效的 access token，则返回用户，否则返回 None。
def try_get_current_user(
    authorization: str | None = Header(None, description="Bearer Token"),
    db: Session = Depends(get_db),
) -> "User | None": # 使用字符串前向引用
    if not authorization:
        return None
    try:
        scheme, token = authorization.split()
        if scheme.lower() != "bearer":
            return None
    except ValueError:
        return None

    try:
        credentials = HTTPAuthorizationCredentials(scheme="bearer", credentials=token)
        return get_current_user(credentials, db)
    except Exception:
        return None
