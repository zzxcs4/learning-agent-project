"""知识库路由：只暴露"知识库"这一层（资料是库的内部组成，不单独成栏）。

状态说明：`quiz_status` 与 `can_quiz` 让前端知道"能不能开始出题"；
`ready_count / pending_count / failed_count` 让用户看到"资料准备到哪一步"。

文档阅读（2026-10-18 新增）：`/knowledge-bases/{id}/resources` 列库内文档，
`/knowledge-bases/{id}/resources/{rid}` 读正文与切片，`/chunks/{index}` 按考纲
`source_ref` 定位到具体片段，`/file` 转发原文件给浏览器内联预览。全是只读接口。
"""

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core import StandardResponse, common_error_responses, raise_error
from app.core.auth import get_current_user
from app.db import get_db
from app.db.models import User
from app.schemas import (
    KnowledgeBaseCreateRequest,
    KnowledgeBaseDeleteResponse,
    KnowledgeBaseItem,
    KnowledgeBaseListResponse,
    KnowledgeBaseUpdateRequest,
    ReassignResourceRequest,
    ResourceChunkItem,
    ResourceChunksResponse,
    ResourceDeleteResponse,
    ResourceDetailResponse,
    ResourceGroupItem,
    ResourceGroupListResponse,
    ResourceItem,
    ResourceListResponse,
    ResourceUpdateRequest,
    UnboundResourceItem,
    UnboundResourceListResponse,
)
from app.services.knowledge_base_service import KnowledgeBaseService

router = APIRouter()
DBSession = Annotated[Session, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]

# 扩展名 → 浏览器用的 MIME（内联预览时自己声明，不依赖 COS 上传时存的类型）
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
}
# 能内联打开的（其余走下载，浏览器本来也渲染不了）
_INLINE_TYPES = {".pdf", ".txt", ".md"}


@router.get(
    "",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="知识库列表（带构建状态）",
)
def list_knowledge_bases(db: DBSession, current_user: CurrentUser) -> StandardResponse:
    """返回当前用户的知识库，按创建时间倒序，附资料就绪数与考纲状态。"""
    items = KnowledgeBaseService.list_knowledge_bases(db, user_id=current_user.id)
    data = KnowledgeBaseListResponse(
        total=len(items), items=[KnowledgeBaseItem(**item) for item in items]
    )
    return StandardResponse(code=0, message="知识库列表获取成功", data=data)


@router.post(
    "",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="创建知识库",
)
def create_knowledge_base(
    payload: KnowledgeBaseCreateRequest, db: DBSession, current_user: CurrentUser
) -> StandardResponse:
    """新建空库；随后上传资料并带上 `knowledge_base_id` 即会异步构建。"""
    item = KnowledgeBaseService.create(
        db, user_id=current_user.id, name=payload.name, description=payload.description
    )
    return StandardResponse(
        code=0, message="知识库创建成功", data=KnowledgeBaseItem(**item)
    )


@router.get(
    "/unbound-resources",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="未归档资料列表（删库留下的孤儿资料）",
)
def list_unbound_resources(db: DBSession, current_user: CurrentUser) -> StandardResponse:
    """列出**不属于任何知识库**的资料。

    这些数据是"删知识库"留下的：删库时资料被**解绑**而不是删除（切片、向量、原文件全留，
    答疑仍能检索到），而 `/knowledge-bases/{id}/resources` 必须传库 ID，
    于是它们在用户侧失去了所有入口。本接口是它们的可见入口，`PATCH` 是回收入口。

    ⚠️ 这条路由必须注册在 `/{knowledge_base_id}` 这类路径参数路由**之前**，
    否则 `/knowledge-bases/unbound-resources` 会被当成"库 ID = unbound-resources"。
    """
    items = KnowledgeBaseService.list_unbound_resources(db, user_id=current_user.id)
    data = UnboundResourceListResponse(
        total=len(items),
        items=[UnboundResourceItem(**item) for item in items],
    )
    return StandardResponse(code=0, message="未归档资料获取成功", data=data)


