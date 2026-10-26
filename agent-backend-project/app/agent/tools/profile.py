"""画像原子能力工具：查掌握度、查薄弱点。

长期记忆**只通过这些工具暴露，不让模型直连数据库**（升级规划 §7.2 / §十一）。
读权限的工具与写权限的工具分开成文件，是为了让"权限边界"在代码结构上可见：
`profile.py` 全只读，`note.py` 才带写能力。
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.tools import tool
from sqlalchemy import select

from app.agent.tools.tool_context import InjectedToolContext
from app.db.models import KnowledgeBase, UserKnowledgeMastery
from app.services.mastery_service import MasteryService


@tool
def get_mastery(
    knowledge_base_id: int | None = None,
    *,
    ctx: InjectedToolContext,
) -> str:
    """查询用户的知识库掌握度（0–100）与练习场次。

    Args:
        knowledge_base_id: 知识库 ID；不传则返回该用户全部知识库的掌握度概览。
    """
    db = ctx["db"]
    user_id = int(ctx["user_id"])

    stmt = (
        select(UserKnowledgeMastery, KnowledgeBase.name)
        .join(KnowledgeBase, KnowledgeBase.id == UserKnowledgeMastery.knowledge_base_id)
        .where(UserKnowledgeMastery.user_id == user_id)
    )
    if knowledge_base_id is not None:
        stmt = stmt.where(UserKnowledgeMastery.knowledge_base_id == int(knowledge_base_id))

    rows = list(db.execute(stmt.order_by(UserKnowledgeMastery.mastery_score.desc())))
    items = [
        {
            "knowledge_base_id": int(mastery.knowledge_base_id),
            "knowledge_base_name": name,
            "mastery_score": round(float(mastery.mastery_score or 0.0), 2),
            "zone": _zone(float(mastery.mastery_score or 0.0)),
            "practice_count": int(mastery.practice_count or 0),
            "last_assessed_at": mastery.last_assessed_at,
        }
        for mastery, name in rows
    ]

    payload: dict[str, Any] = {"items": items, "count": len(items)}
    if not items:
        payload["note"] = "还没有任何已评分的练习，掌握度为空（不要臆造分数）"
    return json.dumps(payload, ensure_ascii=False)


@tool
def get_weak_points(
    knowledge_base_id: int | None = None,
    limit: int = 5,
    *,
    ctx: InjectedToolContext,
) -> str:
    """查询用户当前未改进、未归档的薄弱点，按出现次数降序。

    Args:
        knowledge_base_id: 知识库 ID；不传则返回全部（含未归属任何库的）薄弱点。
        limit: 最多返回条数（1–20，默认 5）。
    """
    items = MasteryService.get_active_weak_points(
        ctx["db"],
        user_id=int(ctx["user_id"]),
        knowledge_base_id=knowledge_base_id,
        limit=max(1, min(int(limit) if limit else 5, 20)),
    )
    payload = {
        "items": [
            {
                "point": item.get("point"),
                "times_seen": int(item.get("times_seen") or 0),
                "knowledge_base_id": item.get("knowledge_base_id"),
                "axis": item.get("axis") or "knowledge",
            }
            for item in items
        ],
        "count": len(items),
    }
    if not items:
        payload["note"] = "暂无活跃薄弱点（要么还没做过练习，要么都标记为已改进了）"
    return json.dumps(payload, ensure_ascii=False)


def _zone(score: float) -> str:
    """复用掌握度服务的分区口径，避免两处算法各写一份。"""
    return MasteryService.zone_label(score)
