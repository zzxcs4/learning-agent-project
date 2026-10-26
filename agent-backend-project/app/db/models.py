"""项目 ORM 模型定义。"""

import time
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, func
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _unix_ts() -> int:
    """返回当前 Unix 秒时间戳。"""
    return int(time.time())


class User(Base):
    """用户表：系统登录主体。"""

    __tablename__ = "users"

    # 自增主键，内部唯一标识。
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # unique=True 数据库层防重名；index=True 便于注册时唯一性查询。
    username: Mapped[str] = mapped_column(
        String(20), unique=True, index=True, nullable=False)
    # 仅存密码哈希，不存明文。
    password: Mapped[str] = mapped_column(String(255), nullable=False)
    avatar: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 插入时未赋值则由 ORM 用 default 写入当前 Unix 秒。
    create_time: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=_unix_ts,
        comment="创建时间 Unix 秒",
    )


class UserProfile(Base):
    """用户个人信息表：存储已提取的用户信息（来源：简历解析或表单填写）"""

    __tablename__ = "user_profiles"
    __table_args__ = (
        Index("idx_user_id", "user_id"),
        {"comment": "用户个人信息表"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="主键")
    
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), 
        nullable=False, unique=True, 
        comment="用户FK（一对一关系）")
    
    target_job: Mapped[str | None] = mapped_column(
        String(128), nullable=True, 
        comment="目标岗位，如'Python后端工程师'")
    
    years_experience: Mapped[str | None] = mapped_column(
        String(64), nullable=True, 
        comment="工作经验，如'3年'")
    
    target_level: Mapped[str | None] = mapped_column(
        String(32), nullable=True, 
        comment="目标等级：初级/中级/高级")
    
    target_skills: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, 
        comment="已掌握技能 JSON数组")
    
    weak_topics: Mapped[list[str] | None] = mapped_column(
        JSON, nullable=True, 
        comment="薄弱点 JSON数组（旧字段，第 1 期起停用，写入方统一为 weak_points）")

    weak_points: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True,
        comment=(
            "富结构弱点列表 JSON 数组（取代 weak_topics）："
            '[{"point","knowledge_base_id","times_seen","first_seen","last_seen",'
            '"improved","archived","next_review","ease_factor","axis","history"}]'
        ))
    
    learning_goal: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="当前学习目标，如'准备中级后端面试'")
    
    learning_style: Mapped[str | None] = mapped_column(
        String(255), nullable=True,
        comment="学习偏好，如'喜欢案例驱动'")
    
    long_term_summary: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="长期画像摘要，用于 Prompt 展示")
    
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, 
        default=_unix_ts,
        comment="创建时间 Unix秒")
    
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, 
        default=_unix_ts, onupdate=_unix_ts,
        comment="更新时间 Unix秒")


class Session(Base):
    """会话表：承载用户的学习/笔记会话。"""

    __tablename__ = "sessions"
    __table_args__ = {"comment": "会话表"}

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False, comment="用户 FK"
    )
    session_model: Mapped[int] = mapped_column(
        nullable=False, comment="会话模式：0=学习，2=笔记（1 原为面试，已停用）")
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="标题")
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=_unix_ts,
        comment="创建时间 Unix 秒",
    )


