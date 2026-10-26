"""知识库原子能力工具：检索资料原文。

对应升级规划 §7.2 的 `search_material(query, knowledge_base_id)`。
**这是工具，不是 worker**——它没有角色人格（不出题、不评分、不讲解），
只是"把资料取回来"这一个原子动作，所以按 §7.1 的划分标准归入 tool。
"""

from __future__ import annotations

import json

from langchain_core.tools import tool
from sqlalchemy import select

from app.agent.tools.tool_context import InjectedToolContext
from app.db.models import Resource
from app.rag.retriever import search_similar_chunks

# 单条切片返回给模型的字符上限，避免一份资料把上下文挤爆
MAX_SNIPPET_CHARS = 800


@tool
def search_material(
    query: str,
    top_k: int = 3,
    *,
    ctx: InjectedToolContext,
) -> str:
    """检索用户知识库里的资料原文，返回最相关的片段与来源文件名。

    用于回答"资料里是怎么说的"这类问题，也用于出题时按考点取原文兜底。
    query 用资料里可能出现的**具体名词**效果最好。

    Args:
        query: 检索用的关键词或问句。
        top_k: 返回片段数（1–10，默认 3）。
    """
    user_id = int(ctx["user_id"])
    db = ctx["db"]
    # 只要"有效 id"：`0` 是前端"明确不使用知识库"的哨兵（第 24 轮），
    # 若原样拿去过滤会变成 `knowledge_base_id = 0`——一条资料都搜不到，且用户看不出原因。
    raw_knowledge_base_id = ctx.get("knowledge_base_id")
    knowledge_base_id = (
        int(raw_knowledge_base_id)
        if raw_knowledge_base_id is not None and int(raw_knowledge_base_id) > 0
        else None
    )

    limit = max(1, min(int(top_k) if top_k else 3, 10))

    # 知识库范围收窄：先查出这个库里的资料 id，再交给检索层过滤。
    # 过滤条件用 `resource_id`（MySQL 权威值）而不是 Qdrant payload——存量点的
    # payload 里没有 resource_id（升级规划 §7 第 9 项），只靠 payload 会漏结果。
    resource_ids: list[int] | None = None
    if knowledge_base_id is not None:
        resource_ids = [
            int(rid)
            for rid in db.scalars(
                select(Resource.id).where(
                    Resource.user_id == user_id,
                    Resource.knowledge_base_id == int(knowledge_base_id),
                )
            )
        ]
        if not resource_ids:
            return json.dumps(
                {"hits": [], "count": 0, "note": "该知识库里没有已归档的资料"},
                ensure_ascii=False,
            )

    hits = search_similar_chunks(
        query=query,
        user_id=user_id,
        db=db,
        top_k=limit,
        resource_ids=resource_ids,
    )

    payload = {
        "hits": [
            {
                "file_name": hit.get("file_name", ""),
                "resource_id": hit.get("resource_id"),
                "chunk_index": hit.get("chunk_index"),
                "score": round(float(hit.get("score") or 0.0), 4),
                "text": (hit.get("text") or "")[:MAX_SNIPPET_CHARS],
            }
            for hit in hits
        ],
        "count": len(hits),
    }
    if not hits:
        payload["note"] = "没有检索到相关片段（资料未入库或该问题的表述与资料差异过大）"
    return json.dumps(payload, ensure_ascii=False)
