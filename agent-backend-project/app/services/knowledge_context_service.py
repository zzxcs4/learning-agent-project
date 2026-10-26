"""出题 / 评测的上下文组装：考纲（默认）＋ 原文（可选或评测必给）。

三种模式（用户可选，**默认 `outline`**）：

- `outline`：只给考纲（约 600 字符）。通用领域（Java/MySQL/算法）够用——事实本就在模型参数里，
  考纲只负责"考哪、先考什么、往哪追"。题更聚焦、input token 最省。
- `outline_with_source`：考纲 + 本次轮到的章节对应的**原文片段**。题贴资料、可追溯，
  私有材料（公司规范、自研框架）必须用这个。
- `source_only`：**只给原文**，不渲染考纲。用于"资料本身就是清单/速查表、提炼考纲没意义"的场景。

**原文的三级来源**（三种模式共用，逐级降级，每级失败都不抛错）：

1. **向量语义检索**（主力）：query = `章节名 + 考点名词` → DashScope `text_type=query` →
   Qdrant（`user_id` 过滤）→ `vector_id` 回查 `knowledge_chunks` 拿权威原文 →
   按**本库 `resource_ids`** 收窄。能吃到"模型概括词 / 中英不一致 / 考点被切散"。
2. **指针**：从 `source_ref.chunk_from` 起连续取片（零依赖、确定）。向量不可用或零命中时用它。
3. **整份资料**：按 `(resource_id, chunk_index)` 顺序拼该库原文（无章节时）。

评测**始终**给原文（`references`）：没有原文，评分就是模型自评，而掌握度 / 弱点 / 趋势全建立在
分数上——这一步不能省。

> 注：曾经还有一条"关键词 2-gram 命中"的定位/覆盖率链路，2026-10-17 按用户要求**整条删除**。
> 覆盖率不再由"给了哪些原文"推算——那个指标要等"出题时每题标注考点"之后才有意义。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.db.models import KnowledgeBase, KnowledgeChunk, Resource
from app.services.quiz_core_service import CHAR_BUDGET, QuizCoreService

# 出题上下文模式
MODE_OUTLINE = "outline"
MODE_OUTLINE_WITH_SOURCE = "outline_with_source"
MODE_SOURCE_ONLY = "source_only"
SUPPORTED_MODES = (MODE_OUTLINE, MODE_OUTLINE_WITH_SOURCE, MODE_SOURCE_ONLY)
DEFAULT_MODE = MODE_OUTLINE

# 原文预算：出题与评测各自独立封顶（考纲本身很小，不占这个预算）
SOURCE_CHAR_BUDGET = 8_000
# 每章最多取多少个切片（500 字/片 → 约 1500 字/章）
MAX_CHUNKS_PER_SECTION = 3
# 出题时最多给几章配原文（预算内、`asked_count` 最低的优先）
MAX_SOURCE_SECTIONS = 3

# 向量语义定位开关：关掉则直接走指针降级（离线排查/省钱时用）
VECTOR_LOCATE_ENABLED = True
# 每章向量检索取多少个候选片（再按预算/配额裁剪）
VECTOR_TOP_K = 6


class KnowledgeContextService:
    """把 `quiz_core`（+ 原文）组装成出题 / 评测的提示词上下文。"""

    @staticmethod
    def normalize_mode(mode: Any) -> str:
        """未知模式一律退回默认值，避免前端传错就把出题链路打挂。"""
        text = str(mode or "").strip().lower()
        return text if text in SUPPORTED_MODES else DEFAULT_MODE

    # ------------------------------------------------------------------ 出题侧
    @staticmethod
    def render_for_quiz(
        db: Session,
        *,
        knowledge_base_id: int | None,
        mode: Any = DEFAULT_MODE,
        char_budget: int = SOURCE_CHAR_BUDGET,
        prefer_reviewed: bool = False,
    ) -> dict[str, Any]:
        """组装出题上下文。

        返回 `{"context", "mode", "sections", "chars", "with_source", "reason", "source_detail"}`：
        `sections` 是本次"轮到"的章节名（评测要用同一批章节取原文）；`context` 为空表示
        本次没有考纲可用（没选库 / 库还没提炼），出题照常进行、只是不带知识库上下文。

        `prefer_reviewed=True`（发散度 ≤2 时）会**反着选章**：优先已经在练过的章，
        而不是永远优先没轮到的章——低发散度的语义就是"往练过的地方多打几遍"。
        """
        resolved_mode = KnowledgeContextService.normalize_mode(mode)
        if knowledge_base_id is None:
            return {
                "context": "",
                "mode": resolved_mode,
                "sections": [],
                "chars": 0,
                "with_source": False,
                "reason": "本次练习不属于任何知识库",
                "source_detail": [],
            }

        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        quiz_core = knowledge_base.quiz_core if knowledge_base else None
        has_outline = _has_outline(quiz_core)
        plan = _pick_sections(
            quiz_core, limit=MAX_SOURCE_SECTIONS, prefer_reviewed=prefer_reviewed
        )
        headings = [
            str(section.get("heading") or "").strip()
            for section in plan
            if section.get("heading")
        ]

        # ---- source_only：只给原文，不渲染考纲 ----
        if resolved_mode == MODE_SOURCE_ONLY:
            located = KnowledgeContextService.locate_source(
                db,
                sections=plan,
                char_budget=char_budget,
                knowledge_base_id=knowledge_base_id,
            )
            source = located["text"]
            return {
                "context": source,
                "mode": resolved_mode,
                "sections": headings,
                "chars": len(source),
                "with_source": bool(source),
                "reason": "只给原文（不看考纲）" if source else "该知识库还没有可用的原文",
                "source_detail": located["details"],
            }

        outline = (
            QuizCoreService.render_quiz_core(quiz_core, char_budget=CHAR_BUDGET)
            if has_outline
            else ""
        )

        # ---- outline：只给考纲；没有考纲就给空串（不塞"只有标题"的空块） ----
        if resolved_mode != MODE_OUTLINE_WITH_SOURCE:
            return {
                "context": outline,
                "mode": resolved_mode,
                "sections": headings,
                "chars": len(outline),
                "with_source": False,
                "reason": "默认只给考纲" if outline else "该知识库还没有考纲",
                "source_detail": [],
            }

        # ---- outline_with_source：考纲 + 原文；没有章节时原文退化为整份资料 ----
        located = KnowledgeContextService.locate_source(
            db,
            sections=plan,
            char_budget=char_budget,
            knowledge_base_id=knowledge_base_id,
        )
        source = located["text"]
        if outline and source:
            context, reason = f"{outline}\n\n## 本次覆盖章节的原文\n\n{source}", "考纲 + 本次章节原文"
        elif source:
            context, reason = f"## 参考原文\n\n{source}", "没有考纲，退化为原文"
        elif outline:
            context, reason = outline, "没取到原文，退化为只给考纲"
        else:
            context, reason = "", "既没有考纲也没有原文"
        return {
            "context": context,
            "mode": resolved_mode,
            "sections": headings,
            "chars": len(context),
            "with_source": bool(source),
            "reason": reason,
            "source_detail": located["details"],
        }

    # ------------------------------------------------------------------ 评测侧
    @staticmethod
    def render_references(
        db: Session,
        *,
        knowledge_base_id: int | None,
        sections: list[str] | None = None,
        char_budget: int = SOURCE_CHAR_BUDGET,
    ) -> str:
        """组装评测用的参考资料（原文）。

        优先用出题时"轮到"的那几章（同批原文，评分有依据）；没有章节信息时按 `asked_count`
        升序补几章；该库**没有章节**时退化为整份资料原文。取不到原文返回空串，
        调用方要在日志里标明"评分无资料依据"。
        """
        if knowledge_base_id is None:
            return ""
        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            return ""

        plan = _pick_sections(
            knowledge_base.quiz_core, limit=MAX_SOURCE_SECTIONS, names=sections
        )
        return KnowledgeContextService.locate_source(
            db, sections=plan, char_budget=char_budget, knowledge_base_id=knowledge_base_id
        )["text"]

    # ------------------------------------------------------------------ 原文回查
    @staticmethod
    def locate_source(
        db: Session,
        *,
        sections: list[dict[str, Any]],
        char_budget: int = SOURCE_CHAR_BUDGET,
        knowledge_base_id: int | None = None,
    ) -> dict[str, Any]:
        """按章节取原文，返回 `{"text": str, "details": [...]}`（details 供日志审计）。

        每章走 `_locate_section`（**向量 → 指针**）；一章都没定位到时，退化为整份资料原文。
        """
        blocks: list[str] = []
        details: list[dict[str, Any]] = []
        used = 0

        # 向量检索要按用户 + 本库资源收窄，先取一次归属信息
        user_id: int | None = None
        resource_ids: set[int] = set()
        if knowledge_base_id is not None:
            knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
            if knowledge_base is not None:
                user_id = knowledge_base.user_id
                resource_ids = set(
                    db.scalars(
                        select(Resource.id).where(
                            Resource.knowledge_base_id == knowledge_base_id
                        )
                    )
                )

        for section in sections or []:
            chunks, detail = _locate_section(
                db,
                section=section,
                max_chunks=MAX_CHUNKS_PER_SECTION,
                char_budget=max(0, char_budget - used),
                user_id=user_id,
                resource_ids=resource_ids,
            )
            if not chunks:
                continue
            body = "\n".join(
                str(item.get("text") or "").strip() for item in chunks if item.get("text")
            )
            if not body:
                continue
            block = f"### {section.get('heading') or '原文'}\n{body}"
            cost = len(block) + 2
            if used + cost > char_budget:
                continue
            blocks.append(block)
            used += cost
            details.append(detail)

        if blocks:
            return {"text": "\n\n".join(blocks), "details": details}

        # 没有章节可用 / 一章都没定位到 → 退化为整份资料原文
        if knowledge_base_id is None:
            return {"text": "", "details": details}
        return {
            "text": _render_fulltext(
                db, knowledge_base_id=knowledge_base_id, char_budget=char_budget
            ),
            "details": details + [{"heading": None, "by": "fulltext"}],
        }

    @staticmethod
    def render_source(
        db: Session,
        *,
        sections: list[dict[str, Any]],
        char_budget: int = SOURCE_CHAR_BUDGET,
        knowledge_base_id: int | None = None,
    ) -> str:
        """`locate_source` 的纯文本版（评测侧只用文本）。"""
        return KnowledgeContextService.locate_source(
            db,
            sections=sections,
            char_budget=char_budget,
            knowledge_base_id=knowledge_base_id,
        )["text"]


def _has_outline(quiz_core: Any) -> bool:
    """有没有可用的考纲内容（章节或元信息都算；只有 title 不算）。"""
    if not isinstance(quiz_core, dict):
        return False
    return bool(
        quiz_core.get("sections")
        or quiz_core.get("frequently_tested")
        or quiz_core.get("followup_paths")
    )


def _vector_locate_section(
    db: Session,
    *,
    section: dict[str, Any],
    user_id: int,
    resource_ids: set[int],
    max_chunks: int,
    char_budget: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按考点做**向量语义检索**定位一章的原文。

    query = 章节名 + 考点名词（`索引入门 聚簇索引与回表 最左前缀`），走 `search_similar_chunks`
    （user_id 过滤 → Qdrant → vector_id 回查 MySQL 原文），再按 `resource_id` 收窄到本知识库。

    任何异常（没配 DashScope / Qdrant 挂 / 集合不存在）都返回空，由调用方降级到指针。
    """
    heading = str(section.get("heading") or "")
    topics = [
        str(topic).strip() for topic in (section.get("topics") or []) if str(topic).strip()
    ]
    detail: dict[str, Any] = {"heading": heading, "by": "vector", "chunks": 0}
    if not topics:
        return [], detail

    query = " ".join([heading, *topics])[:200]
    try:
        from app.rag.retriever import search_similar_chunks

        hits = search_similar_chunks(query, user_id=user_id, db=db, top_k=VECTOR_TOP_K)
    except Exception as exc:  # noqa: BLE001 - 向量检索失败不阻断出题
        main_logger.warning(f"章节向量检索失败（将降级到指针）: {exc}")
        return [], detail
    if not hits:
        return [], detail

    if resource_ids:
        hits = [hit for hit in hits if hit.get("resource_id") in resource_ids]
    if not hits:
        return [], detail

    picked: list[dict[str, Any]] = []
    used = 0
    for hit in hits:  # Qdrant 已按 score 降序
        text = str(hit.get("text") or "").strip()
        if not text:
            continue
        cost = len(text) + 1
        if used + cost > char_budget:
            break
        picked.append({"text": text, "chunk_index": hit.get("chunk_index")})
        used += cost
        if len(picked) >= max_chunks:
            break

    detail.update(
        chunks=len(picked),
        top_score=round(float(hits[0].get("score") or 0.0), 3),
        query=query[:40],
    )
    return picked, detail


