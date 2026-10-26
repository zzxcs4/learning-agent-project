"""笔记管理接口请求与响应模型。"""

from typing import Any

from pydantic import BaseModel, Field


class NoteItem(BaseModel):
    """笔记基础信息。"""

    # 笔记主键 ID。
    id: int
    # 所属用户 ID。
    user_id: int
    # 来源会话 ID；手动创建笔记时为空。
    session_id: int | None = None
    # 来源工作流运行 ID；手动创建笔记时为空。
    source_run_id: str | None = None
    # 笔记类型。
    note_type: str | None = None
    # 笔记标题。
    title: str
    # Markdown 正文。
    content: str
    # 知识点标签。
    tags: list[str] = Field(default_factory=list)
    # 扩展元数据。
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 创建时间 Unix 秒。
    created_at: int
    # 更新时间 Unix 秒。
    updated_at: int


class NoteListItem(BaseModel):
    """笔记列表项，只返回标题列表展示所需字段。"""

    # 笔记主键 ID，用于进入详情。
    id: int
    # 笔记类型。
    note_type: str | None = None
    # 笔记标题。
    title: str
    # 创建时间 Unix 秒。
    created_at: int
    # 更新时间 Unix 秒。
    updated_at: int


class NoteListResponse(BaseModel):
    """笔记分页列表响应。"""

    # 当前页码。
    page: int
    # 每页数量。
    page_size: int
    # 总记录数。
    total: int
    # 是否还有下一页。
    has_more: bool
    # 当前页笔记标题列表。
    notes: list[NoteListItem]


class NoteDetailResponse(BaseModel):
    """笔记详情响应。"""

    # 笔记详情。
    note: NoteItem


class NoteCreateRequest(BaseModel):
    """创建笔记请求体。"""

    # 笔记标题。
    title: str = Field(min_length=1, max_length=255)
    # Markdown 正文。
    content: str = Field(min_length=1)
    # 笔记类型。
    note_type: str | None = Field(default=None, max_length=64)
    # 标签列表。
    tags: list[str] = Field(default_factory=list)
    # 扩展元数据。
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 来源工作流运行 ID；手动创建笔记时不传。
    source_run_id: str | None = Field(default=None, min_length=1, max_length=64)


class NoteUpdateRequest(BaseModel):
    """更新笔记请求体。"""

    # 新标题。
    title: str | None = Field(default=None, min_length=1, max_length=255)
    # 新 Markdown 正文。
    content: str | None = Field(default=None, min_length=1)
    # 新笔记类型；传 null 可清空。
    note_type: str | None = Field(default=None, max_length=64)
    # 新标签列表。
    tags: list[str] | None = None
    # 新扩展元数据。
    metadata: dict[str, Any] | None = None


class NoteDeleteResponse(BaseModel):
    """删除笔记响应。"""

    # 删除结果文案。
    message: str
    # 被删除的笔记 ID。
    note_id: int
