"""知识库接口模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库。"""

    name: str = Field(min_length=1, max_length=128, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="说明（可选）")


class KnowledgeBaseUpdateRequest(BaseModel):
    """更新知识库：只改传入的字段。"""

    name: str | None = Field(default=None, min_length=1, max_length=128, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="说明")


class KnowledgeBaseItem(BaseModel):
    """知识库列表项：带构建状态，前端据此画"准备中 / 就绪 / 失败"。

    `quiz_core` 只在**详情**接口里返回（列表里每库一份考纲会让响应变大很多）；
    前端进阅读器时用详情接口拿到的那份来画"考纲 → 跳原文"。
    """

    id: int = Field(description="知识库ID")
    name: str = Field(description="知识库名称")
    description: str | None = Field(default=None, description="说明")
    resource_count: int = Field(description="库内资料数")
    ready_count: int = Field(description="已入库就绪的资料数")
    failed_count: int = Field(description="入库失败的资料数")
    pending_count: int = Field(description="排队/进行中的资料数")
    quiz_status: str = Field(description="考纲状态：pending / extracting / ready / empty / failed")
    quiz_extracted_at: int | None = Field(default=None, description="最近一次考纲提炼完成时间（Unix 秒）")
    can_quiz: bool = Field(description="考纲是否就绪（决定出题时能否注入知识边界）")
    can_retrieve: bool = Field(description="是否至少有一份资料可检索原文")
    created_at: int | None = Field(default=None, description="创建时间（Unix 秒）")
    quiz_core: dict | None = Field(
        default=None, description="提炼出的考纲（只有详情接口返回；章节里带 source_ref 可用于跳原文）"
    )


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应。"""

    total: int = Field(description="总数")
    items: list[KnowledgeBaseItem] = Field(default_factory=list, description="按创建时间倒序")


class KnowledgeBaseDeleteResponse(BaseModel):
    """删除知识库的响应：说明资料被怎么处理了。

    `retention_policy` 是关键：删库**不是**把库里的东西一起删掉，而是"解绑为主的软删除"。
    调用方（前端、脚本）判断"什么东西还在"时应当读它，不要去猜。
    """

    knowledge_base_id: int = Field(description="被删除的知识库ID")
    released_resource_count: int = Field(
        description="被解绑的资料数（切片、向量、原文件都保留，答疑仍检索得到）"
    )
    released_practice_count: int = Field(description="解绑知识库的练习场次数（历史保留）")
    removed_mastery_count: int = Field(
        description="一并删除的掌握度记录数（掌握度的键就是知识库，库没了键就没了）"
    )
    retention_policy: str = Field(
        description=(
            "保留策略的机器可读摘要，分号分隔：resources_unbound（资料解绑保留）/ "
            "practice_history_kept（练习历史保留）/ weak_points_generalized（弱点改通用后继续注入）/ "
            "mastery_removed（掌握度硬删）"
        )
    )


# ---------------------------------------------------------------- 库内文档（阅读器）


class ResourceItem(BaseModel):
    """库内一份文档（资料）的列表项：不含正文，正文走详情接口。"""

    id: int = Field(description="资料ID")
    file_name: str = Field(description="原始文件名")
    group_name: str | None = Field(default=None, description="库内分组名；为空表示未分组")
    ingest_status: str = Field(description="入库状态：pending / ingesting / ready / failed")
    ingest_error: str | None = Field(default=None, description="向量入库失败原因")
    chunk_count: int = Field(description="切片数（0 表示还没入库）")
    char_count: int = Field(description="可阅读正文字符数（优先 content_chars，否则按切片累加）")
    has_text: bool = Field(description="是否能读到正文（content_text 或切片至少有一个）")
    can_preview: bool = Field(description="是否能打开原文件预览（COS 路径 + 文件类型）")
    create_time: int | None = Field(default=None, description="上传时间（Unix 秒）")
    matched_chunk_count: int = Field(
        default=0, description="搜索时该文档命中的片段数（非搜索为 0）"
    )
    match_field: str | None = Field(
        default=None, description="命中位置：name 文件名 / content 正文 / both / null 非搜索"
    )
    match_snippets: list[str] = Field(
        default_factory=list, description="命中的正文片段（最多 3 条，每条截到 120 字）"
    )


class ResourceListResponse(BaseModel):
    """库内文档列表（可按关键词搜索、按分组过滤）。"""

    knowledge_base_id: int = Field(description="知识库ID")
    total: int = Field(description="文档数")
    keyword: str | None = Field(default=None, description="本次搜索关键词（原样回显）")
    group_name: str | None = Field(default=None, description="本次分组过滤条件（原样回显）")
    items: list[ResourceItem] = Field(default_factory=list, description="按上传时间倒序")


class ResourceUpdateRequest(BaseModel):
    """改名 / 移动分组 / 重新归档：只改传入的字段。"""

    file_name: str | None = Field(
        default=None, min_length=1, max_length=255, description="新的文件名"
    )
    group_name: str | None = Field(
        default=None, max_length=64, description="分组名；传空字符串表示移出分组（置空）"
    )
    knowledge_base_id: int | None = Field(
        default=None,
        gt=0,
        description=(
            "重新归档到某个知识库（只能是自己名下的库）。"
            "删库会把库内资料解绑而不是删除，这是把未归档资料收回库里的入口；"
            "改名/移动分组时不要传这个字段。"
        ),
    )


class UnboundResourceItem(BaseModel):
    """一份**未归档**资料（`knowledge_base_id` 为空）。

    这种数据在删知识库时产生：资料被解绑保留（切片、向量、原文件都还在，答疑仍检索得到），
    而库内文档列表必须传库 ID，于是它再也没有可见入口。本模型就是它的展示形状。
    """

    id: int = Field(description="资料ID")
    file_name: str = Field(description="原始文件名")
    group_name: str | None = Field(default=None, description="未归档资料没有库内分组，恒为空")
    knowledge_base_id: None = Field(default=None, description="恒为空（它不属于任何库）")
    ingest_status: str = Field(description="入库状态：pending / ingesting / ready / failed")
    ingest_error: str | None = Field(default=None, description="向量入库失败原因")
    chunk_count: int = Field(description="切片数（0 表示还没入库）")
    char_count: int = Field(description="可阅读正文字符数")
    has_text: bool = Field(description="是否能读到正文")
    can_preview: bool = Field(description="是否能打开原文件预览")
    create_time: int | None = Field(default=None, description="上传时间（Unix 秒）")
    inactive_days: int | None = Field(
        default=None, description="距上传过了多少天（前端用来提示「很久没用过了」）"
    )


class UnboundResourceListResponse(BaseModel):
    """未归档资料列表：删库留下的孤儿资料，给它们一个可见且有处置入口的地方。"""

    total: int = Field(description="未归档资料数")
    items: list[UnboundResourceItem] = Field(default_factory=list, description="按上传时间倒序")


class ReassignResourceRequest(BaseModel):
    """把一份未归档资料重新归档到某个知识库。"""

    knowledge_base_id: int = Field(gt=0, description="目标知识库ID（必须是自己名下的库）")


class ResourceGroupItem(BaseModel):
    """库内分组：纯字符串，不建分组表。"""

    name: str | None = Field(description="分组名；null 表示未分组")
    resource_count: int = Field(description="该组文档数")


class ResourceGroupListResponse(BaseModel):
    """库内分组列表（含未分组）。"""

    knowledge_base_id: int = Field(description="知识库ID")
    total: int = Field(description="分组数")
    items: list[ResourceGroupItem] = Field(default_factory=list, description="按文档数倒序")


class ResourceDeleteResponse(BaseModel):
    """删除单份文档的结果：让用户看清"连切片、向量和原文件一起清了"。"""

    resource_id: int = Field(description="被删除的资料ID")
    file_name: str = Field(description="被删除的文件名")
    knowledge_base_id: int | None = Field(default=None, description="原所属知识库ID")
    deleted_chunk_count: int = Field(description="同时删除的切片数")
    deleted_vector_count: int = Field(
        description="同时删除的 Qdrant 向量数；小于切片数说明向量库当时不可用（这些孤儿向量不会再被检索到）"
    )
    deleted_file: bool = Field(description="是否一并删除了对象存储里的原文件")
    removed_quiz_sections: int = Field(
        default=0,
        description="同时从考纲里摘掉的章节数（章节以这份资料为原文锚点，资料没了就无法再定位原文）",
    )


class ResourceChunkItem(BaseModel):
    """切片项：`chunk_index` 与考纲 `source_ref.chunk_from/chunk_to` 对齐，用于"点考纲跳原文"。"""

    chunk_index: int = Field(description="片段顺序编号，从 0 开始")
    char_count: int = Field(description="片段字符数")
    text: str = Field(description="片段原文")


class ResourceDetailResponse(BaseModel):
    """文档详情：正文 + 切片索引 + 原文预览链接。"""

    id: int = Field(description="资料ID")
    knowledge_base_id: int | None = Field(default=None, description="所属知识库ID")
    file_name: str = Field(description="原始文件名")
    group_name: str | None = Field(default=None, description="库内分组名")
    ingest_status: str = Field(description="入库状态")
    ingest_error: str | None = Field(default=None, description="入库失败原因")
    create_time: int | None = Field(default=None, description="上传时间（Unix 秒）")
    text: str = Field(description="可阅读正文")
    text_chars: int = Field(description="正文字符数")
    text_source: str = Field(
        description="正文来源：original 入库时存的解析全文 / chunks 用切片拼回（可能有排版损失）/ none 读不到"
    )
    chunk_count: int = Field(description="切片数")
    chunks: list[ResourceChunkItem] = Field(
        default_factory=list, description="全部切片（按 chunk_index 升序），供定位与核对"
    )
    preview_path: str | None = Field(
        default=None,
        description=(
            "原文件的服务端路径（如 /knowledge-bases/1/resources/2/file）；"
            "**要带 Authorization 头请求**，前端不能直接把它当链接打开。"
            "为空表示这份资料没有可预览的原文件。"
        ),
    )
    can_preview: bool = Field(description="是否能预览原文件")


class ResourceChunksResponse(BaseModel):
    """单个切片：按 `chunk_index` 取，供考纲章节跳转。"""

    resource_id: int = Field(description="资料ID")
    resource_name: str = Field(description="文件名（前端用来确认跳到了哪份资料）")
    chunk: ResourceChunkItem = Field(description="该顺序号的切片")