class ChatMessage(Base):
    """消息表：记录用户请求与模型响应。"""

    __tablename__ = "chat_messages"
    __table_args__ = (
        # 按会话拉取消息并按时间排序时走索引，避免全表扫。
        Index("idx_session_id_created_at", "session_id", "created_at"),
        {"comment": "消息表"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(ForeignKey(
        "users.id"), nullable=False, comment="用户 FK")
    session_id: Mapped[int] = mapped_column(ForeignKey(
        "sessions.id"), nullable=False, comment="会话 FK")
    select_model: Mapped[int] = mapped_column(
        nullable=False,
        comment="选择模式：0=默认，1=知识精讲，2=刷题，3=简历优化（4、5 原为模拟面试/面试复盘，已停用）",
    )
    request_id: Mapped[str] = mapped_column(
        String(64), index=True, nullable=False, comment="请求唯一标识"
    )
    # MEDIUMTEXT：单条请求/回复可能很长。
    request_text: Mapped[str] = mapped_column(
        MEDIUMTEXT, nullable=False, comment="用户请求文本")
    response_text: Mapped[str] = mapped_column(
        MEDIUMTEXT, nullable=False, comment="模型响应文本")
    # 仅存附件段信息（file/image/audio），不存聊天正文文本。
    request_segments: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True, comment="用户请求附件段 JSON")
    # 仅存附件段信息（file/image/audio），不存聊天正文文本。
    response_segments: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True, comment="模型响应附件段 JSON")
    response_payload: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="模型响应结构化载荷 JSON")
    reference_sources: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True, comment="本次回答引用的知识片段来源")
    # 消息状态：0=常规聊天（1 原为面试转写入口，已停用）。
    status: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="消息状态：0常规聊天（1 原为面试转写入口，已停用）",
    )
    file_extracted_text: Mapped[str] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="从文件中总结的文本(对话上下文用)")
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=_unix_ts,
        comment="创建时间 Unix 秒",
    )


class Resource(Base):
    """资源治理总表：统一管理文件、图片、音频元数据。"""

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("file_hash", "user_id", name="uk_file_hash_user_id"),
        # user_id 是外键；它原来的支撑索引是 (user_id, doc_category) 复合索引，
        # 那个维度删除后必须留一个独立索引，否则外键没有支撑索引（MySQL 会拒绝删旧索引）。
        Index("ix_resources_user_id", "user_id"),
        {"comment": "资源治理总表（单表方案）"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="资源主键ID",
    )
    resource_type: Mapped[int] = mapped_column(
        nullable=False, comment="资源类型：0=文件，1=图片，2=音频"
    )
    storage_scene: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="存储场景：0=长过期，1=短过期，2=只提取内容不存原文件，3=永久存储",
    )
    upload_purpose: Mapped[int] = mapped_column(
        nullable=False,
        default=0,
        server_default="0",
        comment="上传用途：0=普通资源，1=用户头像",
    )
    file_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="原始文件名")
    file_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="文件MD5")
    storage_path: Mapped[str] = mapped_column(
        String(512), nullable=False, comment="存储路径")
    knowledge_base_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id"),
        index=True,
        nullable=True,
        comment="所属知识库 FK（knowledge_bases.id）；为空表示未归档到知识库",
    )
    group_name: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="知识库内的分组名（纯字符串，不建分组表）",
    )
    ingest_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
        comment="向量入库状态：pending 待处理 / ingesting 进行中 / ready 就绪 / failed 失败",
    )
    ingest_error: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="向量入库失败原因（截断 500 字）")
    content_text: Mapped[str | None] = mapped_column(
        MEDIUMTEXT,
        nullable=True,
        comment="解析清洗后的全文（入库时落库，供知识库阅读器查看原文；为空时用 knowledge_chunks 拼回）",
    )
    content_chars: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="content_text 字符数（列表页展示，避免拉全文）")
    user_id: Mapped[int] = mapped_column(ForeignKey(
        "users.id"), nullable=False, comment="上传用户ID")
    expire_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="过期时间")
    create_time: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        comment="创建时间",
    )


