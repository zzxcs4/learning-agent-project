import re
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

# 最小弱密码黑名单，用于拦截最常见的弱口令。
_COMMON_WEAK_PASSWORDS = {
    "123456",
    "12345678",
    "123456789",
    "password",
    "qwerty",
    "111111",
    "123123",
    "admin",
    "letmein",
    "iloveyou",
    "000000",
}


def _validate_bcrypt_length(password: str) -> bytes:
    # bcrypt 按字节数限制明文长度最大 72。
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > 72:
        raise ValueError("密码字节长度不能超过 72")
    return password_bytes


def hash_password(password: str) -> str:
    # 使用 pyca/bcrypt 直接生成哈希，避免 passlib 与 bcrypt 5.x 的兼容问题。
    password_bytes = _validate_bcrypt_length(password)
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    # 使用 bcrypt 校验明文与哈希是否匹配。
    return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))


def validate_password_strength(password: str, username: str | None = None) -> None:
    # 规则保持简单、可预期，便于前端提示与一致校验。
    normalized = password.strip()
    if normalized.lower() in _COMMON_WEAK_PASSWORDS:
        raise ValueError("密码过于常见，请使用更复杂的密码")
    if username and username.lower() in normalized.lower():
        raise ValueError("密码不能包含用户名")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise ValueError("密码必须同时包含字母和数字")


def create_access_token(
    *,
    user_id: int,
    secret: str,
    algorithm: str,
    expires_minutes: int,
) -> str:
    # 访问令牌包含用户标识与过期时间。
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {"sub": str(user_id), "typ": "access", "exp": expire_at}
    return jwt.encode(payload, secret, algorithm=algorithm)


def create_refresh_token(
    *,
    user_id: int,
    secret: str,
    algorithm: str,
    expires_minutes: int,
) -> str:
    # 刷新令牌有效期更长，仅用于换取新的访问令牌。
    expire_at = datetime.now(timezone.utc) + timedelta(minutes=expires_minutes)
    payload = {"sub": str(user_id), "typ": "refresh", "exp": expire_at}
    return jwt.encode(payload, secret, algorithm=algorithm)


def decode_token(token: str, secret: str, algorithm: str) -> dict:
    # 解码并验证 JWT（包括过期验证）。
    return jwt.decode(token, secret, algorithms=[algorithm])
