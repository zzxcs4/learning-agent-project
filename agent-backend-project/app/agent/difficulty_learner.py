"""难点讲解 Agent：把"用户的难点"变成一张可出图的结构化知识卡。

它做三件事，顺序固定：

1. **取素材**（`build_material`）：用户这句话 + 本会话最近一次完成练习的错题与点评 +
   活跃薄弱点 + 知识库原文/考纲。任何一路取不到都不报错，降级成"素材少一点"。
2. **结构化**（`prepare_card`）：调一次模型，把素材压成知识卡 JSON。
3. **拼图像 Prompt**（`build_image_prompt`）：卡 → 给 Seedream 的提示词。

为什么要"先卡后图"而不是直接让文生图自由发挥：知识卡片的价值是**知识结构**，
不是画面。模型先把结构定下来（标题/难点/误区/正确理解），出图这一步只负责把这份
结构**排得好看**；反过来先出图，画面里出现的知识点就无从校验，也没法进聊天记录做
结构化展示。

卡的字段是**兼容历史口径**的（title/topic/points/summary/pitfall/correct/visual_hint），
不新增字段——出图 Prompt 与前端展示都吃这一套。
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.llm import get_chat_model
from app.core.logging_config import main_logger

# 知识卡最多展示几个难点：超过 3 个，竖版信息图会挤成一团、模型也画不准
MAX_CARD_POINTS = 3
# 素材预算：错题最多带几道、每道题干/点评截多长（素材进提示词，不是进图）
MAX_WRONG_QUESTIONS = 3
MAX_STEM_CHARS = 120
MAX_ANALYSIS_CHARS = 200
MAX_MATERIAL_CHARS = 6_000
# 知识库上下文（考纲/原文）预算：与出题侧共用同一个组装器，这里再收一次上限
MAX_KNOWLEDGE_CONTEXT_CHARS = 4_000

# 出图提示词的固定收尾：这些"不要"是实测最容易毁图的东西（乱码英文、水印、二维码、气泡）
_IMAGE_NEGATIVE_TAIL = (
    "文字言简意赅、少字大字: 图标点缀即可，页面不杂乱; "
    "不要漫画分镜、气泡对话、长段落、水印、二维码、英文乱码。"
)

# 内置的知识卡结构化提示词。`prompt_templates` 表里有 `difficulty_learner:study`
# 的生效版本时会被它替换（运营可改提示词，不改代码）；表里没有就用这一份。
_CARD_SYSTEM_PROMPT = (
    "你是一个难点讲解 Agent，负责把学习难点拆成一张「一图看懂」的知识卡。\n"
    "严格输出 JSON（不要 Markdown 代码块、不要额外解释），字段：\n"
    "{\n"
    '  "title": "≤14 字的标题",\n'
    '  "topic": "所属知识主题（≤12 字）",\n'
    '  "summary": "一句话总述（≤60 字，说清它到底在解决什么问题）",\n'
    '  "points": [{"title": "难点小标题（≤10 字）", "tip": "一句要点（≤24 字）"}],\n'
    '  "pitfall": "最常见的误区（≤40 字）",\n'
    '  "correct": "正确理解（≤40 字）",\n'
    '  "visual_hint": "画面建议（≤30 字，如三个并列图标 / 上下箭头对比）"\n'
    "}\n"
    "硬约束：\n"
    "1. points **最多 3 条**，每条都要是「卡上看一眼就能懂」的短语，不要整句长句；\n"
    "2. 有资料依据时**必须以资料为准**，资料里没有的不要编造；\n"
    "3. 全部输出中文，不要英文、不要 emoji；\n"
    "4. 只讲**这一个**难点，不要展开成课程大纲。"
)


def ensure_list(value: Any) -> list[Any]:
    """把模型给的 list / JSON 字符串 / 单值统一成 list（解析容错用）。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return [text]
        return parsed if isinstance(parsed, list) else [parsed]
    return [value]


