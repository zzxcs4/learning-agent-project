"""知识库级掌握度服务：难度加权得分 + 覆盖率 + EMA 滚动 + 弱点相似度归并（阈值 0.90）。

第 1 期第二步。`attempts` 是原子事实，本服务把一次练习的 attempt 行聚合成两样东西：

- `user_knowledge_mastery`：按 `(user_id, knowledge_base_id)` 的掌握度分数与场次（EMA 滚动）；
- `user_profiles.weak_points`：富结构弱点列表；**新弱点与已有弱点判定为同一条时 `times_seen += 1`**，
  判定阶梯见文末常量处的标定记录，否则新建。

两层分工（见 `升级规划.md` §5.1）：
- 掌握度的键是 `knowledge_base_id`——稳定不漂移，能做 EMA 序列、分区与趋势；
- 弱点用自由文本 + 归并——可计数、可复习调度，**不做掌握度的键**。
- `axis = performance` 的表现类问题**不进知识弱点、不参与掌握度**。

`improved` / `archived` 两个标记的**写入**逻辑属于后面的「覆盖面控制 / 弱点毕业」切片；本服务只提供
读取过滤（`get_active_weak_points`），弱点结构里已预留这两个字段。
"""

from __future__ import annotations

import math
import re
import time
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.models import Attempt, Question, UserKnowledgeMastery, UserProfile
from app.services.review_service import DEFAULT_EASE_FACTOR, ReviewService

# 弱点归并阈值。**不是**文档里的 0.75——按升级规划 §十 风险 7 的要求，用真实 DashScope
# embedding（text-embedding-v4 / 1024 维）对 11 条真实弱点做过两两标定：
#   应合并：0.998（多了个“的”）/ 0.963（长短句）/ 0.924（同义改写）/ 0.836（“索引失效”⊂“MySQL 索引失效场景”）
#   不该合并：0.805（GIL 并行执行 vs GIL 的作用范围）/ 0.785 / 0.782 / 0.396
# 0.75 正好落在“不该合并”的区间里 → 会把两个不同弱点错并成一条（实测发生过）。
# 可分区间只有 (0.805, 0.836]，太窄，所以用三条阶梯规则把区间摊开：
#   ① 归一化文本相同（去空白/标点/大小写）→ 合并，不花 embedding
#   ② 一条是另一条的子串（≥ WEAK_POINT_SUBSTRING_MIN 字）→ 合并，不花 embedding
#   ③ 余弦 ≥ 0.90 → 合并
# 偏严格是有意的：错合并会**丢失**一条弱点，碎成两条只是显示重复（掌握度的键是知识库，不受影响）。
WEAK_POINT_SIMILARITY = 0.90
# 子串包含判定的最短长度（归一化后），避免“GIL”这种极短词吃掉整条弱点
WEAK_POINT_SUBSTRING_MIN = 4
# DEFAULT_EASE_FACTOR 已随 SM-2 调度（第 3 期）移入 app/services/review_service.py，经上方 import 使用
# 分区阈值（前端展示）：< 40 补课区 / 40–69 过渡区 / ≥ 70 优势区
ZONE_REMEDIAL = 40.0
ZONE_TRANSITION = 70.0
# 出题模型没标难度时按 1.0 计（加权退化为普通加权平均）
DEFAULT_DIFFICULTY = 1.0
MIN_DIFFICULTY = 0.5
MAX_DIFFICULTY = 5.0
# 每条弱点最多保留多少条历史，避免 JSON 无限膨胀
MAX_WEAK_POINT_HISTORY = 20
# 弱点毕业：连续这么多场没再出现、且本轮练习分 ≥ IMPROVED_SCORE，才判"已改进"。
# 为什么不只看"没再出现"：一次 40 分的练习里没提到某弱点，不足以证明它好了。
WEAK_POINT_IMPROVED_STREAK = 2
WEAK_POINT_IMPROVED_SCORE = 80.0
# 连续这么多场没再出现 → 归档（不再进出题提示词；保留记录供画像页回溯）
WEAK_POINT_ARCHIVE_STREAK = 5
# 进程内向量缓存：(user_id, 归一化弱点文本) -> 向量
_EMBED_CACHE: dict[tuple[int, str], list[float]] = {}
_EMBED_CACHE_MAX = 500


