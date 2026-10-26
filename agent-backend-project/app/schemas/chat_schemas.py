"""聊天相关 Schema：消息、分页、删除会话、流式请求。"""

from pydantic import BaseModel, ConfigDict, Field


class MessageSegment(BaseModel):
    """附件片段（当前仅约定 file/image/audio）。"""

    # 兼容历史数据中的冗余字段，避免序列化时报错。
    model_config = ConfigDict(extra="ignore")

    type: str = Field(description="附件类型：file/image/audio")
    name: str | None = Field(default=None, description="附件显示名称")
    url: str | None = Field(default=None, description="附件访问地址（带 storage_path 时会现签）")
    resource_id: int | None = Field(default=None, description="资源ID")
    storage_path: str | None = Field(
        default=None,
        description="对象存储路径（cos://bucket/key）：系统生成的附件靠它现签新 URL，避免预签名过期",
    )


class KnowledgeSource(BaseModel):
    """AI 回答引用的知识库片段来源。"""

    model_config = ConfigDict(extra="ignore")

    chunk_index: int | None = None
    file_name: str = ""
    score: float = 0
    text: str | None = None


class ChatMessageItem(BaseModel):
    """会话消息行（含用户问句与助手回复）。"""

    id: int  # chat_messages 主键
    request_id: str  # 请求唯一标识
    status: int  # 消息状态（与数据库状态码保持一致）
    request_text: str  # 用户输入文本
    response_text: str  # 助手输出文本（流式完成后为最终全文）
    request_segments: list[MessageSegment] = Field(default_factory=list)  # 用户侧附件片段
    response_segments: list[MessageSegment] = Field(default_factory=list)  # 助手侧附件片段
    response_payload: dict | None = None  # 结构化响应载荷（如刷题表单、批改结果）
    sources: list[KnowledgeSource] = Field(default_factory=list)  # 本次回答引用来源
    created_at: int  # 创建时间戳（秒）


class ChatMessagePageResponse(BaseModel):
    """会话消息分页返回。"""

    session_id: int  # 所属会话ID
    page: int  # 当前页（从1开始）
    page_size: int  # 每页条数
    has_more: bool  # 是否存在下一页
    messages: list[ChatMessageItem]  # 当前页消息列表


class DeleteSessionResponse(BaseModel):
    """删除会话返回摘要。"""

    message: str  # 人类可读提示
    session_id: int  # 被删除的会话ID
    deleted_messages: int  # 删除的消息条数
    deleted_resources: int  # 删除的资源条数


class StreamChatRequest(BaseModel):
    """流式聊天接口请求体。"""

    request_text: str = Field(min_length=1, max_length=8000, description="用户输入")
    # 与数据库 chat_messages.select_model 字段保持一致。
    select_model: int = Field(default=0, ge=0, le=5, description="模型选择标记")
    # 轻量扩展口：当前默认 deepseek，后续可扩 provider。
    provider: str = Field(default="deepseek", description="模型提供方")
