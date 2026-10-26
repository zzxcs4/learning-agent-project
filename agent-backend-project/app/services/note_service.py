"""笔记管理服务：提供当前用户笔记的增删改查能力。"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import raise_error
from app.db.models import Note


class NoteManagementService:
    """封装笔记 CRUD，路由层只负责参数接入与响应组装。"""

    @staticmethod
    def list_notes(
        db: Session,
        user_id: int,
        page: int,
        page_size: int,
        note_type: str | None = None,
        keyword: str | None = None,
    ) -> tuple[list[Note], int, bool]:
        """分页查询当前用户笔记标题，支持按类型和标题关键词过滤。"""
        filters = [Note.user_id == user_id]
        if note_type:
            filters.append(Note.note_type == note_type.strip())
        if keyword and keyword.strip():
            filters.append(Note.title.like(f"%{keyword.strip()}%"))

        total = db.scalar(select(func.count(Note.id)).where(*filters)) or 0
        stmt = (
            select(Note)
            .where(*filters)
            .order_by(Note.updated_at.desc(), Note.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        notes = list(db.scalars(stmt))
        return notes, total, page * page_size < total

    @staticmethod
    def create_note(
        db: Session,
        user_id: int,
        title: str,
        content: str,
        note_type: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        source_run_id: str | None = None,
    ) -> Note:
        """创建独立笔记；source_run_id 仅用于智能体归档笔记的来源追踪。"""
        source_run_id = source_run_id.strip() if source_run_id else None
        if source_run_id:
            exists = db.scalar(
                select(Note).where(
                    Note.user_id == user_id,
                    Note.source_run_id == source_run_id,
                )
            )
            if exists:
                raise_error(code=409, message="来源笔记已存在")

        note = Note(
            user_id=user_id,
            session_id=None,
            source_run_id=source_run_id,
            note_type=note_type.strip() if note_type else None,
            title=title.strip(),
            content=content.strip(),
            tags=tags or [],
            metadata_json=metadata or {},
        )
        db.add(note)
        db.commit()
        db.refresh(note)
        return note

    @staticmethod
    def get_note_or_404(db: Session, user_id: int, note_id: int) -> Note:
        """按 user_id + note_id 获取笔记，不存在则抛 404。"""
        note = db.scalar(select(Note).where(Note.id == note_id, Note.user_id == user_id))
        if not note:
            raise_error(code=404, message="笔记不存在")
        return note

    @staticmethod
    def update_note(db: Session, user_id: int, note_id: int, changes: dict[str, Any]) -> Note:
        """更新当前用户笔记；只更新请求中传入的字段。"""
        note = NoteManagementService.get_note_or_404(db=db, user_id=user_id, note_id=note_id)
        if "title" in changes:
            note.title = changes["title"].strip()
        if "content" in changes:
            note.content = changes["content"].strip()
        if "note_type" in changes:
            note.note_type = changes["note_type"].strip() if changes["note_type"] else None
        if "tags" in changes:
            note.tags = changes["tags"] or []
        if "metadata" in changes:
            note.metadata_json = changes["metadata"] or {}

        db.commit()
        db.refresh(note)
        return note

    @staticmethod
    def delete_note(db: Session, user_id: int, note_id: int) -> None:
        """删除当前用户笔记。"""
        note = NoteManagementService.get_note_or_404(db=db, user_id=user_id, note_id=note_id)
        db.delete(note)
        db.commit()
