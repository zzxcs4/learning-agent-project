"""学习测评工作流：批量出题、批量评分、提取薄弱点。"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select

from app.agent.workflows.common import ensure_list, invoke_json
from app.agent.workflows.state import LearningWorkflowState
from app.core.logging_config import main_logger
from app.db.models import KnowledgeBase, PracticeSet, Question, UserKnowledgeMastery
from app.services.knowledge_context_service import (
    DEFAULT_MODE,
    KnowledgeContextService,
    bump_asked_count,
)
from app.services.mastery_service import MasteryService
from app.services.practice_service import PracticeService
from app.services.prompt_template_service import AGENT_CONFIG, prompt_template_manager
from app.services.quiz_params_service import normalize_difficulty
from app.services.reflection_service import ReflectionService


def _json_text(value: Any) -> str:
    """生成给模型看的紧凑 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, indent=2)


def _state_get(state: LearningWorkflowState | dict[str, Any], key: str, default: Any = None) -> Any:
    """兼容 Pydantic 状态对象和 dict 状态的字段读取。"""
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _render_prompt_template(template: str, variables: dict[str, Any]) -> str:
    """用简单替换渲染提示词，避免 JSON 示例中的大括号被 format 误处理。"""
    result = template
    for key, value in variables.items():
        result = result.replace("{" + key + "}", str(value))
    return result.strip()


def _get_template_prompt(db: Any, agent_name: str) -> str | None:
    """从提示词表获取模板；失败时返回 None，让调用方走硬编码兜底。"""
    if db is None:
        return None
    scene = AGENT_CONFIG.get(agent_name, {}).get("scene")
    if not scene:
        return None
    return prompt_template_manager.get_full_prompt(agent_name, scene, db)


def _build_quiz_prompt(state: LearningWorkflowState) -> str:
    return f"""
你是出题题库 Agent。请根据用户学习要求生成一套练习题。

用户要求：{_state_get(state, "request_text", "")}
题目数量：{_state_get(state, "question_count") or "用户未指定时默认 10 题"}
本次难度：{_difficulty_label(state)}（入门=概念与记忆为主；进阶=概念辨析+少量变式；高级=跨章综合与易错辨析）

只输出 JSON，不要输出 Markdown，不要输出 HTML。
JSON 格式必须如下：
{{
  "title": "练习标题",
  "description": "练习说明",
  "question_count": 5,
  "questions": [
    {{
      "id": "q1",
      "type": "radio",
      "stem": "题干",
      "options": [{{"value": "A", "label": "选项内容"}}],
      "required": true,
      "points": 20,
      "difficulty": 3
    }}
  ]
}}

约束：
1. type 只能是 radio、checkbox、textarea。
2. radio 表示单选题，checkbox 表示多选题，textarea 表示简答题。
3. radio 和 checkbox 必须提供 options，textarea 的 options 为空数组。
4. 每题 id 使用 q1、q2 递增。
5. points 总分建议合计 100。
6. difficulty 用 1–5 的整数：1 最易、5 最难，按知识点的真实认知难度标（不按题目长短）；
   **必须落在本次难度对应的区间内**（见上文"本次难度"给出的区间）。
7. 不要输出答案，不要输出解析。
""".strip()


def _build_dynamic_quiz_prompt(state: LearningWorkflowState, db: Any = None) -> str:
    template = _get_template_prompt(db, "quiz_generate_workflow")
    if not template:
        return _build_quiz_prompt(state)
    question_count = _state_get(state, "question_count") or "用户未指定时默认 10 题"
    return _render_prompt_template(
        template,
        {
            "request_text": _state_get(state, "request_text", ""),
            "question_count": question_count,
            "difficulty": _difficulty_label(state),
        },
    )


