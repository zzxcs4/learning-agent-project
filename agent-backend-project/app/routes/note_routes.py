"""笔记管理路由：负责鉴权、参数校验与标准响应组装。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import Note, User
from app.schemas import (
    NoteCreateRequest,
    NoteDeleteResponse,
    NoteDetailResponse,
    NoteItem,
    NoteListItem,
    NoteListResponse,
    NoteUpdateRequest,
)
from app.services.note_service import NoteManagementService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


def _to_note_list_item(note: Note) -> NoteListItem:
    """把 ORM 笔记对象转换成列表轻量响应模型。"""
    return NoteListItem(
        id=note.id,
        note_type=note.note_type,
        title=note.title,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


def _to_note_item(note: Note) -> NoteItem:
    """把 ORM 笔记对象转换成详情响应模型。"""
    return NoteItem(
        id=note.id,
        user_id=note.user_id,
        session_id=note.session_id,
        source_run_id=note.source_run_id,
        note_type=note.note_type,
        title=note.title,
        content=note.content,
        tags=note.tags or [],
        metadata=note.metadata_json or {},
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


@router.get("", response_model=StandardResponse, responses=common_error_responses, summary="分页获取笔记列表")
def list_notes(
    db: DBSession,
    current_user: CurrentUser,
    page: Annotated[int, Query(ge=1, description="页码，从1开始")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="每页数量，1-100")] = 20,
    note_type: Annotated[str | None, Query(max_length=64, description="笔记类型")] = None,
    keyword: Annotated[str | None, Query(max_length=100, description="标题关键词")] = None,
) -> StandardResponse:
    """分页查询当前用户的笔记标题列表。"""
    notes, total, has_more = NoteManagementService.list_notes(
        db=db,
        user_id=current_user.id,
        page=page,
        page_size=page_size,
        note_type=note_type,
        keyword=keyword,
    )
    data = NoteListResponse(
        page=page,
        page_size=page_size,
        total=total,
        has_more=has_more,
        notes=[_to_note_list_item(note) for note in notes],
    )
    return StandardResponse(code=0, message="笔记列表获取成功", data=data)


@router.post("", response_model=StandardResponse, responses=common_error_responses, summary="创建笔记")
def create_note(payload: NoteCreateRequest, db: DBSession, current_user: CurrentUser) -> StandardResponse:
    """创建当前用户笔记。"""
    note = NoteManagementService.create_note(
        db=db,
        user_id=current_user.id,
        title=payload.title,
        content=payload.content,
        note_type=payload.note_type,
        tags=payload.tags,
        metadata=payload.metadata,
        source_run_id=payload.source_run_id,
    )
    return StandardResponse(code=0, message="笔记创建成功", data=NoteDetailResponse(note=_to_note_item(note)))


@router.get("/{note_id}", response_model=StandardResponse, responses=common_error_responses, summary="获取笔记详情")
def get_note(
    note_id: Annotated[int, Path(gt=0, description="笔记ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """获取当前用户单篇笔记详情。"""
    note = NoteManagementService.get_note_or_404(db=db, user_id=current_user.id, note_id=note_id)
    return StandardResponse(code=0, message="笔记详情获取成功", data=NoteDetailResponse(note=_to_note_item(note)))


@router.put("/{note_id}", response_model=StandardResponse, responses=common_error_responses, summary="更新笔记")
def update_note(
    note_id: Annotated[int, Path(gt=0, description="笔记ID")],
    payload: NoteUpdateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """更新当前用户笔记，只修改请求中传入的字段。"""
    note = NoteManagementService.update_note(
        db=db,
        user_id=current_user.id,
        note_id=note_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return StandardResponse(code=0, message="笔记更新成功", data=NoteDetailResponse(note=_to_note_item(note)))


@router.delete("/{note_id}", response_model=StandardResponse, responses=common_error_responses, summary="删除笔记")
def delete_note(
    note_id: Annotated[int, Path(gt=0, description="笔记ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """删除当前用户笔记。"""
    NoteManagementService.delete_note(db=db, user_id=current_user.id, note_id=note_id)
    data = NoteDeleteResponse(message="笔记删除成功", note_id=note_id)
    return StandardResponse(code=0, message="笔记删除成功", data=data)
