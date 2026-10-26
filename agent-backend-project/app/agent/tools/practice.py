"""练习原子能力工具：查练习历史。

只读工具。出题的写入口是 `delegate_quiz`（worker），不在这里——
"开始一次练习"是角色行为（出题），不是原子能力（升级规划 §7.1 的划分标准）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain_core.tools import tool

from app.agent.tools.tool_context import InjectedToolContext
from app.services.practice_service import PracticeService

HISTORY_LIMIT = 10


@tool
def list_practice_history(
    knowledge_base_id: int | None = None,
    limit: int = 5,
    *,
    ctx: InjectedToolContext,
) -> str:
    """查询用户的练习历史（时间倒序），含状态与得分。

    Args:
        knowledge_base_id: 知识库 ID；不传则返回该用户全部练习。
        limit: 最多返回几场（1–20，默认 5）。
    """
    size = max(1, min(int(limit) if limit else 5, 20))
    sets, total = PracticeService.list_practice_history(
        ctx["db"],
        user_id=int(ctx["user_id"]),
        knowledge_base_id=knowledge_base_id,
        page=1,
        page_size=size,
    )

    items = [
        {
            "practice_set_id": int(item.id),
            "title": item.title,
            "status": item.status,
            "total_score": None if item.total_score is None else round(float(item.total_score), 2),
            "question_count": int(item.question_count or 0),
            "knowledge_base_id": None if item.knowledge_base_id is None else int(item.knowledge_base_id),
            # 归档/回看总评都要定位到工作流 run；老数据（无 run_id）为空串，
            # 这时归档工具会退化为"该会话最近完成的一场"
            "run_id": item.run_id or "",
            "created_at": _format_ts(item.created_at),
        }
        for item in sets
    ]

    scored = [
        float(item.total_score)
        for item in sets
        if item.status == "completed" and item.total_score is not None
    ]
    payload: dict[str, Any] = {
        "items": items,
        "returned": len(items),
        "total": int(total),
        "average_recent_score": round(sum(scored) / len(scored), 2) if scored else None,
    }
    if not items:
        payload["note"] = "还没有练习记录"
    return json.dumps(payload, ensure_ascii=False)


def _format_ts(value: Any) -> str | None:
    """把 Unix 秒时间戳转成本地可读时间；异常值原样返回 None。"""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return None
