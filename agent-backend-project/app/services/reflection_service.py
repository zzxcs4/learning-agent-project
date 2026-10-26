"""评测后反思（升级规划 §7.2）：一次自评，检查"这套题出得好不好"，结论写进 trace。

## 双向判据（§7.2 明确"判据必须双向"，依据 §5.3）

- **聚焦组**：出题是否覆盖薄弱点——只查这一条，系统会退化成"围着薄弱点打转"；
- **发散组**：考纲章节是否轮换、是否与最近练习重复、难度是否合适。

## 落点与口径

- **挂在评测节点尾部**：全套判据的输入（题干、得分、弱点清单、最近题干）在评测
  完成后才齐；
- **弱点清单取"归并前"**：出题时注入的是归并前的画像（`_load_learner_profile` 同源：
  `get_active_weak_points(limit=5)`）。用归并后的清单判覆盖是**循环论证**——本场
  刚暴露的弱点，更早的出题不可能覆盖。所以输入组装必须在 `_update_mastery` 之前；
- **只评不改**：结论只写 `state.reflection`（→ `workflow_runs.state_json`），不重出题、
  不改分数、不阻塞评测结果返回；
- **失败一律收敛成 `overall="skipped"`**：自评是质量信号，信号缺了不能把一次成功的
  评测变成失败——与 ToolRuntime / MCP 客户端同一契约。

## 结论口径

四个判据的 verdict 由模型给出但**经过白名单收敛**（不在集合内的值一律收成
`unknown`——模型输出不可作契约）；同时 `weak_points` / `chapters` / `recent_stems` /
`score` 任一项缺失时，对应判据**强制 unknown**（护栏不能只写在提示词里，否则
模型可以对着空输入编出"覆盖良好"）。

`overall` **不由模型直接给出**，而是从四个 verdict 推导，三档：

- 出现任意已知坏结论（missed / narrow / repetitive / too_easy / too_hard）→ `warn`；
- 四个判据**全为 unknown**（新用户零信息）→ `unknown`——"不知道"就说不知道，
  不能假装通过；
- 其余 → `pass`。

结论必须可复现，"模型说好"不算数；`unknown` 不触发 `warn`（证据不足不是坏结论）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.agent.workflows.common import ensure_list, invoke_json
from app.core.logging_config import main_logger
from app.services.mastery_service import MasteryService

# 各判据允许的 verdict（白名单之外一律收成 unknown）
_COVERAGE_VERDICTS = {"good", "partial", "missed", "unknown"}
_ROTATION_VERDICTS = {"diverse", "narrow", "unknown"}
_DUPLICATION_VERDICTS = {"fresh", "repetitive", "unknown"}
_DIFFICULTY_VERDICTS = {"too_easy", "appropriate", "too_hard", "unknown"}
# 这些 verdict 出现任意一个 → overall = warn
_BAD_VERDICTS = {"missed", "narrow", "repetitive", "too_easy", "too_hard"}

# 自评指令与输出 schema（花括号是字面量，不走 str.format）
_INSTRUCTIONS = """
请做双向自评，两组判据都要给结论，不能只看薄弱点：
1. 聚焦组：逐条判断"活跃薄弱点"是否被本轮题目覆盖（covered 给出覆盖它的题号或题干关键词，missed 给出没覆盖的弱点原文）；
2. 发散组：章节是否轮换（本轮章节之外是否还有别的章节被练到——信息不足给 unknown）；题干与最近练习是否重复；难度是否合适（依据得分：>=90 偏易、<40 偏难、中间合适，结合作答情况给证据）。
只输出 JSON（不要输出其它文字），结构固定：
{"weak_point_coverage": {"verdict": "good|partial|missed|unknown", "covered": ["..."], "missed": ["..."]},
 "chapter_rotation": {"verdict": "diverse|narrow|unknown", "chapters": ["..."]},
 "duplication": {"verdict": "fresh|repetitive|unknown", "duplicates": ["..."]},
 "difficulty": {"verdict": "too_easy|appropriate|too_hard|unknown", "evidence": "一句话证据"},
 "suggestions": ["最多 3 条，给下一次出题的具体动作"]}"""


class ReflectionService:
    """评测后的出题质量自评（只评不改，失败降级 skipped）。"""

    # ------------------------------------------------------------------ 输入组装
    @staticmethod
    def collect_inputs(
        db: Session | None,
        *,
        state: Any,
        recent_stems: list[str] | None = None,
        score: int | None = None,
    ) -> dict[str, Any]:
        """自评输入的确定性组装（全部来自真实数据，不含任何模型转述）。

        `recent_stems` 由调用方传入（评测节点复用 `_load_recent_question_stems`，
        同一份"最近 8 题"既服务出题去重也服务这里的查重，不查两遍库）。
        """
        user_id = _state_get(state, "user_id")
        knowledge_base_id = _state_get(state, "knowledge_base_id")

        weak_points: list[str] = []
        try:
            items = MasteryService.get_active_weak_points(
                db,
                user_id=int(user_id),
                knowledge_base_id=int(knowledge_base_id) if knowledge_base_id else None,
                limit=5,
            )
            weak_points = [str(item.get("point") or "").strip() for item in items]
            weak_points = [point for point in weak_points if point]
        except Exception as exc:  # noqa: BLE001 - 弱点读不到 → 判据降级为 unknown，不抛
            main_logger.warning(f"反思输入：弱点清单读取失败（coverage 将为 unknown）: {exc}")

        quiz = _state_get(state, "quiz") or {}
        questions = [
            {
                "stem": str(item.get("stem") or item.get("question") or ""),
                "focus_area": item.get("focus_area"),
            }
            for item in ensure_list(quiz.get("questions"))
            if isinstance(item, dict)
        ]

        return {
            "weak_points": weak_points,
            "chapters": [str(section) for section in ensure_list(_state_get(state, "context_sections"))],
            "questions": questions,
            "recent_stems": [str(stem) for stem in (recent_stems or [])],
            "score": score,
        }

    # ------------------------------------------------------------------ 自评
    @staticmethod
    def reflect(llm: Any, inputs: dict[str, Any], *, provider: str = "deepseek") -> dict[str, Any]:
        """一次 LLM 自评：结论经白名单收敛；任何失败收敛成 skipped（绝不抛）。"""
        try:
            data = invoke_json(llm, _build_prompt(inputs), provider=provider)
        except Exception as exc:  # noqa: BLE001 - 自评失败不打断评测
            main_logger.warning(f"出题质量自评失败（本次跳过）: {exc}")
            return {"overall": "skipped", "reason": f"{type(exc).__name__}: {exc}"}
        return _normalize(data if isinstance(data, dict) else {}, inputs)

    @staticmethod
    def after_evaluate(
        db: Session | None,
        *,
        state: Any,
        recent_stems: list[str] | None = None,
        score: int | None = None,
        llm: Any = None,
        provider: str = "deepseek",
    ) -> dict[str, Any]:
        """评测节点的一体化入口：组装 + 自评，任何一步失败都收敛成 skipped。"""
        try:
            inputs = ReflectionService.collect_inputs(
                db, state=state, recent_stems=recent_stems, score=score
            )
        except Exception as exc:  # noqa: BLE001
            return {"overall": "skipped", "reason": f"输入组装失败: {type(exc).__name__}: {exc}"}
        return ReflectionService.reflect(llm, inputs, provider=provider)


# ---------------------------------------------------------------------------
# 内部：提示词与结论收敛
# ---------------------------------------------------------------------------
def _state_get(state: Any, key: str, default: Any = None) -> Any:
    """兼容 pydantic 模型与 dict 两种 state（与 learning.py 同名助手同口径）。"""
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def _build_prompt(inputs: dict[str, Any]) -> str:
    lines: list[str] = ["你是出题质量的自评员。下面是一次刚结束的练习的全部输入，请检查这套题出得好不好。"]

    weak_points = inputs.get("weak_points") or []
    if weak_points:
        lines.append("【本轮出题针对的活跃薄弱点】")
        lines.extend(f"- {point}" for point in weak_points)
    else:
        lines.append("【本轮出题针对的活跃薄弱点】（无：用户还没有沉淀弱点，本判据按 unknown 处理）")

    chapters = inputs.get("chapters") or []
    lines.append("【本轮出题覆盖的考纲章节】" + ("、".join(chapters) if chapters else "（无：本轮没走知识库考纲出题）"))

    questions = inputs.get("questions") or []
    lines.append("【本轮题目】")
    if questions:
        for index, question in enumerate(questions, start=1):
            stem = str(question.get("stem") or "")[:80]
            focus = str(question.get("focus_area") or "未标注")
            lines.append(f"{index}. {stem}｜考点标注：{focus}")
    else:
        lines.append("（无题目信息）")

    recent = inputs.get("recent_stems") or []
    if recent:
        lines.append("【用户最近练过的题干（比对重复）】")
        lines.extend(f"- {str(stem)[:60]}" for stem in recent)
    else:
        lines.append("【用户最近练过的题干】（无：首次练习，查重判据按 unknown 处理）")

    score = inputs.get("score")
    lines.append(f"【评测结果】得分 {score if score is not None else '?'}/100")
    lines.append(_INSTRUCTIONS)
    return "\n".join(lines)


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _verdict(section: dict[str, Any], allowed: set[str]) -> str:
    value = str(section.get("verdict") or "").strip().lower()
    return value if value in allowed else "unknown"


def _str_list(value: Any) -> list[str]:
    return [str(item).strip() for item in ensure_list(value) if str(item).strip()]


def _normalize(data: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
    """模型结论 → 白名单收敛 + overall 推导（口径见模块注释）。"""
    weak_points = inputs.get("weak_points") or []
    coverage_section = _section(data, "weak_point_coverage")
    coverage_verdict = _verdict(coverage_section, _COVERAGE_VERDICTS)
    covered = _str_list(coverage_section.get("covered"))
    missed = _str_list(coverage_section.get("missed"))
    if not weak_points:
        # 没有弱点就谈不上覆盖：模型给什么都收成 unknown（防幻觉）
        coverage_verdict, covered, missed = "unknown", [], []

    chapters = inputs.get("chapters") or []
    rotation_verdict = _verdict(_section(data, "chapter_rotation"), _ROTATION_VERDICTS)
    if not chapters:
        rotation_verdict = "unknown"

    duplication_verdict = _verdict(_section(data, "duplication"), _DUPLICATION_VERDICTS)
    if not inputs.get("recent_stems"):
        # 没有历史题干就无从查重：与提示词里的口径保持一致（护栏不能只写在提示词里）
        duplication_verdict = "unknown"

    difficulty_section = _section(data, "difficulty")
    difficulty_verdict = _verdict(difficulty_section, _DIFFICULTY_VERDICTS)
    if inputs.get("score") is None:
        difficulty_verdict = "unknown"

    verdicts = (coverage_verdict, rotation_verdict, duplication_verdict, difficulty_verdict)
    if set(verdicts) & _BAD_VERDICTS:
        overall = "warn"
    elif not (set(verdicts) - {"unknown"}):
        # 四个判据全无证据（新用户零信息）-> 不假装通过，"不知道"就说不知道
        overall = "unknown"
    else:
        overall = "pass"

    return {
        "weak_point_coverage": {
            "verdict": coverage_verdict,
            "covered": covered,
            "missed": missed,
        },
        "chapter_rotation": {
            "verdict": rotation_verdict,
            "chapters": _str_list(_section(data, "chapter_rotation").get("chapters")),
        },
        "duplication": {
            "verdict": duplication_verdict,
            "duplicates": _str_list(_section(data, "duplication").get("duplicates")),
        },
        "difficulty": {
            "verdict": difficulty_verdict,
            "evidence": str(difficulty_section.get("evidence") or "").strip(),
        },
        "overall": overall,
        "suggestions": _str_list(data.get("suggestions"))[:3],
        # 证据快照：结论必须能对着输入复核，否则就是一句空话
        "evidence": {
            "score": inputs.get("score"),
            "question_count": len(inputs.get("questions") or []),
            "chapters": list(chapters),
            "weak_point_count": len(weak_points),
            "recent_stem_count": len(inputs.get("recent_stems") or []),
        },
    }
