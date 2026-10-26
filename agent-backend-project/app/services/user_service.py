
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.db import User
from app.schemas import UserCreateRequest, UserResponse, UserUpdateRequest
from app.security import hash_password, validate_password_strength, verify_password


class UserService:
    @staticmethod
    def _ensure_username_unique(
        db: Session, username: str, exclude_user_id: int | None = None
    ) -> None:
        # ... (此方法保持不变)
        stmt = select(User).where(User.username == username)
        existing_user = db.scalar(stmt)
        if existing_user and (exclude_user_id is None or existing_user.id != exclude_user_id):
            raise_error(code=409, message="用户名已被占用", detail={"username": username})

    @staticmethod
    def create_user(db: Session, payload: UserCreateRequest) -> User:
        """
        创建新用户，并返回 User ORM 对象。
        """
        # 1) 校验用户名唯一性
        UserService._ensure_username_unique(db, payload.username)

        # 2) 校验密码强度并哈希
        try:
            validate_password_strength(payload.password, payload.username)
            hashed_password = hash_password(payload.password)
        except ValueError as exc:
            raise_error(code=400, message=str(exc), detail={"field": "password"})

        # 3) 创建数据库用户记录
        user = User(username=payload.username, password=hashed_password)
        db.add(user)
        db.commit()
        db.refresh(user)

        # 4) 返回 ORM 对象，而不是响应模型
        return user

    # ... (list_users, get_user, authenticate_user, update_user, delete_user 保持不变)
    @staticmethod
    def list_users(db: Session) -> list[UserResponse]:
        # 按 id 正序返回，保证列表顺序稳定。
        users = db.scalars(select(User).order_by(User.id.asc())).all()
        return [UserResponse.model_validate(user) for user in users]

    @staticmethod
    def get_user(db: Session, user_id: int) -> UserResponse:
        # 使用主键查询单条记录，性能稳定且语义明确。
        user = db.get(User, user_id)
        if not user:
            raise_error(
                code=404,
                message="用户不存在",
                detail={"user_id": user_id},
            )
        return UserResponse.model_validate(user)

    @staticmethod
    def authenticate_user(db: Session, username: str, password: str) -> User:
        # 认证统一返回“用户名或密码错误”。
        stmt = select(User).where(User.username == username)
        user = db.scalar(stmt)
        if not user or not verify_password(password, user.password):
            raise_error(code=401, message="用户名或密码错误")
        return user

    @staticmethod
    def update_user(db: Session, user_id: int, payload: UserUpdateRequest) -> UserResponse:
        # 先确保目标用户存在。
        current_user = db.get(User, user_id)
        if not current_user:
            raise_error(
                code=404,
                message="用户不存在",
                detail={"user_id": user_id},
            )

        # 更新接口要求至少传一个字段，避免空更新请求。
        if payload.username is None and payload.password is None:
            raise_error(
                code=400,
                message="请至少提供一个需要更新的字段",
            )

        # 对未传入字段沿用旧值，实现“部分更新”。
        new_username = payload.username or current_user.username
        if payload.username is not None:
            # 修改用户名时再次做唯一性校验（排除自己）。
            UserService._ensure_username_unique(db, payload.username, exclude_user_id=user_id)

        new_password = current_user.password
        if payload.password is not None:
            # 仅在显式传入新密码时，重新计算密码哈希值。
            try:
                validate_password_strength(payload.password, new_username)
                new_password = hash_password(payload.password)
            except ValueError as exc:
                raise_error(code=400, message=str(exc), detail={"field": "password"})

        current_user.username = new_username
        current_user.password = new_password
        db.commit()
        db.refresh(current_user)
        return UserResponse.model_validate(current_user)

    @staticmethod
    def delete_user(db: Session, user_id: int) -> None:
        # 删除前先检查存在性，保证接口返回语义一致（不存在时 404）。
        user = db.get(User, user_id)
        if not user:
            raise_error(
                code=404,
                message="用户不存在",
                detail={"user_id": user_id},
            )
        db.delete(user)
        db.commit()
