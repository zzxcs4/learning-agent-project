"""学习画像读接口：统计 / 掌握度 / 弱点清单。

只读服务：写入方在 `practice_service`（练习落库）与 `mastery_service`（掌握度与弱点沉淀）。
**不做"知识覆盖率"**（2026-10-17 决定）：它只能靠"给模型看了哪几章原文"推算，是输入侧的假指标；
画像页用"练习次数 + 掌握度 + 弱点清单"三样真实数据回答"练了多少、练得怎样、弱在哪"。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import KnowledgeBase, PracticeSet, UserKnowledgeMastery, UserProfile
from app.services.mastery_service import MasteryService
from app.services.practice_service import STATUS_COMPLETED

# 趋势图取最近多少场
RECENT_SCORE_LIMIT = 10


class ProfileService:
    """画像页所需的三个只读查询。"""

    @staticmethod
    def stats(db: Session, *, user_id: int, recent: int = RECENT_SCORE_LIMIT) -> dict[str, Any]:
        """总练习次数 / 已完成场次 / 平均分 / 最近一次得分 / 最近 N 场分数（时间升序）。

        "最近一次得分"取**最近一场已完成**的练习：最近一场可能还没作答（`total_score` 为空），
        拿它当"最近一次"会让画像页显示空白。
        """
        base = [PracticeSet.user_id == user_id]
        completed = base + [
            PracticeSet.status == STATUS_COMPLETED,
            PracticeSet.total_score.is_not(None),
        ]

        total = db.scalar(select(func.count(PracticeSet.id)).where(*base)) or 0
        completed_count = db.scalar(select(func.count(PracticeSet.id)).where(*completed)) or 0
        average = db.scalar(select(func.avg(PracticeSet.total_score)).where(*completed))

        latest = db.scalar(
            select(PracticeSet)
            .where(*completed)
            .order_by(PracticeSet.created_at.desc(), PracticeSet.id.desc())
            .limit(1)
        )
        recent_rows = list(
            db.scalars(
                select(PracticeSet)
                .where(*completed)
                .order_by(PracticeSet.created_at.desc(), PracticeSet.id.desc())
                .limit(recent)
            )
        )
        # 查询是倒序（最新在前），画趋势要正序
        recent_scores = [int(row.total_score or 0) for row in reversed(recent_rows)]

        return {
            "practice_count": int(total),
            "completed_count": int(completed_count),
            "average_score": round(float(average), 1) if average is not None else 0.0,
            "latest_score": int(latest.total_score) if latest is not None else None,
            "latest_practice_at": int(latest.created_at) if latest is not None else None,
            "recent_scores": recent_scores,
        }

    @staticmethod
    def list_mastery(db: Session, *, user_id: int) -> list[dict[str, Any]]:
        """按知识库的掌握度（降序）+ 分区；知识库被删时 name 为空但仍返回该行。"""
        rows = db.execute(
            select(UserKnowledgeMastery, KnowledgeBase.name)
            .join(
                KnowledgeBase,
                KnowledgeBase.id == UserKnowledgeMastery.knowledge_base_id,
                isouter=True,
            )
            .where(UserKnowledgeMastery.user_id == user_id)
            .order_by(UserKnowledgeMastery.mastery_score.desc())
        ).all()

        items: list[dict[str, Any]] = []
        for mastery, knowledge_base_name in rows:
            score = round(float(mastery.mastery_score or 0.0), 1)
            items.append(
                {
                    "knowledge_base_id": int(mastery.knowledge_base_id),
                    "knowledge_base_name": knowledge_base_name,
                    "mastery_score": score,
                    "zone": MasteryService.zone_label(score),
                    "practice_count": int(mastery.practice_count or 0),
                    "last_assessed_at": (
                        int(mastery.last_assessed_at)
                        if mastery.last_assessed_at is not None
                        else None
                    ),
                }
            )
        return items

    @staticmethod
    def list_weak_points(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int | None = None,
    ) -> dict[str, Any]:
        """弱点清单：活跃在前（按出现次数降序），已改进/已归档排后面。顺带给出三类计数。

        **只列知识轴**（`axis = knowledge`）：表现轴（表达习惯类）不是知识弱点，
        也不进出题提示词，混进来会让"弱点清单"变成两种东西的杂烩。
        """
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        items = [
            dict(item)
            for item in ((profile.weak_points or []) if profile else [])
            if isinstance(item, dict)
            and (item.get("axis") or "knowledge") != "performance"
        ]

        if knowledge_base_id is not None:
            # 与出题注入保持同一口径：本库的 + 未归属任何库的通用弱点
            items = [
                item
                for item in items
                if item.get("knowledge_base_id") in (None, knowledge_base_id)
            ]

        active = [item for item in items if not item.get("improved") and not item.get("archived")]
        improved = [item for item in items if item.get("improved")]
        archived = [item for item in items if item.get("archived")]

        active.sort(key=lambda item: int(item.get("times_seen") or 0), reverse=True)
        improved.sort(key=lambda item: int(item.get("times_seen") or 0), reverse=True)
        archived.sort(key=lambda item: int(item.get("times_seen") or 0), reverse=True)

        return {
            "total": len(items),
            "active": len(active),
            "improved": len(improved),
            "archived": len(archived),
            "items": active + improved + archived,
        }