def _pointer_locate_section(
    db: Session,
    *,
    section: dict[str, Any],
    max_chunks: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """指针定位（降级层）：从 `source_ref.chunk_from` 起连续取片，零依赖、确定。"""
    source_ref = section.get("source_ref") or {}
    resource_id = source_ref.get("resource_id")
    chunk_from = source_ref.get("chunk_from")
    detail: dict[str, Any] = {
        "heading": str(section.get("heading") or ""),
        "by": "pointer",
        "chunks": 0,
    }
    if not isinstance(resource_id, int) or not isinstance(chunk_from, int):
        return [], detail

    rows = list(
        db.scalars(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.resource_id == resource_id,
                KnowledgeChunk.chunk_index >= chunk_from,
                KnowledgeChunk.chunk_index < chunk_from + max_chunks,
            )
            .order_by(KnowledgeChunk.chunk_index.asc())
        )
    )
    if not rows:
        return [], detail
    detail["chunks"] = len(rows)
    return [{"text": chunk.text, "chunk_index": chunk.chunk_index} for chunk in rows], detail


def _locate_section(
    db: Session,
    *,
    section: dict[str, Any],
    max_chunks: int,
    char_budget: int,
    user_id: int | None = None,
    resource_ids: set[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """定位一章的原文：**向量 → 指针**（每级失败都不抛错）。"""
    if VECTOR_LOCATE_ENABLED and user_id is not None and resource_ids is not None:
        picked, detail = _vector_locate_section(
            db,
            section=section,
            user_id=user_id,
            resource_ids=resource_ids,
            max_chunks=max_chunks,
            char_budget=char_budget,
        )
        if picked:
            return picked, detail
    return _pointer_locate_section(db, section=section, max_chunks=max_chunks)


def _render_fulltext(
    db: Session, *, knowledge_base_id: int, char_budget: int
) -> str:
    """无章节时的退化：按 `(resource_id, chunk_index)` 顺序拼该库资料原文，<= 预算。

    整片放不下就停（按片取舍，不切字符）——这是 TechSpar 的"全文直塞"那一段，
    但它用 `.slice()` 裸切，我们按切片取舍。
    """
    rows = db.scalars(
        select(KnowledgeChunk)
        .join(Resource, Resource.id == KnowledgeChunk.resource_id)
        .where(Resource.knowledge_base_id == knowledge_base_id)
        .order_by(KnowledgeChunk.resource_id.asc(), KnowledgeChunk.chunk_index.asc())
    )
    blocks: list[str] = []
    used = 0
    for chunk in rows:
        text = str(chunk.text or "").strip()
        if not text:
            continue
        cost = len(text) + 2
        if used + cost > char_budget:
            break
        blocks.append(text)
        used += cost
    return "\n\n".join(blocks)


def _pick_sections(
    quiz_core: Any,
    *,
    limit: int,
    names: list[str] | None = None,
    prefer_reviewed: bool = False,
) -> list[dict[str, Any]]:
    """挑出"本次轮到"的章节：指定了名字就按名字取，否则按 `asked_count` 排序。

    `asked_count` 的语义是「**这一章被选作出题上下文的次数**」，**不是**"考过几次"——
    题实际考了哪一章无从得知（模型可能考没给它原文的章），所以它只用于轮换，不能当覆盖率。

    `prefer_reviewed=True` 时**降序**（已练过的章优先）——低发散度想让用户在练过的地方多打几遍。
    """
    if not isinstance(quiz_core, dict):
        return []
    sections = [item for item in (quiz_core.get("sections") or []) if isinstance(item, dict)]
    if not sections:
        return []

    if names:
        wanted = {str(name).strip() for name in names if str(name).strip()}
        matched = [item for item in sections if str(item.get("heading") or "").strip() in wanted]
        if matched:
            return matched[:limit]

    sections = sorted(
        sections,
        key=lambda item: int(item.get("asked_count") or 0),
        reverse=prefer_reviewed,
    )
    return sections[:limit]


def bump_asked_count(
    quiz_core: Any, headings: list[str] | None, *, now_iso: str
) -> dict[str, Any] | None:
    """把本次**被选作上下文**的章节 `asked_count += 1` / `last_asked_at = now`，返回新的 `quiz_core`。

    再次强调：这是"给模型看了哪几章"的记账，不是"考了哪几章"的统计（见 `_pick_sections`）。
    返回 `None` 表示没有可写的内容（调用方跳过更新）。
    """
    if not isinstance(quiz_core, dict) or not headings:
        return None
    wanted = {str(name).strip() for name in headings if str(name).strip()}
    if not wanted:
        return None
    changed = False
    sections: list[dict[str, Any]] = []
    for section in quiz_core.get("sections") or []:
        if not isinstance(section, dict):
            continue
        item = dict(section)
        if str(item.get("heading") or "").strip() in wanted:
            item["asked_count"] = int(item.get("asked_count") or 0) + 1
            item["last_asked_at"] = now_iso
            changed = True
        sections.append(item)
    if not changed:
        return None
    updated = dict(quiz_core)
    updated["sections"] = sections
    return updated