def build_material(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    request_text: str,
    knowledge_base_id: int | None = None,
    references: str = "",
) -> dict[str, Any]:
    """组装讲解素材。**每一路都自带降级**，返回结构里的空列表就是「这路没有」。

    `references` 是调用方已经拿到的补充资料（agent loop 里工具检索的原文）：它直接当
    知识库上下文用，此时**不再兜底检索**，也不再看 `knowledge_base_id`——手上已经有原文
    还去查一遍，既多花一次 embedding，又会在提示词里塞两份并不一致的资料。
    """
    practice = _latest_practice(db, user_id=user_id)
    weak_points = _active_weak_points(db, user_id=user_id, knowledge_base_id=knowledge_base_id)
    wrong_questions = _wrong_questions(db, practice=practice) if practice else []
    supplied = (references or "").strip()
    knowledge_context = supplied or _knowledge_context(
        db, knowledge_base_id=knowledge_base_id, request_text=request_text
    )
    return {
        "request_text": (request_text or "").strip(),
        "practice": practice,
        "weak_points": weak_points,
        "wrong_questions": wrong_questions,
        "knowledge_context": knowledge_context,
    }


def render_material(material: dict[str, Any]) -> str:
    """把素材渲染成进提示词的纯文本（空素材返回空串，调用方不要塞空块）。"""
    blocks: list[str] = []

    practice = material.get("practice") or {}
    if practice:
        blocks.append(
            "【最近一次完成的练习】\n"
            f"标题: {practice.get('title') or '未命名'}\n"
            f"总分: {_format_score(practice.get('total_score'))}"
            f"（题数 {practice.get('question_count') or 0}）"
        )

    weak_points = [
        str(item.get("point") or "").strip()
        for item in (material.get("weak_points") or [])
        if isinstance(item, dict) and str(item.get("point") or "").strip()
    ]
    if weak_points:
        blocks.append("【活跃薄弱点】\n" + "\n".join(f"- {item}" for item in weak_points))

    wrong = material.get("wrong_questions") or []
    if wrong:
        lines: list[str] = []
        for index, item in enumerate(wrong, start=1):
            lines.append(
                f"{index}. 题干: {item.get('stem') or ''}\n"
                f"   我的答案: {item.get('my_answer') or '（空）'}\n"
                f"   点评: {item.get('analysis') or '（无点评）'}"
            )
        blocks.append("【最近一次练习里做错的题】\n" + "\n".join(lines))

    knowledge_context = str(material.get("knowledge_context") or "").strip()
    if knowledge_context:
        blocks.append("【知识库资料】\n" + knowledge_context)

    text = "\n\n".join(blocks).strip()
    return text[:MAX_MATERIAL_CHARS]


