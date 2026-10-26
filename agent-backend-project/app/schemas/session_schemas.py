"""会话管理接口响应模型。"""

from pydantic import BaseModel, Field


class SessionItem(BaseModel):
    """会话基础信息。"""

    # 会话主键ID。
    id: int
    # 会话模式：0=学习，2=笔记（1 原为面试，已停用）。
    session_model: int
    # 会话标题。
    title: str
    # 创建时间（Unix 秒）。
    created_at: int


class SessionListResponse(BaseModel):
    """会话分页列表响应。"""

    # 当前页码（从1开始）。
    page: int
    # 每页大小。
    page_size: int
    # 总记录数。
    total: int
    # 是否还有下一页。
    has_more: bool
    # 当前页会话数据。
    sessions: list[SessionItem]


class SessionCreateRequest(BaseModel):
    """创建会话请求体。"""

    # 会话模式：0=学习，2=笔记（1 原为面试，已停用）。
    session_model: int = Field(default=0, ge=0, le=2)
    # 会话主题标题。
    title: str = Field(min_length=1, max_length=255)


class SessionCreateResponse(BaseModel):
    """创建会话响应体。"""

    # 创建结果文案。
    message: str
    # 新建会话数据。
    session: SessionItem


class SessionUpdateTitleRequest(BaseModel):
    """更新会话标题请求体。"""

    # 新标题。
    title: str = Field(min_length=1, max_length=255)


class SessionUpdateTitleResponse(BaseModel):
    """更新会话标题响应体。"""

    # 更新结果文案。
    message: str
    # 更新后的会话数据。
    session: SessionItem

