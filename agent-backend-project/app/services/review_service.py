"""SM-2 复习调度（升级规划 §八 第 3 期）：弱点的 `next_review` / `ease_factor` 滚动。

算法 = 标准 SuperMemo-2（1987）：

- 质量分 q ∈ 0–5；
- q < 3（失败）：`repetition` 归零、间隔回到 1 天（EF 仍按公式下调——连着失败 EF 会
  一路降到下限 1.3，之后间隔几乎不再增长，这正是"总记不住的东西该一直复习"的算法表达）；
- q ≥ 3（成功）：`EF' = EF + (0.1 − (5−q) × (0.2 + (5−q) × 0.08))`，下限 1.3；
  间隔阶梯：第 1 次成功 1 天 → 第 2 次 6 天 → 之后 `round(上次间隔 × EF')`；
- `next_review = 今天 + 间隔`。

## 质量分怎么从"练习"映射（本项目与背卡片场景的差异）

背卡片的 q 是"这次回忆起来没有"；本项目的最小事实是**弱点在本场练习里复现与否**
加整场分数——归并（`mastery_service.merge_weak_points`）已经算出了这两样：

- **复现**（本场又出现了这条弱点）：q = 2。不管整场多少分，它还在错，
  这就是"仍然弱"的直接证据，间隔必须归零重来；
- **缺席**：整场分数定档，与弱点毕业的分数门槛一致——≥ 80 → q=5、≥ 60 → q=4、
  否则 q=3。一次 40 分的练习里没犯，不足以证明"记得牢"（同 §5.1 毕业逻辑的口径）；
- **新建**：learn today, review tomorrow——首复排明天，EF 用默认值。

## 为什么挂在弱点归并里而不是独立入口

归并是唯一同时知道"复现/缺席"与整场分数的地方；另起一个判定入口只会造出
第二份真相。本模块只提供纯函数与查询，**不持有状态**。

## 到期视图的口径（`due_reviews`）

只收**活跃**弱点（未改进、未归档、非表现轴）。毕业条目退出归并后就不再有
"下一次练习"来滚动计划——不排除的话它们会把到期视图**永久占满**；等做了
"标记已复习"动作再考虑放开。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import UserProfile

DEFAULT_EASE_FACTOR = 2.5
# SuperMemo-2 原文的 EF 下限：再怎么失败也不低于 1.3（否则间隔几乎永不增长）
MIN_EASE_FACTOR = 1.3
# 质量分映射（SM-2 口径 0–5）
QUALITY_REPEAT = 2  # 复现：还在错
QUALITY_PASS = 3  # 缺席且整场 < 60：勉强算过
QUALITY_GOOD = 4  # 缺席且整场 60–79
QUALITY_GREAT = 5  # 缺席且整场 ≥ 80
# 缺席时按整场分数定档的门槛（GREAT 与弱点毕业的 WEAK_POINT_IMPROVED_SCORE 同值，口径一致）
GOOD_SCORE = 60.0
GREAT_SCORE = 80.0
# 成功复习的间隔阶梯（天）：1 → 6 → round(上次间隔 × EF)
FIRST_INTERVAL = 1
SECOND_INTERVAL = 6


class ReviewService:
    """SM-2 调度：质量映射、单条计划滚动、到期查询。"""

    # ------------------------------------------------------------------ 质量映射
    @staticmethod
    def quality_for(recurred: bool, practice_score: float) -> int:
        """从"本场复现与否 + 整场分数"映射 SM-2 质量分（依据见模块注释）。"""
        if recurred:
            return QUALITY_REPEAT
        if practice_score >= GREAT_SCORE:
            return QUALITY_GREAT
        if practice_score >= GOOD_SCORE:
            return QUALITY_GOOD
        return QUALITY_PASS

    # ------------------------------------------------------------------ 计划滚动
    @staticmethod
    def schedule(entry: dict[str, Any], *, quality: int, today: date) -> dict[str, Any]:
        """按本次质量滚动一条弱点的复习计划（原地修改并返回，纯函数便于单测）。

        条目里新增两个内部记账字段：`interval_days`（当前间隔）与 `repetition`
        （连续成功次数）——SM-2 需要它们，弱点的 JSON 结构里原先只预留了
        `next_review` / `ease_factor` 两个"对外可见"字段。
        """
        ease = float(entry.get("ease_factor") or DEFAULT_EASE_FACTOR)
        repetition = int(entry.get("repetition") or 0)
        interval = int(entry.get("interval_days") or 0)

        if quality < 3:
            # 失败：间隔与连击归零，但 EF 仍按公式下调（q 越低降得越多），同样夹下限
            ease += 0.1 - (5 - quality) * (0.2 + (5 - quality) * 0.08)
            ease = max(MIN_EASE_FACTOR, ease)
            repetition = 0
            interval = FIRST_INTERVAL
        else:
            ease += 0.1 - (5 - quality) * (0.2 + (5 - quality) * 0.08)
            ease = max(MIN_EASE_FACTOR, ease)
            repetition += 1
            if repetition == 1:
                interval = FIRST_INTERVAL
            elif repetition == 2:
                interval = SECOND_INTERVAL
            else:
                # 防御 interval 异常（老数据/手工改库）导致的 0 间隔
                interval = max(FIRST_INTERVAL, round(interval * ease))

        entry["ease_factor"] = round(ease, 2)
        entry["repetition"] = repetition
        entry["interval_days"] = interval
        entry["next_review"] = (today + timedelta(days=interval)).isoformat()
        return entry

    @staticmethod
    def schedule_new(entry: dict[str, Any], *, today: date) -> dict[str, Any]:
        """新建弱点的首排：明天复习一次，EF 保持默认（还没有证据调整它）。"""
        entry["ease_factor"] = round(float(entry.get("ease_factor") or DEFAULT_EASE_FACTOR), 2)
        entry["repetition"] = 0
        entry["interval_days"] = FIRST_INTERVAL
        entry["next_review"] = (today + timedelta(days=FIRST_INTERVAL)).isoformat()
        return entry

    # ------------------------------------------------------------------ 到期查询
    @staticmethod
    def is_due(entry: dict[str, Any], *, today: date) -> bool:
        """是否今天该复习：活跃 + 已排程 + 应复习日期 <= 今天。"""
        if entry.get("improved") or entry.get("archived"):
            return False
        if (entry.get("axis") or "knowledge") == "performance":
            return False
        raw = str(entry.get("next_review") or "")
        if not raw:
            return False
        try:
            due = date.fromisoformat(raw)
        except ValueError:
            return False
        return due <= today

    @staticmethod
    def due_reviews(
        db: Session, *, user_id: int, today: date | None = None
    ) -> dict[str, Any]:
        """今日到期复习：活跃弱点里 `next_review <= 今天` 的条目。

        排序：先到期的在前（拖得越久越该先补），同日的按出现次数降序（错得最多的
        更该先看）。`today` 参数供验收脚本固定"今天"，生产路径用系统日期。
        """
        today = today or date.today()
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        items = [
            dict(item)
            for item in ((profile.weak_points or []) if profile else [])
            if isinstance(item, dict)
        ]
        due = [item for item in items if ReviewService.is_due(item, today=today)]
        due.sort(
            key=lambda item: (
                str(item.get("next_review") or ""),
                -int(item.get("times_seen") or 0),
            )
        )
        return {"total": len(due), "today": today.isoformat(), "items": due}