def _build_evaluation_prompt(state: LearningWorkflowState, references: str = "") -> str:
    return f"""
你是学习测评 Agent。请根据题目和用户答案完成评分、点评和薄弱点提取。

题目：
{_json_text(_state_get(state, "quiz", {}))}

用户答案：
{_json_text(_state_get(state, "answers", []))}
{_reference_block(references)}

只输出 JSON，不要输出 Markdown。
JSON 格式必须如下：
{{
  "score": 85,
  "items": [
    {{
      "question_id": "q1",
      "score": 20,
      "is_correct": true,
      "correct_answer": "标准答案或参考要点",
      "analysis": "本题点评"
    }}
  ],
  "weaknesses": ["薄弱点1", "薄弱点2"],
  "review": "给用户看的总评和学习建议",
  "display_text": "给前端直接展示的完整中文点评文本"
}}

要求：
1. score 是 0 到 100 的整数。
2. 每道题都必须评分并给出 analysis。
3. weaknesses 最多 5 个，使用短语。
4. review 用中文，简洁具体。
5. display_text 必须是详细自然语言字符串，适合直接展示给用户。
6. display_text 必须包含总分、整体评价、逐题点评、参考答案或参考要点、薄弱点和学习建议。
7. display_text 不要输出 JSON 字符串，不要使用 Markdown 表格。
""".strip()


def _build_dynamic_evaluation_prompt(
    state: LearningWorkflowState, db: Any = None, references: str = ""
) -> str:
    template = _get_template_prompt(db, "learning_evaluator")
    if not template:
        return _build_evaluation_prompt(state, references)
    prompt = _render_prompt_template(
        template,
        {
            "quiz_json": _json_text(_state_get(state, "quiz", {})),
            "answers_json": _json_text(_state_get(state, "answers", [])),
            "references": references,
        },
    )
    return f"""
{prompt}
{_reference_block(references)}

补充要求：
1. display_text 必须是详细自然语言字符串，适合直接展示给用户。
2. display_text 必须包含总分、整体评价、逐题点评、参考答案或参考要点、薄弱点和学习建议。
3. display_text 不要输出 JSON 字符串，不要使用 Markdown 表格。
""".strip()


def _reference_block(references: str) -> str:
    """评测用的参考资料块；没有原文时返回空串（由调用方在日志里标注"无资料依据"）。"""
    text = (references or "").strip()
    if not text:
        return ""
    return (
        "\n参考资料（评分与参考答案以此为准；参考资料没覆盖到的题，按题目内容本身判定）：\n"
        f"{text}\n"
    )


def _with_quiz_context(prompt: str, context: str) -> str:
    """把知识库上下文追加到出题提示词末尾。

    没有上下文（没选库 / 库还没考纲）时原样返回——这是 §八 验收要求的"不选知识库也能出题"。
    """
    text = (context or "").strip()
    if not text:
        return prompt
    return (
        f"{prompt}\n\n---\n"
        "下面是这套资料的知识边界（考纲）。出题必须落在这些章节内，"
        "并按「最常考什么」的优先级分配题目，不要超出范围：\n"
        f"{text}\n\n"
        "**优先级**：范围以上面的考纲为准。上面「用户要求」里若出现考纲之外的知识点"
        "（例如某个自拟的章节名或技术名词），一律忽略——只把它当作题量/侧重的提示，"
        "绝对不要按它去编造考纲里没有的内容。"
    )


# 发散度档位对应的出题行为（写进提示词，让模型按档位分配范围与题量）。
# 文案口径与用户 2026-10-19 给的说明一致（参数卡里显示的就是这一份；原项目 TechSpar 的
# training.divergence 五档）：低档往薄弱点打，高档往没涉猎过的地方探。
DIVERGENCE_HINTS = {
    1: "聚焦薄弱：100% 针对存在弱点的知识域，适合考前专项突击；薄弱点相关题可占一半以上，不要跨界。",
    2: "侧重薄弱：约 70% 针对薄弱点、30% 拓展至新知识点；以核心概念为主，可少量变式。",
    3: "均衡：薄弱环节巩固与全新知识盲区发掘各占 50%（核心概念约 60%、迁移与变式约 40%）。",
    4: "侧重探索：约 30% 回顾薄弱点、70% 探索全新知识层面；多出跨章节综合题与边界情形。",
    5: "全面探索：100% 探索未涉猎过的新知识领域，发掘潜在盲区；优先冷门考点与近似概念辨析，可以完全不围绕薄弱点。",
}