@router.patch(
    "/unbound-resources/{resource_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="把未归档资料重新归档到某个知识库",
)
def reassign_unbound_resource(
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    payload: ReassignResourceRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """重新归档是**纯元数据变更**：切片与向量从来没被删过，所以不会重算、也不会重新 embedding。

    目标库必须是本人的，否则等于把资料塞进别人的库（`_require_own_knowledge_base` 会 404）。
    """
    detail = KnowledgeBaseService.reassign_resource(
        db,
        user_id=current_user.id,
        resource_id=resource_id,
        knowledge_base_id=payload.knowledge_base_id,
    )
    return StandardResponse(code=0, message="已重新归档", data=ResourceDetailResponse(**detail))


@router.delete(
    "/unbound-resources/{resource_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="彻底删除一份未归档资料（切片、向量、原文件一起清）",
)
def delete_unbound_resource(
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    db: DBSession,
    current_user: CurrentUser,
    delete_file: Annotated[
        bool, Query(description="是否一并删除对象存储里的原文件（默认 true）")
    ] = True,
) -> StandardResponse:
    """未归档资料不属于任何库，所以这里没有库 ID 可取。

    复用库内删除的同一套服务逻辑（`delete_resource` 只按 `(user_id, resource_id)` 鉴权，
    并要求该资料确实解绑了——否则应当走库内那条路由，避免"传个假库 ID 就能绕过归属校验"）。
    """
    resource = KnowledgeBaseService.get_resource_or_404(
        db, user_id=current_user.id, resource_id=resource_id
    )
    if resource.knowledge_base_id is not None:
        raise_error(
            code=409,
            message="这份资料还属于某个知识库，请在库内文档列表里删除",
        )
    result = KnowledgeBaseService.delete_resource(
        db, user_id=current_user.id, resource_id=resource_id, delete_file=delete_file
    )
    return StandardResponse(
        code=0, message="未归档资料已删除", data=ResourceDeleteResponse(**result)
    )


@router.get(
    "/{knowledge_base_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="知识库详情（含构建状态）",
)
def get_knowledge_base(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """单库详情：资料数、就绪数、考纲状态。"""
    item = KnowledgeBaseService.get_detail(
        db, user_id=current_user.id, knowledge_base_id=knowledge_base_id
    )
    return StandardResponse(
        code=0, message="知识库详情获取成功", data=KnowledgeBaseItem(**item)
    )


@router.patch(
    "/{knowledge_base_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="更新知识库（名称 / 说明）",
)
def update_knowledge_base(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    payload: KnowledgeBaseUpdateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """只更新名称与说明；状态与考纲由构建流水线维护，不接受外部修改。"""
    item = KnowledgeBaseService.update(
        db,
        user_id=current_user.id,
        knowledge_base_id=knowledge_base_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return StandardResponse(
        code=0, message="知识库更新成功", data=KnowledgeBaseItem(**item)
    )


@router.get(
    "/{knowledge_base_id}/resources",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="库内文档列表（阅读器左栏；支持搜索与分组过滤）",
)
def list_knowledge_base_resources(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    db: DBSession,
    current_user: CurrentUser,
    keyword: Annotated[
        str | None,
        Query(max_length=100, description="库内搜索：文件名或切片正文命中即返回（留空=不过滤）"),
    ] = None,
    group_name: Annotated[
        str | None, Query(max_length=64, description="只看某个分组（留空=不过滤）")
    ] = None,
) -> StandardResponse:
    """列出这个库里的资料：文件名、状态、切片数、可读字数、能否预览原文（**不含正文**）。

    传了 `keyword` 会附带 `match_field` / `matched_chunk_count` / `match_snippets`，
    前端据此说明"为什么这份文档出现在结果里"。
    """
    items = KnowledgeBaseService.list_resources(
        db,
        user_id=current_user.id,
        knowledge_base_id=knowledge_base_id,
        keyword=keyword,
        group_name=group_name,
    )
    data = ResourceListResponse(
        knowledge_base_id=knowledge_base_id,
        total=len(items),
        keyword=(keyword or None),
        group_name=(group_name or None),
        items=[ResourceItem(**item) for item in items],
    )
    return StandardResponse(code=0, message="库内文档获取成功", data=data)


@router.get(
    "/{knowledge_base_id}/groups",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="库内分组列表（含未分组）",
)
def list_knowledge_base_groups(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """分组由 `resources.group_name` 这个纯字符串列反推，没有分组表，也就没有单独的"重命名分组"接口。"""
    items = KnowledgeBaseService.list_groups(
        db, user_id=current_user.id, knowledge_base_id=knowledge_base_id
    )
    data = ResourceGroupListResponse(
        knowledge_base_id=knowledge_base_id,
        total=len(items),
        items=[ResourceGroupItem(**item) for item in items],
    )
    return StandardResponse(code=0, message="库内分组获取成功", data=data)


@router.patch(
    "/{knowledge_base_id}/resources/{resource_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="文档改名 / 移动分组",
)
def update_knowledge_base_resource(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    payload: ResourceUpdateRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """只改文件名与分组（`group_name` 传空字符串表示移出分组）；入库状态与切片不接受外部修改。"""
    resource = KnowledgeBaseService.get_resource_or_404(
        db, user_id=current_user.id, resource_id=resource_id
    )
    if resource.knowledge_base_id != knowledge_base_id:
        raise_error(code=404, message="该资料不属于这个知识库")
    detail = KnowledgeBaseService.update_resource(
        db,
        user_id=current_user.id,
        resource_id=resource_id,
        changes=payload.model_dump(exclude_unset=True),
    )
    return StandardResponse(code=0, message="文档已更新", data=ResourceDetailResponse(**detail))


@router.delete(
    "/{knowledge_base_id}/resources/{resource_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="删除单份文档（连切片、向量与原文件一起删）",
)
def delete_knowledge_base_resource(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    db: DBSession,
    current_user: CurrentUser,
    delete_file: Annotated[
        bool,
        Query(description="是否一并删除对象存储里的原文件（默认 true；false 则只从库里移除）"),
    ] = True,
) -> StandardResponse:
    """**不可撤销**：切片、Qdrant 向量、原文件都会被清掉。

    `deleted_vector_count` 可能小于切片数——Qdrant 当时不可用时只记日志、不阻塞删除
    （这些孤儿向量不会再被检索到：检索走 `vector_id` 回查 MySQL，查不到就丢弃）。
    """
    resource = KnowledgeBaseService.get_resource_or_404(
        db, user_id=current_user.id, resource_id=resource_id
    )
    if resource.knowledge_base_id != knowledge_base_id:
        raise_error(code=404, message="该资料不属于这个知识库")
    result = KnowledgeBaseService.delete_resource(
        db, user_id=current_user.id, resource_id=resource_id, delete_file=delete_file
    )
    return StandardResponse(code=0, message="文档已删除", data=ResourceDeleteResponse(**result))


@router.get(
    "/{knowledge_base_id}/resources/{resource_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="文档详情（正文 + 切片 + 原文预览链接）",
)
def get_knowledge_base_resource(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """读一份资料的正文。

    `text_source` 说明正文来自哪里：`original` = 入库时存的解析全文（推荐，排版最完整）；
    `chunks` = 用切片拼回（老数据，块间重叠已消除）；`none` = 还没入库成功，读不到正文。
    `preview_path` 是原文件的服务端路径，**要带 Authorization 头**请求（走 `/file` 接口）。
    """
    detail = KnowledgeBaseService.get_resource_detail(
        db, user_id=current_user.id, resource_id=resource_id
    )
    data = ResourceDetailResponse(**detail)
    return StandardResponse(code=0, message="文档详情获取成功", data=data)


@router.get(
    "/{knowledge_base_id}/resources/{resource_id}/chunks/{chunk_index}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="按顺序号取片段（考纲章节跳转用）",
)
def get_knowledge_base_resource_chunk(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    chunk_index: Annotated[int, Path(ge=0, description="片段顺序号，从 0 开始")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """考纲 `source_ref` 里存的是 `{resource_id, chunk_from, chunk_to}`，用它直接定位原文片段。"""
    result = KnowledgeBaseService.get_chunk(
        db, user_id=current_user.id, resource_id=resource_id, chunk_index=chunk_index
    )
    data = ResourceChunksResponse(
        resource_id=result["resource_id"],
        resource_name=result["resource_name"],
        chunk=ResourceChunkItem(**result["chunk"]),
    )
    return StandardResponse(code=0, message="片段获取成功", data=data)


@router.get(
    "/{knowledge_base_id}/resources/{resource_id}/file",
    response_class=StreamingResponse,
    responses=common_error_responses,
    summary="原文件（后端转发，供浏览器内联预览）",
)
def get_knowledge_base_resource_file(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    resource_id: Annotated[int, Path(gt=0, description="资料ID")],
    db: DBSession,
    current_user: CurrentUser,
    download: Annotated[
        bool, Query(description="true 强制下载；默认 false 由浏览器内联打开（PDF/TXT/MD）")
    ] = False,
):
    """把 COS 里的原文件流式转发给浏览器。

    **为什么不让前端直接用 COS 链接**：2024-01-01 之后创建的存储桶，用 COS 默认域名
    访问一律被强制下载（COS 会覆写 `Content-Disposition: attachment`），
    `response-content-disposition=inline` 覆盖参数会被忽略（本机实测）。
    由后端转发就能自己声明响应头，PDF 直接内联打开；顺带不外泄 COS 地址、无需跨域。

    不支持 Range 断点续传（前端预览用不到），大文件按流式返回。
    """
    resource = KnowledgeBaseService.get_resource_or_404(
        db, user_id=current_user.id, resource_id=resource_id
    )
    if resource.knowledge_base_id != knowledge_base_id:
        raise_error(code=404, message="该资料不属于这个知识库")

    stream = KnowledgeBaseService.get_file_stream(resource)
    filename = resource.file_name or f"resource-{resource.id}"
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    content_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")
    disposition = "attachment" if download or suffix not in _INLINE_TYPES else "inline"
    # RFC 5987：中文文件名必须用 filename* 才不会乱码；同时给 ASCII 兜底名。
    encoded_name = quote(filename, safe="")
    content_disposition = (
        f"{disposition}; filename=\"resource-{resource.id}{suffix}\"; "
        f"filename*=UTF-8''{encoded_name}"
    )

    def iter_body():
        body = stream.get("Body")
        if body is None:
            return
        # COS SDK 的 Body 是文件对象，read 分块 → 不把整份文件读进内存
        while True:
            block = body.read(64 * 1024)
            if not block:
                break
            yield block

    headers = {
        "Content-Disposition": content_disposition,
        # 预览走同源临时地址，不缓存，避免删/换文件后浏览器还拿旧内容
        "Cache-Control": "no-store",
    }
    length = stream.get("Content-Length") or stream.get("content-length")
    if length and not download:
        headers["Content-Length"] = str(length)
    return StreamingResponse(iter_body(), media_type=content_type, headers=headers)


@router.delete(
    "/{knowledge_base_id}",
    response_model=StandardResponse,
    responses=common_error_responses,
    summary="删除知识库",
)
def delete_knowledge_base(
    knowledge_base_id: Annotated[int, Path(gt=0, description="知识库ID")],
    db: DBSession,
    current_user: CurrentUser,
) -> StandardResponse:
    """删除库：资料与练习解绑、掌握度记录删除、弱点改回通用；**不删文件与向量**。"""
    result = KnowledgeBaseService.delete(
        db, user_id=current_user.id, knowledge_base_id=knowledge_base_id
    )
    data = KnowledgeBaseDeleteResponse(**result)
    return StandardResponse(code=0, message="知识库删除成功", data=data)
