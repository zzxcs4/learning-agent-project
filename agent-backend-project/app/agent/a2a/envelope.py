"""A2A 消息信封：统一专家 Agent 之间的输出格式。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class A2AEnvelope(BaseModel):
    """A2A 通信信封：只保留最小必要字段，避免过度设计。"""

    from_agent: str = Field(description="消息来源 Agent 名称")
    payload_type: str = Field(description="业务载荷类型，如 weak_points")
    payload: dict[str, Any] = Field(default_factory=dict, description="业务数据")
    next_agent: str | None = Field(default=None, description="建议下一个专家 Agent")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="输出可信度")