# 难度三档 → 允许的每题 `difficulty`（1–5）区间 + 认知层级要求。
# 三档是"目标难度"，不是给某道题定死分：模型仍按知识点真实认知难度标，只是必须落在区间内。
DIFFICULTY_RANGES = {"入门": (1, 2), "进阶": (2, 4), "高级": (4, 5)}
DIFFICULTY_HINTS = {
    "入门": "题目以核心概念、定义、记忆与直接应用为主，不要出跨章综合题；题干直白，一问一答。",
    "进阶": "题目要有概念辨析与少量变式，可以有 1 道跨知识点的综合题；多数题是常规难度。",
    "高级": "题目要出跨章综合、边界情形与易错写法辨析；至少一半题目需要推理或多步骤判断。",
}



def _load_learner_profile(db: Any, state: LearningWorkflowState) -> dict[str, Any]:
    """读掌握度与活跃弱点（个性化注入用）。

    任何异常都退化为空：个性化是增强，不能反过来让出题失败。
    """
    profile: dict[str, Any] = {"mastery": None, "weak_points": []}
    if db is None:
        return profile
    user_id = _state_get(state, "user_id")
    if not user_id:
        return profile

    knowledge_base_id = _state_get(state, "knowledge_base_id")
    try:
        if knowledge_base_id:
            row = db.scalar(
                select(UserKnowledgeMastery).where(
                    UserKnowledgeMastery.user_id == int(user_id),
                    UserKnowledgeMastery.knowledge_base_id == int(knowledge_base_id),
                )
            )
            if row is not None:
                score = round(float(row.mastery_score or 0.0), 1)
                profile["mastery"] = {
                    "score": score,
                    "zone": MasteryService.zone_label(score),
                    "practice_count": int(row.practice_count or 0),
                }
        profile["weak_points"] = MasteryService.get_active_weak_points(
            db,
            user_id=int(user_id),
            knowledge_base_id=int(knowledge_base_id) if knowledge_base_id else None,
            limit=5,
        )
    except Exception as exc:  # noqa: BLE001 - 画像读取失败不影响出题
        main_logger.warning(f"读取学习者画像失败（本轮不注入个性化）: {exc}")
        return {"mastery": None, "weak_points": []}
    return profile


def _learner_block(profile: dict[str, Any]) -> str:
    """把掌握度与弱点拼成提示词块；两者都没有时返回空串（新用户首次出题就是这种情况）。"""
    lines: list[str] = []
    mastery = profile.get("mastery")
    if mastery:
        lines.append(
            "- 该知识库掌握度：{score}（{zone}），已练 {count} 场".format(
                score=mastery["score"],
                zone=mastery["zone"],
                count=mastery["practice_count"],
            )
        )
    weak_points = profile.get("weak_points") or []
    if weak_points:
        lines.append("- 用户反复出现的薄弱点（按出现次数排序）：")
        for index, item in enumerate(weak_points, start=1):
            lines.append(
                "  {i}. {text}（出现 {times} 次）".format(
                    i=index,
                    text=str(item.get("point") or "").strip(),
                    times=int(item.get("times_seen") or 0),
                )
            )
        lines.append("- 出题时薄弱点要有覆盖，但**不要整卷都围绕薄弱点**（那只是复习，不会进步）。")
    if not lines:
        return ""
    return "\n\n---\n## 学习者画像（个性化出题依据）\n" + "\n".join(lines)


def _divergence_block(level: Any) -> str:
    """发散度档位的出题行为说明；非法值一律按 3 处理。"""
    try:
        resolved = max(1, min(5, int(level or 3)))
    except (TypeError, ValueError):
        resolved = 3
    return f"\n\n---\n## 本轮发散度：{resolved}/5\n{DIVERGENCE_HINTS[resolved]}"


def _resolved_difficulty(state: LearningWorkflowState | dict[str, Any]) -> str:
    """本次难度（三档）；未定/非法值按画像口径的默认"进阶"。"""
    return normalize_difficulty(_state_get(state, "difficulty"))


def _difficulty_label(state: LearningWorkflowState | dict[str, Any]) -> str:
    """基础提示词里的难度一行：档位 + 允许的 difficulty 区间（模型要照着落值）。"""
    level = _resolved_difficulty(state)
    low, high = DIFFICULTY_RANGES[level]
    return f"{level}（每题 difficulty 取 {low}–{high}）"


