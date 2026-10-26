"""出题参数卡：把「知识库 / 每轮题目数 / 发散度 / 难度」收敛成一份可渲染、可校验的规格。

为什么要有这个模块（第 23 轮，用户 2026-10-19 要求）：
出题参数原来在**发送前**的表单里选（ChatInput 的知识库下拉 + 发散度档位），用户看不见
"这些参数到底影响什么"，也不知道默认值。改成**对话中先问再出题**之后，同一份参数
需要三处一致：

1. 后端写进聊天气泡的参数卡（`status=awaiting_params` 的那条消息）；
2. 前端渲染控件（数值范围、下拉选项、五档说明都来自这里，不重复写文案）；
3. 提交后的**校验**（范围/枚举/知识库归属）。

所以规格与校验必须是**同一份数据**：字段表的 `min/max/options` 就是校验依据，
不存在"前端限制了 5–20、后端却接受 3"的漂移。

`resolved=True` 的字段=**已经有权威值**（调用方传了 / 用户只有唯一知识库），
参数卡只把它当只读信息展示，不再问；`missing` 就是还缺的那几个。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import raise_error

# 每轮题目数：范围 5–20、默认 10（用户 2026-10-19 明确的口径）
QUESTION_COUNT_MIN = 5
QUESTION_COUNT_MAX = 20
QUESTION_COUNT_DEFAULT = 10
# 发散度：1–5 五档，默认 3（均衡）
DIVERGENCE_MIN = 1
DIVERGENCE_MAX = 5
DIVERGENCE_DEFAULT = 3

# 发散度五档：文案与口径对齐用户给的说明（原项目 TechSpar 的 training.divergence）
DIVERGENCE_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "value": 1,
        "label": "聚焦薄弱",
        "description": "100% 针对存在弱点的知识域，适合考前专项突击",
    },
    {
        "value": 2,
        "label": "侧重薄弱",
        "description": "约 70% 针对薄弱点，30% 拓展至新知识点",
    },
    {
        "value": 3,
        "label": "均衡",
        "description": "薄弱环节巩固与全新知识盲区发掘各占 50%",
    },
    {
        "value": 4,
        "label": "侧重探索",
        "description": "约 30% 回顾薄弱点，70% 探索全新知识层面",
    },
    {
        "value": 5,
        "label": "全面探索",
        "description": "100% 探索未涉猎过的新知识领域，发掘潜在盲区",
    },
)

# 难度三档：控制题目的认知层级（不是"题目长短"），默认取画像目标等级
DIFFICULTY_LEVELS: tuple[str, ...] = ("入门", "进阶", "高级")
DIFFICULTY_DEFAULT = "进阶"
DIFFICULTY_OPTIONS: tuple[dict[str, Any], ...] = (
    {"value": "入门", "label": "入门", "description": "概念与记忆为主，适合第一次接触或打基础"},
    {"value": "进阶", "label": "进阶", "description": "概念辨析 + 少量变式，默认档"},
    {"value": "高级", "label": "高级", "description": "跨章综合、边界情形与易错写法辨析"},
)

# 「不使用知识库」的哨兵值（第 24 轮，用户 2026-10-20 要求"选项里要有不选知识库"）。
#
# 为什么不用 `None`：`None` 已经表示"调用方没传 = 还没定，需要问"。若"明确不选"也用 None，
# 前端提交时会被当成空值丢掉、后端又回落成默认值 → 用户选了"不用库"却照样被再问一次。
# 用 `0` 表达"明确了：本轮不归属任何库"，`None` 保持"没定"。落库/检索时两者都收敛成 NULL。
NO_KNOWLEDGE_BASE_VALUE = 0
NO_KNOWLEDGE_BASE_OPTION: dict[str, Any] = {
    "value": NO_KNOWLEDGE_BASE_VALUE,
    "label": "不使用知识库",
    "description": "只按我的要求出题，不检索你的资料",
}


def normalize_knowledge_base_id(value: Any) -> int | None:
    """把外部传入的知识库 id 收敛成"有效 id 或 None"（0/负数/空 → None）。

    检索与落库只认"有效 id"：`0` 是给参数卡/前端表达"明确不选"的哨兵，绝不能被当成
    `WHERE knowledge_base_id = 0` 去过滤（那会一条资料都搜不到）。
    """
    try:
        number = int(value) if value is not None and str(value).strip() != "" else 0
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


# 画像 `user_profiles.target_level` → 难度档。抽不到就按"进阶"。
_LEVEL_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("初级", "入门", "新手", "实习", "校招"), "入门"),
    (("中级", "进阶", "熟练"), "进阶"),
    (("高级", "资深", "专家", "架构"), "高级"),
)


def normalize_difficulty(value: Any, *, fallback: str = DIFFICULTY_DEFAULT) -> str:
    """把外部传入的难度收敛到三档之一；不认识的值按 fallback。"""
    text = str(value or "").strip()
    if text in DIFFICULTY_LEVELS:
        return text
    for keywords, level in _LEVEL_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return level
    return fallback


def difficulty_from_profile(db: Session | None, user_id: int | None) -> str:
    """难度默认值：优先用画像里的目标等级（`target_level`），取不到按"进阶"。"""
    if db is None or not user_id:
        return DIFFICULTY_DEFAULT
    try:
        from app.db.models import UserProfile

        row = db.scalar(select(UserProfile).where(UserProfile.user_id == int(user_id)))
        level = str(getattr(row, "target_level", "") or "")
    except Exception:  # noqa: BLE001 - 画像读不到不该让出题失败
        return DIFFICULTY_DEFAULT
    return normalize_difficulty(level)


def _knowledge_base_options(db: Session, user_id: int) -> list[dict[str, Any]]:
    """知识库候选（名称 + 文档数），只取自己的库；**第一项恒是"不使用知识库"**。

    复用 `KnowledgeBaseService.list_knowledge_bases`：文档数口径（哪些资料算"在这个库里"）
    与知识库页完全一致，避免参数卡显示的数字和列表页对不上。
    """
    from app.services.knowledge_base_service import KnowledgeBaseService

    items = KnowledgeBaseService.list_knowledge_bases(db, user_id=user_id)
    options = [dict(NO_KNOWLEDGE_BASE_OPTION)]
    options.extend(
        {
            "value": int(item["id"]),
            "label": str(item.get("name") or ""),
            "resource_count": int(item.get("resource_count") or 0),
        }
        for item in items
    )
    return options


def build_params_spec(
    db: Session,
    *,
    user_id: int,
    question_count: int | None,
    knowledge_base_id: int | None,
    divergence: int | None,
    difficulty: str | None,
) -> dict[str, Any]:
    """生成参数卡规格：四个字段 + `missing`（还需要用户选的那些）。

    `resolved` 的判定就是"只问缺的"的落地：
    - 调用方给了值 → 已定，不再问；传 `0` = 用户明确选了"不使用知识库"（同样算已定）；
    - 知识库：用户只有 1 个库时自动用它（没得选，且带上考纲出题质量更好）；一个库都没有
      时也不是"缺"（没得可问），只是本轮练习不归属任何库。
    """
    options = _knowledge_base_options(db, user_id)
    kb_resolved = True
    kb_note = ""
    kb_value: int | None = knowledge_base_id
    if normalize_knowledge_base_id(knowledge_base_id) is not None:
        kb_note = "已按你的选择指定"
    elif knowledge_base_id == NO_KNOWLEDGE_BASE_VALUE:
        kb_value = NO_KNOWLEDGE_BASE_VALUE
        kb_note = "已按你的选择：本轮不使用知识库"
    elif len(options) == 2:  # 恒有一项"不使用知识库"，所以"只剩一个真库"时长度是 2
        kb_value = int(options[1]["value"])
        kb_note = f"你只有一个知识库，已自动使用「{options[1]['label']}」"
    elif len(options) == 1:
        kb_note = "你还没有知识库，本轮只按你的要求出题"
    else:
        kb_resolved = False

    profile_difficulty = difficulty_from_profile(db, user_id)
    fields: list[dict[str, Any]] = [
        {
            "key": "knowledge_base_id",
            "label": "知识库",
            "type": "select",
            "value": kb_value,
            "default": kb_value,
            "resolved": kb_resolved,
            "note": kb_note,
            "options": options,
        },
        {
            "key": "question_count",
            "label": "每轮题目数",
            "type": "number",
            "value": question_count,
            "default": QUESTION_COUNT_DEFAULT,
            "min": QUESTION_COUNT_MIN,
            "max": QUESTION_COUNT_MAX,
            "resolved": question_count is not None,
            "note": "已按你的要求指定" if question_count is not None else "",
        },
        {
            "key": "divergence",
            "label": "题目发散度",
            "type": "radio",
            "value": divergence,
            "default": DIVERGENCE_DEFAULT,
            "min": DIVERGENCE_MIN,
            "max": DIVERGENCE_MAX,
            "resolved": divergence is not None,
            "note": "已按你的要求指定" if divergence is not None else "",
            "options": [dict(item) for item in DIVERGENCE_OPTIONS],
        },
        {
            "key": "difficulty",
            "label": "难度",
            "type": "radio",
            "value": difficulty or profile_difficulty,
            "default": profile_difficulty,
            "resolved": bool(difficulty),
            "note": "已按你的要求指定" if difficulty else f"默认按你的目标等级「{profile_difficulty}」",
            "options": [dict(item) for item in DIFFICULTY_OPTIONS],
        },
    ]
    missing = [field["key"] for field in fields if not field["resolved"]]
    return {"fields": fields, "missing": missing}


def resolve_submitted_params(
    db: Session,
    *,
    user_id: int,
    spec: dict[str, Any],
    submitted: dict[str, Any],
) -> dict[str, Any]:
    """校验参数卡提交值，缺失的字段回落到规格里的 `default`。

    校验依据直接取自规格（同一份 min/max/options），非法值一律 400。

    **「没提供」的两种形状必须等价**：键不存在、或值为 `None`。调用方（服务层把四个 key
    都摆进提交体、HTTP 模型把没传的字段置 None）会自然产生后者；若只认"键不存在"，
    一个 `knowledge_base_id=None` 就会把**已经定好的知识库**（唯一库自动使用 / 本会话挂载）
    清成 NULL——第 23 轮的"唯一库自动使用"确认后丢库就是这个坑。
    """
    resolved: dict[str, Any] = {}
    for field in spec.get("fields") or []:
        key = str(field.get("key") or "")
        raw = submitted.get(key)
        if raw is None:
            raw = field.get("default")
        if key == "knowledge_base_id":
            value = _as_int(raw)
            # `0` = 用户明确选了"不使用知识库"：落库要 NULL（不是 0），也不要去做归属校验
            if value == NO_KNOWLEDGE_BASE_VALUE:
                resolved[key] = None
                continue
            if value is not None:
                # 归属校验：不能把别人的库挂到自己的练习上
                from app.services.knowledge_base_service import KnowledgeBaseService

                KnowledgeBaseService.get_or_404(db, user_id=user_id, knowledge_base_id=value)
            resolved[key] = value
            continue
        if key == "question_count":
            resolved[key] = _bounded_int(
                raw,
                minimum=QUESTION_COUNT_MIN,
                maximum=QUESTION_COUNT_MAX,
                label="每轮题目数",
                default=QUESTION_COUNT_DEFAULT,
            )
            continue
        if key == "divergence":
            resolved[key] = _bounded_int(
                raw,
                minimum=DIVERGENCE_MIN,
                maximum=DIVERGENCE_MAX,
                label="发散度",
                default=DIVERGENCE_DEFAULT,
            )
            continue
        if key == "difficulty":
            resolved[key] = normalize_difficulty(raw, fallback=str(field.get("default") or DIFFICULTY_DEFAULT))
    return resolved


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() != "" else None
    except (TypeError, ValueError):
        raise_error(code=400, message="知识库ID必须是整数")


def _bounded_int(raw: Any, *, minimum: int, maximum: int, label: str, default: int) -> int:
    try:
        value = int(raw) if raw is not None and str(raw).strip() != "" else default
    except (TypeError, ValueError):
        raise_error(code=400, message=f"{label}必须是整数")
    if value < minimum or value > maximum:
        raise_error(code=400, message=f"{label}必须在 {minimum}–{maximum} 之间")
    return value
