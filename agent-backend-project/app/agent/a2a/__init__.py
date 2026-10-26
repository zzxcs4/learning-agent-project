"""A2A 协议包：定义专家 Agent 之间的通信契约与接力调度。"""

from app.agent.a2a.dispatcher import decide_next_agent
from app.agent.a2a.envelope import A2AEnvelope

__all__ = ["A2AEnvelope", "decide_next_agent"]