def _difficulty_block(state: LearningWorkflowState | dict[str, Any]) -> str:
    """难度档位的出题行为说明（与发散度块并列写进提示词）。"""
    level = _resolved_difficulty(state)
    low, high = DIFFICULTY_RANGES[level]
    return (
        f"\n\n---\n## 本次难度：{level}\n{DIFFICULTY_HINTS[level]}\n"
        f"每题 `difficulty` 必须取 {low}–{high} 之间的整数。"
    )


def _load_recent_question_stems(
    db: Any, state: LearningWorkflowState, limit: int = 8
) -> list[str]:
    """最近出过的题干（跨知识库取，因为重复题跨库也一样烦人）。取不到返回空。"""
    if db is None:
        return []
    user_id = _state_get(state, "user_id")
    if not user_id:
        return []
    try:
        rows = db.scalars(
            select(Question.stem)
            .join(PracticeSet, PracticeSet.id == Question.practice_set_id)
            .where(PracticeSet.user_id == int(user_id))
            .order_by(Question.id.desc())
            .limit(limit)
        )
        return [str(stem).strip() for stem in rows if str(stem or "").strip()]
    except Exception as exc:  # noqa: BLE001 - 去重提示失败不影响出题
        main_logger.warning(f"读取最近题目失败（本轮不做去重提示）: {exc}")
        return []


def _recent_questions_block(stems: list[str]) -> str:
    if not stems:
        return ""
    lines = ["\n\n---\n## 最近已经出过的题（**不要重复，也不要出高度相似的题**）"]
    lines.extend(f"- {stem[:60]}" for stem in stems)
    return "\n".join(lines)


def _compose_quiz_prompt(
    db: Any,
    state: LearningWorkflowState,
    context_info: dict[str, Any],
    profile: dict[str, Any],
    recent_stems: list[str],
) -> str:
    """最终出题提示词 = 基础模板 + 考纲上下文 + 学习者画像 + 发散度 + 难度 + 最近题目去重。"""
    prompt = _build_dynamic_quiz_prompt(state, db)
    prompt = _with_quiz_context(prompt, context_info.get("context") or "")
    prompt += _learner_block(profile)
    prompt += _divergence_block(_state_get(state, "divergence"))
    prompt += _difficulty_block(state)
    prompt += _recent_questions_block(recent_stems)
    return prompt


def _is_valid_question(question: dict[str, Any]) -> bool:
    """只接受前端可识别的表单题型。"""
    return str(question.get("type") or "").strip() in {"radio", "checkbox", "textarea"}


def _persist_practice_set(db: Any, state: LearningWorkflowState, quiz: dict[str, Any]) -> dict[str, Any]:
    """出题落库：写 practice_sets / questions，返回要合并进状态的主键。

    落库失败只记日志、不让出题失败——出题是主链路，闭环落库是增强，不能反过来拖垮它。
    """
    if db is None:
        return {}
    user_id = _state_get(state, "user_id")
    if not user_id:
        return {}

    knowledge_base_id = _state_get(state, "knowledge_base_id")
    try:
        practice_set = PracticeService.create_practice_set(
            db=db,
            user_id=int(user_id),
            knowledge_base_id=int(knowledge_base_id) if knowledge_base_id else None,
            title=str(quiz.get("title") or "学习练习"),
            questions=[item for item in ensure_list(quiz.get("questions")) if isinstance(item, dict)],
            # 关联本条 workflow run：练习详情回看 review/reflection、归档工具定位都靠它
            run_id=str(_state_get(state, "run_id", "")).strip() or None,
        )
    except Exception as exc:  # noqa: BLE001 - 落库异常不应中断出题
        main_logger.warning(f"练习题落库失败（不影响出题）: {exc}")
        return {}
    return {"practice_set_id": practice_set.id}


def _persist_attempts(
    db: Any, state: LearningWorkflowState, score: int, evaluation: dict[str, Any]
) -> None:
    """评测落库：把评分明细写成 attempts，并把总分/状态回写 practice_sets。

    同样只记日志不抛错：评分结果已经拿到，落库失败不能让它变成 500。
    """
    if db is None:
        return
    user_id = _state_get(state, "user_id")
    practice_set_id = _state_get(state, "practice_set_id")
    if not user_id or not practice_set_id:
        return

    try:
        created = PracticeService.record_attempts(
            db=db,
            user_id=int(user_id),
            practice_set_id=int(practice_set_id),
            items=[item for item in ensure_list(evaluation.get("items")) if isinstance(item, dict)],
            answers=[item for item in ensure_list(_state_get(state, "answers")) if isinstance(item, dict)],
            score=score,
        )
        main_logger.info(f"练习 {practice_set_id} 已落库 {created} 条答题记录")
    except Exception as exc:  # noqa: BLE001 - 落库异常不应中断评分
        main_logger.warning(f"答题记录落库失败（不影响评分）: {exc}")