class KnowledgeChunk(Base):
    """知识库片段表：存每个文本切片的原文与顺序，作为 Qdrant 的数据源。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("idx_resource_id", "resource_id"),
        Index("idx_vector_id", "vector_id"),
        {"comment": "知识库片段表：Qdrant 向量库的权威原文存储"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="主键")
    resource_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("resources.id"),
        nullable=False,
        comment="关联资源 FK（resources.id）",
    )
    chunk_index: Mapped[int] = mapped_column(
        nullable=False, comment="片段顺序编号，从 0 开始")
    text: Mapped[str] = mapped_column(
        MEDIUMTEXT, nullable=False, comment="片段原文（权威来源）")
    char_count: Mapped[int] = mapped_column(
        nullable=False, comment="片段字符数")
    vector_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="对应 Qdrant Point 的 UUID（双向关联键）",
    )
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=_unix_ts,
        comment="创建时间 Unix 秒",
    )


class WorkflowRun(Base):
    """工作流运行记录：保存跨请求执行的图状态。"""

    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("idx_user_workflow_status", "user_id", "workflow_type", "status"),
        Index("idx_session_workflow", "session_id", "workflow_type"),
        {"comment": "工作流运行记录表"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="主键")
    run_id: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False, comment="工作流运行ID")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False, comment="用户 FK")
    session_id: Mapped[int] = mapped_column(
        ForeignKey("sessions.id"), index=True, nullable=False, comment="会话 FK")
    workflow_type: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="工作流类型，如 learning_quiz")
    status: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, comment="运行状态")
    state_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, comment="可恢复的工作流状态 JSON")
    error_message: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="失败原因")
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, onupdate=_unix_ts, comment="更新时间 Unix 秒")
    completed_at: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="完成时间 Unix 秒")


class Note(Base):
    """笔记表：保存智能体生成的 Markdown 笔记。"""

    __tablename__ = "notes"
    __table_args__ = (
        UniqueConstraint("user_id", "source_run_id", name="uk_note_source"),
        Index("idx_user_note_type", "user_id", "note_type"),
        {"comment": "笔记表"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False, comment="用户 FK")
    session_id: Mapped[int | None] = mapped_column(index=True, nullable=True, comment="来源会话ID")
    source_run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True, comment="来源工作流运行ID")
    note_type: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="笔记类型")
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="笔记标题")
    content: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False, comment="Markdown 正文")
    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True, comment="知识点标签 JSON数组")
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True, comment="扩展元数据 JSON")
    created_at: Mapped[int] = mapped_column(BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, onupdate=_unix_ts, comment="更新时间 Unix 秒")


class PromptTemplate(Base):
    """提示词模板版本管理表：统一管理各智能体的提示词模板，支持版本控制和复用。"""

    __tablename__ = "prompt_templates"
    __table_args__ = (
        Index("idx_agent_scene", "agent_name", "scene"),
        {"comment": "提示词模板版本管理表"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="模板ID")
    agent_name: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="智能体名称，公共模板此字段为空")
    scene: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="业务场景")
    template_type: Mapped[int] = mapped_column(
        default=1,
        server_default="1",
        comment="模板类型：1-私有 2-公共",
    )
    name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="模板名称")
    description: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="版本变更说明")
    template_content: Mapped[str] = mapped_column(
        MEDIUMTEXT, nullable=False, comment="提示词正文（含变量占位）")
    variables: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment='模板变量JSON列表，如["target_job","weak_points"]',
    )
    version: Mapped[int] = mapped_column(
        nullable=False, default=1, server_default="1", comment="版本号，整数自增")
    is_active: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
        comment="是否生效：1-是 0-否",
    )
    created_at: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=_unix_ts,
        comment="创建时间 Unix 秒",
    )


# ---------------------------------------------------------------------------
# 第 1 期闭环新增模型：知识库 / 练习套题 / 题目 / 答题记录 / 知识库级掌握度
# 说明：本机 MySQL 5.7 没有 JSONB 类型，设计文档里的 JSONB 列在这里统一落为 JSON 列
#       （与 chat_messages / user_profiles 现有 JSON 列一致）。
# ---------------------------------------------------------------------------


class KnowledgeBase(Base):
    """知识库表：用户多知识库（一层），同时是掌握度的粒度。"""

    __tablename__ = "knowledge_bases"
    __table_args__ = {"comment": "知识库表（用户多知识库 + 出题知识库）"}

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False, comment="用户 FK")
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="知识库名称")
    quiz_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
        comment="考纲提炼状态：pending 待提炼 / extracting 进行中 / ready 就绪 / empty 无可提炼内容 / failed 失败",
    )
    quiz_extracted_at: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最近一次考纲提炼完成时间 Unix 秒")
    description: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="知识库说明")
    quiz_core: Mapped[list[dict] | None] = mapped_column(
        JSON,
        nullable=True,
        comment=(
            "出题知识库（考纲）：从原始资料提炼出的知识边界，不是摘要。"
            "结构 {title, frequently_tested[], followup_paths[], "
            "sections[{heading, topics[], source_ref{resource_id,chunk_from,chunk_to}, "
            "asked_count, last_asked_at}]}；不被掌握度引用"
        ),
    )
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, onupdate=_unix_ts, comment="更新时间 Unix 秒")


class PracticeSet(Base):
    """练习题套表：一次 AI 出题 = 一次练习，可按知识库分组查询。"""

    __tablename__ = "practice_sets"
    __table_args__ = (
        Index("idx_practice_user_knowledge_base", "user_id", "knowledge_base_id"),
        Index("ix_practice_sets_run_id", "run_id"),
        {"comment": "练习题套表：按知识库分组的练习历史"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, comment="用户 FK")
    knowledge_base_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id"),
        nullable=True,
        comment="知识库 FK；为空表示本次练习不属于任何知识库",
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="关联的学习测评 workflow run_id（无外键）。用于回看该类场练习的 review/reflection，也供归档工具定位",
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False, comment="练习标题")
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="waiting_answer",
        server_default="waiting_answer",
        comment="练习状态：waiting_answer/completed/failed",
    )
    total_score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="评测总分（0-100）")
    question_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0", comment="题目数量")
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, onupdate=_unix_ts, comment="更新时间 Unix 秒")


class Question(Base):
    """题目表：把题目从 workflow_runs.state_json 搬出来，变得可查询 / 可统计。"""

    __tablename__ = "questions"
    __table_args__ = (
        Index("idx_question_practice_set", "practice_set_id", "question_code"),
        {"comment": "题目表：练习套题下的逐题记录"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    practice_set_id: Mapped[int] = mapped_column(
        ForeignKey("practice_sets.id"), nullable=False, comment="所属练习套题 FK")
    question_code: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="题号（q1/q2…）：与 state_json 里 quiz.questions[].id、作答 question_id 对齐",
    )
    focus_area: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="出题模型顺手标注的考点短语：只用于展示与去重，不做掌握度键（会漂移）",
    )
    question_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="题型：radio/checkbox/textarea")
    stem: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False, comment="题干")
    options: Mapped[list[dict] | None] = mapped_column(
        JSON, nullable=True, comment='选项 JSON 数组 [{"value","label"}]，简答题为空')
    answer: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="参考答案 JSON（由评测节点回写 correct_answer）")
    points: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0", comment="本题分值")
    difficulty: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="难度系数（出题模型未标注时为空，掌握度加权用）")
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")


class Attempt(Base):
    """答题记录表：闭环关键——掌握度、趋势、统计全部由它聚合而来。"""

    __tablename__ = "attempts"
    __table_args__ = (
        Index("idx_attempt_user_question", "user_id", "question_id"),
        {"comment": "答题记录表：一次作答 = 一条记录"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    question_id: Mapped[int] = mapped_column(
        ForeignKey("questions.id"), nullable=False, comment="题目 FK")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, comment="用户 FK")
    answer: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment='用户原始作答 JSON：{"value": 作答值}')
    score: Mapped[float | None] = mapped_column(
        Float, nullable=True, comment="本题得分")
    is_correct: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="本题是否正确（模型未判定时为空）")
    analysis: Mapped[str | None] = mapped_column(
        MEDIUMTEXT, nullable=True, comment="本题点评")
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")


class UserKnowledgeMastery(Base):
    """知识库级掌握度表：掌握度的权威存储，键 = (user_id, knowledge_base_id)。"""

    __tablename__ = "user_knowledge_mastery"
    __table_args__ = (
        UniqueConstraint("user_id", "knowledge_base_id", name="uk_user_knowledge_mastery"),
        {"comment": "知识库级掌握度表（EMA 滚动结果）"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True, comment="主键")
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, comment="用户 FK")
    knowledge_base_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id"), nullable=False, comment="知识库 FK（掌握度键）")
    mastery_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0", comment="掌握度分数 0-100（EMA 滚动结果）")
    practice_count: Mapped[int] = mapped_column(
        nullable=False, default=0, server_default="0",
        comment="参与过评分的练习场次（不用 session_count，避免与 sessions 会话表混淆）")
    last_assessed_at: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="最近一次评测时间 Unix 秒")
    remark: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="备注：分区（补课/过渡/优势）等展示信息")
    created_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, comment="创建时间 Unix 秒")
    updated_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=_unix_ts, onupdate=_unix_ts, comment="更新时间 Unix 秒")