class DifficultyLearnerAgent:
    """难点讲解 Agent：知识卡结构化 + 图像 Prompt 拼装（不碰数据库写入与对象存储）。"""

    def __init__(self, llm: Any = None, provider: str = "deepseek"):
        self._llm = llm
        self.provider = provider

    # ------------------------------------------------------------------ 结构化
    def prepare_card(
        self, material: dict[str, Any], *, system_prompt: str | None = None
    ) -> dict[str, Any]:
        """把素材压成知识卡（字段口径见模块 docstring）。

        `system_prompt` 非空时**替换**内置的卡结构提示词——留给 `prompt_templates` 表里
        `difficulty_learner:study` 的运营版本（与出题/归档同一套模板机制）。表里没有时
        用内置提示词，所以离线/新库也能跑。
        """
        topic = str(material.get("request_text") or "").strip() or "学习难点"
        fallback = _fallback_card(topic)
        context = render_material(material)
        if not context and not topic:
            return fallback

        prompt = self._build_card_prompt(topic=topic, context=context, system_prompt=system_prompt)
        llm = self._llm or get_chat_model(provider=self.provider)
        try:
            message = llm.invoke(prompt)
            raw = _message_text(message)
            card = normalize_card(_parse_json_object(raw))
        except Exception as exc:  # noqa: BLE001 - 模型/解析失败不能让讲解整条挂掉
            main_logger.warning(f"知识卡结构化失败（降级成最小卡）: {exc}")
            card = {}

        # 模型给的卡缺标题时用用户那句话兜底：宁可标题朴素，也不要出现"知识卡"这种空壳
        return {**fallback, **{key: value for key, value in card.items() if value}}

    def _build_card_prompt(
        self, *, topic: str, context: str, system_prompt: str | None = None
    ) -> str:
        """知识卡提示词。**刻意写得硬**：出图渲染中文长句会糊，所以先约束长度。"""
        system = (system_prompt or "").strip() or _CARD_SYSTEM_PROMPT
        user = f"用户想弄懂的难点：{topic}"
        if context:
            user += f"\n\n可用素材（可能不完整，够用就用）：\n{context}"
        return f"{system}\n\n{user}"

    # ------------------------------------------------------------------ 出图 Prompt
    @staticmethod
    def build_image_prompt(card: dict[str, Any]) -> str:
        """知识卡 → 竖版信息图的图像 Prompt。

        与历史口径一字不差地保留（竖版中文学习知识卡 / 少字大字 / 不要气泡对话），
        只把字段来源钉在 `normalize_card` 归一后的卡上，避免模型字段缺失时拼出空块。
        """
        points: list[str] = []
        for index, item in enumerate(ensure_list(card.get("points") or [])):
            if isinstance(item, dict):
                title = str(item.get("title") or "").strip()
                tip = str(item.get("tip") or "").strip()
                if title or tip:
                    points.append(f"{index}.{title}" + (f"（{tip}）" if tip else ""))
            elif str(item).strip():
                points.append(f"{index}.{str(item).strip()}")

        pitfalls = [str(item).strip() for item in ensure_list(card.get("pitfall")) if str(item).strip()]
        return (
            "竖版中文学习知识卡，信息图，浅色干净背景，清晰好看。"
            f"标题: {card.get('title') or '知识卡'}.主题: {card.get('topic') or ''}."
            f"难点 (≤3): {';'.join(points) or card.get('summary') or ''}."
            f"总述: {card.get('summary') or ''}."
            f"误区: {'；'.join(pitfalls)}. 正确: {card.get('correct') or ''}."
            f"画面: {card.get('visual_hint') or '要点旁配少量简洁小图标，留白充足'}."
            + _IMAGE_NEGATIVE_TAIL
        )


def normalize_card(raw: dict[str, Any]) -> dict[str, Any]:
    """把模型输出（或历史数据）归一成出图 Prompt 与前端都能直接吃的卡。

    - `points` 统一成 `[{"title", "tip"}]`，**最多 3 条**（多的丢掉，图会挤爆）；
    - `pitfall` 统一成 list：模型偶尔会给多个误区，出图时逐个列更清楚；
    - 所有文本去空白 + 按字段限长（出图渲染中文，长句必糊）。
    """
    if not isinstance(raw, dict):
        return {}

    points: list[dict[str, str]] = []
    for item in ensure_list(raw.get("points")):
        if isinstance(item, dict):
            title = _clip_text(item.get("title"), 20)
            tip = _clip_text(item.get("tip"), 48)
            if title or tip:
                points.append({"title": title, "tip": tip})
        else:
            text = _clip_text(item, 48)
            if text:
                points.append({"title": text, "tip": ""})
        if len(points) >= MAX_CARD_POINTS:
            break

    card = {
        "title": _clip_text(raw.get("title"), 28),
        "topic": _clip_text(raw.get("topic"), 24),
        "summary": _clip_text(raw.get("summary"), 120),
        "points": points,
        "pitfall": [
            text for text in (_clip_text(item, 80) for item in ensure_list(raw.get("pitfall"))) if text
        ],
        "correct": _clip_text(raw.get("correct"), 80),
        "visual_hint": _clip_text(raw.get("visual_hint"), 60),
    }
    # summary 缺失时用第一条要点的 tip 顶上：卡片正文不能只有标题
    if not card["summary"] and points:
        card["summary"] = points[0]["tip"] or points[0]["title"]
    return card


def to_payload(card: dict[str, Any]) -> dict[str, Any]:
    """卡的对外载荷形状（接口返回与消息 response_payload 共用，避免两处走形）。

    `pitfall` 对外是**字符串**（前端直接当一句话渲染），内部是 list（出图要逐条列）。
    """
    return {
        "title": card.get("title") or "知识卡",
        "topic": card.get("topic") or "",
        "summary": card.get("summary") or "",
        "points": card.get("points") or [],
        "pitfall": "；".join(card.get("pitfall") or []),
        "correct": card.get("correct") or "",
        "visual_hint": card.get("visual_hint") or "",
    }


