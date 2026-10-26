"""A2A 接力调度器：根据信封决定是否接力给下一个专家 Agent。"""

from app.agent.a2a.envelope import A2AEnvelope

# 置信度阈值：低于此值不接力，防止低质量输出污染下游
CONFIDENCE_THRESHOLD = 0.6

# 接力白名单：明确规定"谁允许推给谁"，防止专家 Agent 越权
#
# 第 2 期新增 `learning_partner`（supervisor）一行：这正是 §7.1 路线 B′ 的落点——
# **只新增委派关系，不动既有角色的身份与提示词**。
# `learning_evaluator` 那一行是第 1 期就有的，一个字不改（零回归）。
HANDOFF_RULES: dict[str, tuple[str, ...]] = {
    "learning_evaluator": ("difficulty_learner", "note_archiver"),
    "learning_partner": (
        "quiz_generate_workflow",
        "learning_evaluator",
        "difficulty_learner",
        "note_archiver",
    ),
}

def decide_next_agent(envelope: A2AEnvelope) -> str | None:
    """根据信封决策是否接力：白名单 + 置信度 + 建议三重校验。"""
    # 规则 1：置信度熔断
    if envelope.confidence < CONFIDENCE_THRESHOLD:
        return None
    # 规则 2：上游没有建议就不接力
    if not envelope.next_agent:
        return None
    # 规则 3：白名单校验，防止越权
    allowed = HANDOFF_RULES.get(envelope.from_agent, ())
    if envelope.next_agent not in allowed:
        return None
    return envelope.next_agent