def _update_mastery(db: Any, state: LearningWorkflowState, weaknesses: list[str]) -> None:
    """掌握度 + 弱点沉淀：EMA 滚动 `user_knowledge_mastery`，弱点按相似度阶梯归并写 `user_profiles`。

    同样只记日志不抛错——评分已经拿到，掌握度算失败也不能把这次练习变成 500。
    """
    if db is None:
        return
    user_id = _state_get(state, "user_id")
    practice_set_id = _state_get(state, "practice_set_id")
    if not user_id or not practice_set_id:
        return

    try:
        summary = MasteryService.update_after_practice(
            db=db,
            user_id=int(user_id),
            practice_set_id=int(practice_set_id),
            knowledge_base_id=_state_get(state, "knowledge_base_id"),
            weaknesses=weaknesses,
        )
        main_logger.info(f"练习 {practice_set_id} 掌握度/弱点更新: {summary}")
    except Exception as exc:  # noqa: BLE001 - 掌握度异常不应中断评分
        main_logger.warning(f"掌握度更新失败（不影响评分）: {exc}")


def _load_knowledge_context(db: Any, state: LearningWorkflowState) -> dict[str, Any]:
    """组装出题上下文并打日志（§八 验收要求日志能证明有/无 knowledge_context）。"""
    empty: dict[str, Any] = {
        "context": "",
        "mode": DEFAULT_MODE,
        "sections": [],
        "chars": 0,
        "with_source": False,
        "reason": "db 不可用",
    }
    if db is None:
        return empty
    try:
        try:
            divergence = int(_state_get(state, "divergence") or 3)
        except (TypeError, ValueError):
            divergence = 3
        info = KnowledgeContextService.render_for_quiz(
            db,
            knowledge_base_id=_state_get(state, "knowledge_base_id"),
            mode=_state_get(state, "context_mode") or DEFAULT_MODE,
            # 低发散度（≤2）= "往练过的地方多打几遍"，所以反着选章
            prefer_reviewed=divergence <= 2,
        )
    except Exception as exc:  # noqa: BLE001 - 上下文组装失败不应中断出题
        main_logger.warning(f"知识库上下文组装失败（退化为无上下文）: {exc}")
        return empty
    main_logger.info(
        "出题知识库上下文: mode={mode} 字数={chars} 章节={sections} 含原文={with_source} 说明={reason}".format(
            mode=info.get("mode"),
            chars=info.get("chars"),
            sections=info.get("sections"),
            with_source=info.get("with_source"),
            reason=info.get("reason"),
        )
    )
    # 原文定位明细（可审计：哪一章、按什么方式、几片、最高相似度、检索 query）
    for detail in info.get("source_detail") or []:
        main_logger.info(
            "  原文定位: 章节={heading} 方式={by} 片数={chunks} 最高相似度={score} query=«{query}»".format(
                heading=detail.get("heading"),
                by=detail.get("by"),
                chunks=detail.get("chunks"),
                score=detail.get("top_score", "-"),
                query=detail.get("query", "-"),
            )
        )
    return info


def _bump_section_asked(db: Any, state: LearningWorkflowState, sections: list[str]) -> None:
    """把本次**被选作出题上下文**的章节 `asked_count += 1` / `last_asked_at` 写回 `quiz_core`。"""
    if db is None or not sections:
        return
    knowledge_base_id = _state_get(state, "knowledge_base_id")
    if not knowledge_base_id:
        return
    try:
        knowledge_base = db.get(KnowledgeBase, int(knowledge_base_id))
        if knowledge_base is None:
            return
        updated = bump_asked_count(
            knowledge_base.quiz_core, sections, now_iso=date.today().isoformat()
        )
        if updated is None:
            return
        knowledge_base.quiz_core = updated
        db.commit()
        main_logger.info(f"quiz_core 章节计数已回写: {sections}")
    except Exception as exc:  # noqa: BLE001 - 计数回写失败不影响出题
        db.rollback()
        main_logger.warning(f"章节 asked_count 回写失败（不影响出题）: {exc}")


