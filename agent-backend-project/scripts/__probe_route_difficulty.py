"""临时探针：实测 route() 对用户原话的决策。用完即删。"""

from __future__ import annotations

import os

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.services.orchestrator_service import (  # noqa: E402
    DIFFICULTY_WORKFLOW_TYPE,
    WORKFLOW_ROUTE_RULES,
    OrchestratorService,
)
from app.utils.intent_classifier import classify_intent  # noqa: E402

CASES = [
    "大模型参数的演化",              # 用户原话
    "帮我讲讲大模型参数的演化",        # 加一个关键词
    "大模型参数的演化是个难点",
    "什么是向量数据库",               # 既有"简单问题"示例
    "这道题我没弄懂，深入讲一下",       # 语义应命中
    "帮我生成一套学习测评",           # 出题侧，不该被抢
]

rule = WORKFLOW_ROUTE_RULES["difficulty_learner"]
orch = OrchestratorService.__new__(OrchestratorService)  # route 不用 db

print(f"required_keywords = {rule.required_keywords}")
print(f"threshold         = {rule.threshold}")
print()
print(f"{'输入':<28} {'关键词':<7} {'语义score':<11} {'最终 route_type':<10} reason")
print("-" * 108)
for text in CASES:
    kw = any(k in text for k in rule.required_keywords)
    m = classify_intent(
        text=text,
        examples={DIFFICULTY_WORKFLOW_TYPE: rule.intent_examples},
        threshold=rule.threshold,
        default_label="chat",
    )
    r = orch.route("difficulty_learner", text)
    print(f"{text:<28} {str(kw):<7} {m.score:<11.3f} {str(r['route_type']):<10} {r['reason']}")
