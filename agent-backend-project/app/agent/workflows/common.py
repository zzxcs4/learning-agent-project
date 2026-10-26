"""工作流通用工具函数。"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.llm import get_chat_model


def extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出中提取 JSON 对象。"""
    content = (text or "").strip()
    if not content:
        raise ValueError("模型没有返回内容")

    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", content)
        if not match:
            raise
        return json.loads(match.group(0))


def invoke_json(llm: Any, prompt: str, provider: str = "deepseek") -> dict[str, Any]:
    """调用模型并解析 JSON 响应。"""
    model = llm or get_chat_model(provider=provider)
    response = model.invoke(prompt)
    content = getattr(response, "content", response)
    if isinstance(content, list):
        content = "".join(str(item) for item in content)
    return extract_json_object(str(content))


def ensure_list(value: Any) -> list[Any]:
    """把模型可能返回的空值或非列表值收敛成列表。"""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]