def _load_references(db: Any, state: LearningWorkflowState) -> str:
    """评测参考资料：优先用出题时那几章的原文（同批原文，评分才有依据）。"""
    if db is None:
        return ""
    try:
        references = KnowledgeContextService.render_references(
            db,
            knowledge_base_id=_state_get(state, "knowledge_base_id"),
            sections=_state_get(state, "context_sections") or [],
        )
    except Exception as exc:  # noqa: BLE001 - 参考资料失败不阻断评测
        main_logger.warning(f"评测参考资料组装失败（评分将无资料依据）: {exc}")
        return ""
    if references:
        main_logger.info(f"评测参考资料: {len(references)} 字符")
    else:
        main_logger.warning("评测没有取到参考资料：本次评分无资料依据")
    return references


def create_prepare_learning_context_node():
    """创建学习上下文准备节点。"""

    def node(state: LearningWorkflowState) -> dict[str, Any]:
        return {"status": "generating_quiz"}

    return node


def create_generate_quiz_node(llm=None, db: Any = None):
    """创建出题节点。"""

    def node(state: LearningWorkflowState) -> dict[str, Any]:
        # 出题上下文：默认只给考纲；用户可选"考纲 + 本章原文"。没选库 / 库还没考纲时为空串，
        # 出题照常进行（§八 验收要求的"不选知识库也能出题"）。
        context_info = _load_knowledge_context(db, state)
        # 个性化三件套：画像（掌握度+弱点）、发散度、最近题目去重
        profile = _load_learner_profile(db, state)
        recent_stems = _load_recent_question_stems(db, state)
        prompt = _compose_quiz_prompt(db, state, context_info, profile, recent_stems)
        main_logger.info(
            "个性化注入: 掌握度={mastery} 弱点数={weak} 发散度={divergence} 去重题数={recent}".format(
                mastery=(profile.get("mastery") or {}).get("score", "无"),
                weak=len(profile.get("weak_points") or []),
                divergence=_state_get(state, "divergence") or 3,
                recent=len(recent_stems),
            )
        )
        data = invoke_json(llm, prompt, provider="deepseek")
        questions = [
            item
            for item in ensure_list(data.get("questions"))
            if isinstance(item, dict) and _is_valid_question(item)
        ]
        quiz = {
            "title": str(data.get("title") or "学习练习"),
            "description": str(data.get("description") or "请完成以下题目。"),
            "question_count": len(questions),
            "questions": questions,
        }
        # 出题成功后立刻落库：practice_sets / questions，并把 practice_set_id 写回状态，
        # 供评测节点（跨请求恢复执行）回写 attempts。
        updates: dict[str, Any] = {
            "quiz": quiz,
            "status": "waiting_answer",
            "context_mode": context_info["mode"],
            "context_sections": context_info["sections"],
        }
        updates.update(_persist_practice_set(db, state, quiz))
        # 记账："这一章这次被用作出题上下文了"（≠ 考过——题实际考哪章无从得知）
        _bump_section_asked(db, state, context_info["sections"])
        return updates

    return node


def create_wait_user_answer_node():
    """创建等待用户提交答案的中断节点。"""

    def node(state: LearningWorkflowState) -> dict[str, Any]:
        from langgraph.types import interrupt

        answers = interrupt({"quiz": _state_get(state, "quiz", {})})
        return {"answers": ensure_list(answers), "status": "grading"}

    return node


