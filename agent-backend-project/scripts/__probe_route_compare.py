"""临时探针：两边 agent 的路由对照，确认出题侧零回归。用完即删。"""

from __future__ import annotations

import os

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.services.orchestrator_service import OrchestratorService  # noqa: E402

orch = OrchestratorService.__new__(OrchestratorService)

CASES = [
    # (agent_name, request_text, 期望)
    ("difficulty_learner", "大模型参数的演化", "workflow"),
    ("difficulty_learner", "什么是向量数据库", "workflow"),
    ("difficulty_learner", "帮我讲讲 GIL", "workflow"),
    # 出题侧必须零回归：没出题意图时仍然走普通聊天
    ("quiz_generate_workflow", "什么是向量数据库", "chat"),
    ("quiz_generate_workflow", "帮我出题", "workflow"),
    ("quiz_generate_workflow", "帮我生成一套学习测评", "workflow"),
    ("quiz_generate_workflow", "大模型参数的演化", "chat"),
    # 未指定角色
    (None, "随便聊聊", "chat"),
]

print(f"{'agent_name':<24} {'request_text':<18} {'route_type':<10} {'期望':<10} 结果")
print("-" * 104)
all_ok = True
for agent, text, expect in CASES:
    r = orch.route(agent, text)
    ok = r["route_type"] == expect
    all_ok = all_ok and ok
    print(f"{str(agent):<24} {text:<18} {str(r['route_type']):<10} {expect:<10} {'OK' if ok else 'FAIL'}  {r['reason']}")

print()
print("全部符合预期" if all_ok else "有不符合预期的用例")
raise SystemExit(0 if all_ok else 1)
