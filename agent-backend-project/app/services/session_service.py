"""会话管理查询服务。"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.db.models import Session as SessionModel


class SessionService:
    """会话查询相关服务。"""

    @staticmethod
    def list_sessions(
        db: Session, user_id: int, page: int, page_size: int, session_model: int
    ) -> tuple[list[SessionModel], int, bool]:
        """分页查询当前用户会话列表。"""
        total_stmt = select(func.count(SessionModel.id)).where(
            SessionModel.user_id == user_id,
            SessionModel.session_model == session_model,
        )
        total = db.scalar(total_stmt) or 0

        stmt = (
            select(SessionModel)
            .where(
                SessionModel.user_id == user_id,
                SessionModel.session_model == session_model,
            )
            .order_by(SessionModel.created_at.desc(), SessionModel.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        sessions = list(db.scalars(stmt))
        has_more = page * page_size < total
        return sessions, total, has_more

    @staticmethod
    def create_session(
        db: Session, user_id: int, title: str, session_model: int
    ) -> SessionModel:
        """创建新会话并返回会话对象。"""
        session = SessionModel(
            user_id=user_id, title=title.strip(), session_model=session_model)
        db.add(session)
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def update_session_title(
        db: Session, user_id: int, session_id: int, title: str, session_model: int
    ) -> SessionModel:
        """更新会话标题并返回更新后的会话对象。"""
        session = SessionService.get_session_or_404(
            db=db, user_id=user_id, session_id=session_id, session_model=session_model
        )
        session.title = title.strip()
        db.commit()
        db.refresh(session)
        return session

    @staticmethod
    def get_session_or_404(
        db: Session, user_id: int, session_id: int, session_model: int | None = None
    ) -> SessionModel:
        """按 user_id + session_id (+session_model) 获取会话，不存在则抛 404。"""
        stmt = select(SessionModel).where(
            SessionModel.id == session_id,
            SessionModel.user_id == user_id,
        )
        if session_model is not None:
            stmt = stmt.where(SessionModel.session_model == session_model)
        session = db.scalar(stmt)
        if not session:
            raise_error(code=404, message="会话不存在")
        return session