def create_evaluate_and_review_node(llm=None, db: Any = None):
    """创建评分点评节点。"""
    from app.agent.a2a import A2AEnvelope

    def node(state: LearningWorkflowState) -> dict[str, Any]:
        # 评测必须带原文：没有原文，评分就成了模型自评，掌握度/弱点会失去依据
        references = _load_references(db, state)
        data = invoke_json(
            llm, _build_dynamic_evaluation_prompt(state, db, references), provider="deepseek"
        )
        score = int(data.get("score") or 0)
        score = max(0, min(score, 100))
        weaknesses = [str(item) for item in ensure_list(data.get("weaknesses")) if str(item).strip()]
        evaluation = {"score": score, "items": ensure_list(data.get("items"))}
        review = str(data.get("display_text") or data.get("review") or "本次练习已完成，请根据错题继续复习。")
        # 评分明细落库：attempts + 回写 questions.answer + 更新 practice_sets 状态
        _persist_attempts(db, state, score, evaluation)
        # 第 3 期反思（升级规划 §7.2）：判"是否覆盖薄弱点"要用出题时注入的那批弱点，
        # 所以输入组装必须在 _update_mastery 之前——归并后混入本场新弱点就是循环论证
        # （本场刚暴露的弱点，更早的出题不可能覆盖）。组装失败在服务内收敛，不影响评分。
        reflection_inputs = ReflectionService.collect_inputs(
            db,
            state=state,
            recent_stems=_load_recent_question_stems(db, state),
            score=score,
        )
        # 有了 attempts 才能算掌握度与弱点（EMA 按 knowledge_base_id + 弱点相似度归并）
        _update_mastery(db, state, weaknesses)
        # 自评紧跟归并之后执行；结论只进 state（→ workflow_runs.state_json = trace），只评不改
        reflection = ReflectionService.reflect(
            llm, reflection_inputs, provider=str(_state_get(state, "provider") or "deepseek")
        )
        has_weakness = bool(weaknesses)
        envelope = A2AEnvelope(
            from_agent="learning_evaluator",
            payload_type="weak_points",
            payload={
                "run_id": _state_get(state, "run_id", ""),
                "score": score,
                "weaknesses": weaknesses,
                "review": review,
            },
            next_agent="note_archiver" if has_weakness else None,
            confidence=0.9 if has_weakness else 0.4,
        )
        return {
            "evaluation": evaluation,
            "score": score,
            "weaknesses": weaknesses,
            "review": review,
            "reflection": reflection,
            "a2a_envelope": envelope.model_dump(),
            "status": "completed",
        }

    return node


def create_finalize_learning_result_node():
    """创建学习测评完成节点。"""

    def node(state: LearningWorkflowState) -> dict[str, Any]:
        return {"status": "completed"}

    return node


def build_learning_graph(llm=None, checkpointer=None, db: Any = None):
    """构建完整学习图：出题 -> 中断等待答案 -> 评分 -> 完成。"""
    from langgraph.graph import END, StateGraph

    builder = StateGraph(LearningWorkflowState)
    builder.add_node("generate_quiz", create_generate_quiz_node(llm, db))
    builder.add_node("wait_user_answer", create_wait_user_answer_node())
    builder.add_node("evaluate_answer", create_evaluate_and_review_node(llm, db))
    builder.add_node("finalize_result", create_finalize_learning_result_node())
    builder.set_entry_point("generate_quiz")
    builder.add_edge("generate_quiz", "wait_user_answer")
    builder.add_edge("wait_user_answer", "evaluate_answer")
    builder.add_edge("evaluate_answer", "finalize_result")
    builder.add_edge("finalize_result", END)
    return builder.compile(checkpointer=checkpointer)


def build_start_learning_graph(llm=None, db: Any = None):
    """构建启动学习图：准备上下文 -> 生成题目。"""
    from langgraph.graph import END, StateGraph

    builder = StateGraph(LearningWorkflowState)
    builder.add_node("prepare_learning_context", create_prepare_learning_context_node())
    builder.add_node("generate_quiz", create_generate_quiz_node(llm, db))
    builder.set_entry_point("prepare_learning_context")
    builder.add_edge("prepare_learning_context", "generate_quiz")
    builder.add_edge("generate_quiz", END)
    return builder.compile()


def build_submit_learning_answer_graph(llm=None, db: Any = None):
    """构建提交答案图：评分、点评、薄弱点提取。"""
    from langgraph.graph import END, StateGraph

    builder = StateGraph(LearningWorkflowState)
    builder.add_node("evaluate_and_review", create_evaluate_and_review_node(llm, db))
    builder.set_entry_point("evaluate_and_review")
    builder.add_edge("evaluate_and_review", END)
    return builder.compile()