# ---------------------------------------------------------------------- 内部工具
def _latest_practice(db: Session, *, user_id: int) -> dict[str, Any] | None:
    """本用户最近一次**已完成**的练习（没有就返回 None，讲解继续用别的素材）。"""
    from app.db.models import PracticeSet

    row = db.scalar(
        select(PracticeSet)
        .where(PracticeSet.user_id == user_id, PracticeSet.status == "completed")
        .order_by(PracticeSet.updated_at.desc(), PracticeSet.id.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {
        "practice_set_id": row.id,
        "run_id": row.run_id,
        "knowledge_base_id": row.knowledge_base_id,
        "title": row.title,
        "status": row.status,
        "total_score": row.total_score,
        "question_count": row.question_count,
    }


def _wrong_questions(db: Session, *, practice: dict[str, Any]) -> list[dict[str, Any]]:
    """那次练习里做错的题：题干 + 我的作答 + 点评（按得分升序，最差的在前）。"""
    from app.db.models import Attempt, Question

    practice_set_id = practice.get("practice_set_id")
    if not practice_set_id:
        return []

    rows = db.execute(
        select(Question, Attempt)
        .outerjoin(Attempt, Attempt.question_id == Question.id)
        .where(Question.practice_set_id == practice_set_id)
        .order_by(Question.id.asc())
    ).all()

    wrong: list[dict[str, Any]] = []
    for question, attempt in rows:
        if attempt is not None and attempt.is_correct is True:
            continue
        # 没作答（attempt 为空）也算"没掌握"：它同样值得讲
        if attempt is not None and attempt.score is not None and attempt.score >= 60:
            continue
        wrong.append(
            {
                "stem": _clip_text(question.stem, MAX_STEM_CHARS),
                "focus_area": question.focus_area,
                "my_answer": _clip_text(_answer_text(attempt.answer if attempt else None), MAX_STEM_CHARS),
                "analysis": _clip_text(attempt.analysis if attempt else None, MAX_ANALYSIS_CHARS),
                "score": attempt.score if attempt else None,
            }
        )

    wrong.sort(key=lambda item: (item.get("score") is not None, item.get("score") or 0))
    return wrong[:MAX_WRONG_QUESTIONS]


def _active_weak_points(
    db: Session, *, user_id: int, knowledge_base_id: int | None
) -> list[dict[str, Any]]:
    """活跃薄弱点（复用掌握度服务的口径，取不到就当没有）。"""
    try:
        from app.services.mastery_service import MasteryService

        return MasteryService.get_active_weak_points(
            db, user_id=user_id, knowledge_base_id=knowledge_base_id, limit=5
        )
    except Exception as exc:  # noqa: BLE001 - 弱点不是讲解的必要条件
        main_logger.warning(f"讲解取薄弱点失败（忽略）: {exc}")
        return []


def _knowledge_context(
    db: Session, *, knowledge_base_id: int | None, request_text: str
) -> str:
    """知识库上下文：有考纲给考纲，同时**按用户这句话**检索一次原文。

    为什么不直接用 `render_for_quiz`：它是"按章节轮换出题"的组装器，与"用户问什么"
    无关。讲解要的是"和这个问题相关的原文"，所以这里先用用户的问句做一次向量检索
    （命中就优先用它），再拿考纲补全局结构。
    """
    if knowledge_base_id is None:
        return ""

    parts: list[str] = []
    retrieved = _retrieve_by_request(
        db, knowledge_base_id=knowledge_base_id, request_text=request_text
    )
    if retrieved:
        parts.append("## 与本次难点最相关的原文\n\n" + retrieved)

    try:
        from app.services.knowledge_context_service import KnowledgeContextService

        outline = KnowledgeContextService.render_for_quiz(
            db, knowledge_base_id=knowledge_base_id
        ).get("context")
    except Exception as exc:  # noqa: BLE001 - 没有考纲不该影响讲解
        main_logger.warning(f"讲解取考纲失败（忽略）: {exc}")
        outline = ""

    if outline:
        parts.append("## 该知识库考纲\n\n" + str(outline))

    context = "\n\n".join(parts).strip()
    if not context:
        return ""
    return context[:MAX_KNOWLEDGE_CONTEXT_CHARS]


def _retrieve_by_request(
    db: Session, *, knowledge_base_id: int | None, request_text: str
) -> str:
    """用用户问句做向量检索，收窄到本知识库的资源（任何失败都返回空串）。"""
    query = (request_text or "").strip()
    if not query or knowledge_base_id is None:
        return ""
    try:
        from app.db.models import KnowledgeBase, Resource
        from app.rag.retriever import search_similar_chunks

        knowledge_base = db.get(KnowledgeBase, knowledge_base_id)
        if knowledge_base is None:
            return ""
        resource_ids = [
            int(rid)
            for rid in db.scalars(
                select(Resource.id).where(Resource.knowledge_base_id == knowledge_base_id)
            )
        ]
        if not resource_ids:
            # 该库还没归档任何资料：直接返回空。不返回空的话下面会退化成全局检索，
            # 用户明明选了库却拿到别处的资料——比"没有资料"更糟。
            return ""
        hits = search_similar_chunks(
            query=query[:200],
            user_id=knowledge_base.user_id,
            db=db,
            top_k=3,
            # ⚠️ 必须在**检索层**就收窄范围（与 `search_material` 同一口径）。
            # 曾经的写法是先全局取 top-3、再在 Python 里丢掉不属于本库的命中：
            # 那样本库明明有相关切片，也会被别库的高分切片挤掉名额，结果**静默少给资料**
            # （检索层按 resource_id 过滤是 MySQL 侧的权威过滤，见 retriever 的说明）。
            resource_ids=resource_ids,
        )
    except Exception as exc:  # noqa: BLE001 - 向量检索是加分项，不是前置条件
        main_logger.warning(f"讲解向量检索失败（忽略）: {exc}")
        return ""

    blocks: list[str] = []
    for hit in hits or []:
        text = str(hit.get("text") or "").strip()
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def _fallback_card(topic: str) -> dict[str, Any]:
    """最小可用卡：模型不可用/输出不可解析时的兜底，保证出图与落库都有东西。"""
    title = _clip_text(topic, 20) or "知识卡"
    return {
        "title": title,
        "topic": "",
        "summary": f"围绕「{title}」的关键理解与常见误区。",
        "points": [],
        "pitfall": [],
        "correct": "",
        "visual_hint": "",
    }


def _parse_json_object(text: str) -> dict[str, Any]:
    """从模型输出里抠出第一个 JSON 对象（容忍 ```json 包裹与前后废话）。"""
    raw = (text or "").strip()
    if not raw:
        return {}
    fenced = re.search(r"```(?:json)?\s*(.+?)```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(raw[start : end + 1])
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _message_text(message: Any) -> str:
    """兼容 str / list[dict] 两种 content 形态（与 workers/_message_text 同口径）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return str(content or "").strip()


def _answer_text(answer: Any) -> str:
    """作答 JSON → 可读文本（选择题/填空题都可能是 list 或 str）。"""
    if answer is None:
        return ""
    if isinstance(answer, dict):
        value = answer.get("value")
        if isinstance(value, list):
            return "、".join(str(item) for item in value if str(item).strip())
        return str(value or "").strip()
    if isinstance(answer, list):
        return "、".join(str(item) for item in answer if str(item).strip())
    return str(answer).strip()


def _format_score(score: Any) -> str:
    if score is None:
        return "未评分"
    try:
        return f"{float(score):.0f} 分"
    except (TypeError, ValueError):
        return str(score)


def _clip_text(value: Any, limit: int) -> str:
    """取文本并限长（出图渲染中文长句会糊，所有字段都要有上限）。"""
    text = str(value).strip() if value is not None else ""
    return text[:limit]


__all__ = [
    "DifficultyLearnerAgent",
    "build_material",
    "ensure_list",
    "normalize_card",
    "render_material",
    "to_payload",
]