class MasteryService:
    """一次练习结束后的掌握度更新与弱点沉淀。"""

    # ------------------------------------------------------------------ 对外主入口
    @staticmethod
    def update_after_practice(
        db: Session,
        *,
        user_id: int,
        practice_set_id: int,
        knowledge_base_id: int | None,
        weaknesses: list[str],
    ) -> dict[str, Any]:
        """一次练习结束后调用：更新掌握度 + 归并弱点，返回摘要（供日志与后续画像页使用）。

        只做**一次 commit**：掌握度与弱点同属一次练习的产物，不能一个成功一个失败。
        """
        questions = MasteryService._load_questions(db, practice_set_id)
        attempts = MasteryService._load_attempts(db, practice_set_id)
        practice_score, coverage = MasteryService.compute_practice_score(questions, attempts)

        summary: dict[str, Any] = {
            "practice_score": practice_score,
            "coverage": coverage,
            "question_count": len(questions),
        }

        # 一道题都没评分 = 没有可用的样本，写进去会把掌握度污染成 0 分
        if coverage <= 0:
            summary["mastery"] = "skipped: 本次没有任何评分记录"
        elif knowledge_base_id is None:
            summary["mastery"] = "skipped: 本次练习不属于任何知识库（掌握度的键是 knowledge_base_id）"
        else:
            summary["mastery"] = MasteryService._update_mastery(
                db,
                user_id=user_id,
                knowledge_base_id=int(knowledge_base_id),
                practice_score=practice_score,
                coverage=coverage,
            )

        summary["weak_points"] = MasteryService.merge_weak_points(
            db,
            user_id=user_id,
            knowledge_base_id=knowledge_base_id,
            points=weaknesses,
            practice_set_id=practice_set_id,
            practice_score=practice_score,
        )
        db.commit()
        return summary

    # ------------------------------------------------------------------ 打分
    @staticmethod
    def compute_practice_score(
        questions: list[Question], attempts: list[Attempt]
    ) -> tuple[float, float]:
        """难度加权得分（0–100）与覆盖率。

        公式：`100 × Σ(score_i × d_i) / Σ(points_i × d_i)`

        为什么不是升级规划 §六 的字面写法 `Σ(score_i × d_i) / Σ(d_i) × 10`：那是 TechSpar
        「单题 10 分制」的特例；本项目题目 `points` 合计 100，直接乘 10 会算出 670 分。
        按 `points` 归一化后，无论 points 合计是 100 还是 60，结果都稳定落在 0–100。

        覆盖率 = 已评分题数 / 总题数，用于给不完整的练习打折（见 EMA 权重）。
        """
        attempt_by_question = {attempt.question_id: attempt for attempt in attempts}
        weighted_got = 0.0
        weighted_full = 0.0
        scored = 0

        for question in questions:
            difficulty = MasteryService._difficulty(question)
            weighted_full += float(question.points or 0) * difficulty
            attempt = attempt_by_question.get(question.id)
            if attempt is not None and attempt.score is not None:
                weighted_got += float(attempt.score) * difficulty
                scored += 1

        total = len(questions)
        coverage = round(scored / total, 2) if total else 0.0

        if weighted_full > 0:
            return round(100.0 * weighted_got / weighted_full, 1), coverage

        # 兜底：模型没给 points（全为 0）时按“判对比例”算，避免掌握度被记成 0 分
        judged = [attempt for attempt in attempts if attempt.is_correct is not None]
        if not judged:
            return 0.0, coverage
        correct = sum(1 for attempt in judged if attempt.is_correct)
        return round(100.0 * correct / len(judged), 1), coverage

    @staticmethod
    def zone_label(score: float) -> str:
        """分区：< 40 补课区 / 40–69 过渡区 / ≥ 70 优势区。"""
        if score < ZONE_REMEDIAL:
            return "补课区"
        if score < ZONE_TRANSITION:
            return "过渡区"
        return "优势区"

    # ------------------------------------------------------------------ EMA
    @staticmethod
    def _update_mastery(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int,
        practice_score: float,
        coverage: float,
    ) -> dict[str, Any]:
        """按 (user_id, knowledge_base_id) 做一次 EMA 滚动。

        首场**直接以本次得分初始化**：没有历史可比时再乘一次 coverage 等于把分数惩罚两次
        （升级规划 §六 的副作用提醒，取“直接初始化”这一支）。
        之后 `w = max(0.15, 1/(practice_count+1)) × coverage`：练得越多单次越稳，
        但任何一次都至少能撼动 15%，不会练到后面分数僵死。
        """
        row = db.scalar(
            select(UserKnowledgeMastery).where(
                UserKnowledgeMastery.user_id == user_id,
                UserKnowledgeMastery.knowledge_base_id == knowledge_base_id,
            )
        )
        created = row is None
        if created:
            row = UserKnowledgeMastery(
                user_id=user_id,
                knowledge_base_id=knowledge_base_id,
                mastery_score=0.0,
                practice_count=0,
            )
            db.add(row)

        before = float(row.mastery_score or 0.0)
        count = int(row.practice_count or 0)

        if created:
            after = practice_score
            weight = 1.0
        else:
            weight = max(0.15, 1.0 / (count + 1)) * coverage
            after = round(before * (1 - weight) + practice_score * weight, 1)

        row.mastery_score = after
        row.practice_count = count + 1
        row.last_assessed_at = int(time.time())
        row.remark = MasteryService.zone_label(after)

        return {
            "before": before,
            "after": after,
            "weight": round(weight, 3),
            "zone": row.remark,
            "practice_count": row.practice_count,
            "initialized": created,
        }

    # ------------------------------------------------------------------ 弱点归并
    @staticmethod
    def merge_weak_points(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int | None,
        points: list[str],
        practice_set_id: int,
        practice_score: float,
    ) -> dict[str, Any]:
        """把本次的弱点并进 `user_profiles.weak_points`：命中判定阶梯见 `_best_match`。

        同时做**弱点毕业与老化**（本场之前就存在的活跃弱点）：
        - 本场**没有再出现** → `absent_streak += 1`；出现了 → 归零；
        - `absent_streak ≥ 2` 且本轮练习分 ≥ 80 → `improved = True`（"连续两场没犯、成绩也上来了"）；
        - `absent_streak ≥ 5` → `archived = True`（长期没犯，不再进出题提示词）。
        > 与 `升级规划` §5.1 的偏差：文档写的是"被强项匹配到即毕业"，但**我们没有强项列表**，
        > 所以用"连续未复现 + 本轮分数"代替。副作用：`improved` 的弱点不再参与匹配，
        > 复发时会作为**新弱点**重新记录（视为回归，从头累计）。

        同时按 SM-2 滚动每条弱点的复习计划（`next_review` / `ease_factor`，算法与
        质量映射见 `review_service`）：复现 → q=2；缺席 → 整场分数定档；新建 → 明天首复。

        返回 `{"added", "merged", "improved", "archived", "total"}`。embedding 不可用时
        **降级为归一化文本去重**（宁可少合并，也不要因为向量服务抖动而把这次弱点直接丢掉）。
        """
        texts = [str(point).strip() for point in (points or []) if str(point).strip()]

        profile = MasteryService._get_or_create_profile(db, user_id=user_id)
        existing = [dict(item) for item in (profile.weak_points or []) if isinstance(item, dict)]
        # 本场之前就存在的条目才会被"老化"（本场新增的不算"没再出现"）
        pre_count = len(existing)
        # 只与“未改进、未归档、非表现轴”的弱点比对：表现轴是表达习惯，不该混进知识弱点
        candidates = [item for item in existing if MasteryService._is_merge_candidate(item)]

        vector_map: dict[str, list[float]] = {}
        if texts and candidates:
            vector_map = MasteryService._embed_cached(
                user_id, [str(item.get("point") or "") for item in candidates] + texts
            )

        today_date = date.today()
        today = today_date.isoformat()
        added = merged = 0
        matched_ids: set[int] = set()
        for text in texts:
            matched = MasteryService._best_match(text, candidates, vector_map)
            if matched is not None:
                target = matched
                target["times_seen"] = int(target.get("times_seen") or 0) + 1
                target["last_seen"] = today
                target["last_checked_at"] = today
                target["absent_streak"] = 0
                if not target.get("knowledge_base_id"):
                    target["knowledge_base_id"] = knowledge_base_id
                target["history"] = MasteryService._append_history(
                    target.get("history"), today, practice_set_id, practice_score
                )
                # SM-2 调度（第 3 期）：复现 = 这条弱点还在错，质量与整场分数无关
                ReviewService.schedule(
                    target,
                    quality=ReviewService.quality_for(recurred=True, practice_score=practice_score),
                    today=today_date,
                )
                matched_ids.add(id(target))
                merged += 1
                continue

            entry = {
                "point": text,
                "knowledge_base_id": knowledge_base_id,
                "times_seen": 1,
                "first_seen": today,
                "last_seen": today,
                "improved": False,
                "archived": False,
                "absent_streak": 0,
                "last_checked_at": today,
                "next_review": None,
                "ease_factor": DEFAULT_EASE_FACTOR,
                "interval_days": 0,
                "repetition": 0,
                "axis": "knowledge",
                "history": [{"date": today, "practice_set_id": practice_set_id, "practice_score": practice_score}],
            }
            # SM-2 调度：新弱点首复排明天（learn today, review tomorrow）
            ReviewService.schedule_new(entry, today=today_date)
            existing.append(entry)
            # 同一场练习内的后续弱点也要能互相去重（但没有向量，只按归一化文本比）
            candidates.append(entry)
            added += 1

        # ★ 毕业与老化：本场之前就存在、但本场没有再出现的活跃弱点
        improved = archived = 0
        for item in existing[:pre_count]:
            if not MasteryService._is_merge_candidate(item):
                continue
            if id(item) in matched_ids:
                continue
            item["absent_streak"] = int(item.get("absent_streak") or 0) + 1
            item["last_checked_at"] = today
            # SM-2 调度：缺席 = 这次没犯，质量由整场分数定档（≥80 → 5 / ≥60 → 4 / 否则 3）
            ReviewService.schedule(
                item,
                quality=ReviewService.quality_for(recurred=False, practice_score=practice_score),
                today=today_date,
            )
            streak = int(item["absent_streak"])
            if not item.get("improved") and streak >= WEAK_POINT_IMPROVED_STREAK and practice_score >= WEAK_POINT_IMPROVED_SCORE:
                item["improved"] = True
                item["improved_at"] = today
                improved += 1
            if not item.get("archived") and streak >= WEAK_POINT_ARCHIVE_STREAK:
                item["archived"] = True
                item["archived_at"] = today
                archived += 1

        # 必须赋新对象，否则 SQLAlchemy 检测不到 JSON 列的原地修改
        profile.weak_points = existing
        return {
            "added": added,
            "merged": merged,
            "improved": improved,
            "archived": archived,
            "total": len(existing),
        }

    @staticmethod
    def get_active_weak_points(
        db: Session,
        *,
        user_id: int,
        knowledge_base_id: int | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """取出题 / 讲解提示词要注入的弱点：未改进、未归档、非表现轴，按 `times_seen` 降序。

        传给 `knowledge_base_id` 时还会过滤成“本知识库的弱点 + 未归属任何库的弱点”
        （未选库时攒下的弱点仍然通用，不该丢）。
        """
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        if profile is None:
            return []
        items = [item for item in (profile.weak_points or []) if isinstance(item, dict)]
        items = [
            item
            for item in items
            if not item.get("improved")
            and not item.get("archived")
            and (item.get("axis") or "knowledge") != "performance"
        ]
        if knowledge_base_id is not None:
            items = [
                item
                for item in items
                if item.get("knowledge_base_id") in (None, knowledge_base_id)
            ]
        items.sort(key=lambda item: int(item.get("times_seen") or 0), reverse=True)
        return items[:limit]

    # ------------------------------------------------------------------ 内部工具
    @staticmethod
    def _load_questions(db: Session, practice_set_id: int) -> list[Question]:
        return list(
            db.scalars(
                select(Question)
                .where(Question.practice_set_id == practice_set_id)
                .order_by(Question.id.asc())
            )
        )

    @staticmethod
    def _load_attempts(db: Session, practice_set_id: int) -> list[Attempt]:
        question_ids = select(Question.id).where(Question.practice_set_id == practice_set_id)
        return list(db.scalars(select(Attempt).where(Attempt.question_id.in_(question_ids))))

    @staticmethod
    def _get_or_create_profile(db: Session, *, user_id: int) -> UserProfile:
        """取用户画像行；没有就建一行空画像（不 commit，跟随调用方的事务）。"""
        profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
        if profile is None:
            # 旧字段 weak_topics 自第 1 期起停用，写入方只认 weak_points
            profile = UserProfile(user_id=user_id, target_skills=[], weak_points=[])
            db.add(profile)
            db.flush()
        return profile

    @staticmethod
    def _difficulty(question: Question) -> float:
        """难度系数：出题模型没标时按 1.0，并夹在 [0.5, 5] 防止模型给出离谱值把加权带偏。"""
        raw = question.difficulty
        if raw is None:
            return DEFAULT_DIFFICULTY
        return min(max(float(raw), MIN_DIFFICULTY), MAX_DIFFICULTY)

    @staticmethod
    def _is_merge_candidate(item: dict[str, Any]) -> bool:
        if item.get("improved") or item.get("archived"):
            return False
        return (item.get("axis") or "knowledge") != "performance"

    @staticmethod
    def _append_history(
        history: Any, today: str, practice_set_id: int, practice_score: float
    ) -> list[dict[str, Any]]:
        items = list(history or []) if isinstance(history, list) else []
        items.append(
            {
                "date": today,
                "practice_set_id": practice_set_id,
                "practice_score": practice_score,
            }
        )
        return items[-MAX_WEAK_POINT_HISTORY:]

    @staticmethod
    def _best_match(
        text: str, candidates: list[dict[str, Any]], vector_map: dict[str, list[float]]
    ) -> dict[str, Any] | None:
        """在候选弱点里找最相似的一条（三条阶梯规则，见文件头注释）。

        ① 归一化文本相同 → 直接命中；
        ② 一条包含另一条（≥ 4 字）→ 命中；
        ③ 余弦 ≥ WEAK_POINT_SIMILARITY → 命中。
        ①② 不依赖 embedding，所以向量服务不可用时仍能去重掉“显然同一条”的弱点。
        """
        key = _normalize(text)
        if not key:
            return None
        vector = vector_map.get(key)
        best_item: dict[str, Any] | None = None
        best_score = 0.0
        longest_containment = 0

        for item in candidates:
            other_key = _normalize(str(item.get("point") or ""))
            if not other_key:
                continue
            # ① 归一化后完全相同
            if other_key == key:
                return item
            # ② 子串包含：取最长的那条候选，避免短词抢先命中
            shorter, longer = sorted((key, other_key), key=len)
            if (
                len(shorter) >= WEAK_POINT_SUBSTRING_MIN
                and shorter in longer
                and len(shorter) > longest_containment
            ):
                longest_containment = len(shorter)
                best_item = item
                best_score = 1.0
                continue
            # ③ 余弦
            if vector is None:
                continue
            other_vector = vector_map.get(other_key)
            if other_vector is None:
                continue
            similarity = _cosine(vector, other_vector)
            if similarity > best_score:
                best_score = similarity
                best_item = item

        if best_item is not None and best_score >= WEAK_POINT_SIMILARITY:
            return best_item
        return None

    @staticmethod
    def _embed_cached(user_id: int, texts: list[str]) -> dict[str, list[float]]:
        """按 (user_id, 归一化文本) 取向量；未命中的批量调 DashScope。

        失败时返回已命中的部分（可能是空表）——调用方会退化为归一化文本去重，不会抛异常。
        """
        result: dict[str, list[float]] = {}
        missing: dict[str, str] = {}
        for text in texts:
            key = _normalize(text)
            if not key or key in result:
                continue
            cached = _EMBED_CACHE.get((user_id, key))
            if cached is not None:
                result[key] = cached
            else:
                missing.setdefault(key, text)

        if not missing:
            return result

        try:
            # 懒加载：不沉淀弱点时不必付 RAG 模块的导入成本
            from app.rag.core import embed_texts

            vectors = embed_texts(list(missing.values()))
        except Exception as exc:  # noqa: BLE001 - 向量服务不可用时降级为文本去重
            main_logger.warning(f"弱点向量化失败，本次降级为文本去重: {exc}")
            return result

        for key, vector in zip(missing.keys(), vectors):
            result[key] = vector
            if len(_EMBED_CACHE) < _EMBED_CACHE_MAX:
                _EMBED_CACHE[(user_id, key)] = vector
        return result


def _normalize(text: str) -> str:
    """归一化弱点文本：去掉所有空白与标点、统一小写，用于“显然是同一条”的精确命中。"""
    return re.sub(r"[\s\W_]+", "", str(text or "")).lower()


def _cosine(left: list[float], right: list[float]) -> float:
    """余弦相似度；维度不一致或零向量时返回 0。"""
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)
