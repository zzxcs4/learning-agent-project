"""模型工厂：统一创建聊天模型实例。"""

from collections.abc import Callable
from typing import Any

from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI

from app.core.settings import settings


# provider -> ChatOpenAI 构造函数。
LlmFactory = Callable[[], ChatOpenAI]

# 第三方 OpenAI 兼容 relay 把"思考内容"放在这个非标准字段里（官方 OpenAI 没有）。
# 名字与 `app/agent/learning_partner.py::_chunk_reasoning` 必须一致。
REASONING_FIELD = "reasoning_content"


def _extract_reasoning_delta(chunk: Any) -> str:
    """从**原始** SSE chunk 里取出这一段的推理文本（没有就返回空串）。"""
    if not isinstance(chunk, dict):
        return ""
    choices = chunk.get("choices") or chunk.get("chunk", {}).get("choices") or []
    if not choices:
        return ""
    delta = choices[0].get("delta") or {}
    value = delta.get(REASONING_FIELD)
    return value if isinstance(value, str) else ""


class ReasoningAwareChatOpenAI(ChatOpenAI):
    """把 relay 的 `reasoning_content` 增量**保留**到 `additional_kwargs`。

    为什么必须自己补：`ChatOpenAI` 只面向官方 OpenAI 规范，它的模块 docstring 明确
    写着非标准字段（`reasoning_content` / `reasoning_details`）"**not** extracted or
    preserved"，并建议改用它家 provider-specific 包（`ChatDeepSeek` / `ChatOpenRouter`）。
    但这里的 relay 是 OpenAI 兼容的思考型模型，而 `langchain-deepseek` 并没有装，
    所以用最小覆写补上——**只加一个字段，不改任何既有语义**。

    代价（很轻）：这个字段只在我们自己的循环里被读一次（`_stream_model_step` 逐 chunk
    取用），不参与 `content`、不参与 `tool_calls`、也不回传给模型。实测 relay 的
    `reasoning_content` 是碎片的（一句话回答有 55 帧），所以整段推理只存在于前端的
    临时状态里，**不落库**（见 `learning_partner.stream()`）。

    为什么挂在 `_convert_chunk_to_generation_chunk`：它是**方法**（可覆写），而且拿到的
    是**原始 chunk dict**（上游 `chunk.model_dump()` 保留了 SDK 的 extra 字段），
    所以在转换结果上补一刀即可，不必碰 `_stream` / `_astream` 那两个长实现。
    """

    def _convert_chunk_to_generation_chunk(
        self,
        chunk: dict,
        default_chunk_class: type,
        base_generation_info: dict | None,
    ) -> ChatGenerationChunk | None:
        generation_chunk = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        reasoning = _extract_reasoning_delta(chunk)
        if generation_chunk is not None and reasoning:
            kwargs = generation_chunk.message.additional_kwargs
            # 每个 chunk 只带自己那一片；累加与否无所谓（消费方只按片读），
            # 但写成累加可以让"万一有人去看累积消息"时也是完整的。
            kwargs[REASONING_FIELD] = kwargs.get(REASONING_FIELD, "") + reasoning
        return generation_chunk


def _build_deepseek_chat() -> ChatOpenAI:
    """创建 DeepSeek(OpenAI兼容) 聊天模型。"""
    if not settings.deepseek_api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")
    return ReasoningAwareChatOpenAI(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        temperature=settings.deepseek_temperature,
        max_tokens=settings.deepseek_max_tokens,
        streaming=True,
        # 第 3 期可观测：流式也要回传 usage（否则 on_llm_end 的聚合结果里没有 token 数，
        # 用量统计永远是空）。实测本 relay 支持并返回 input/output/total（含 reasoning 明细）；
        # 若换到不支持的中转，拿不到 usage 时统计显示为空值，loop 行为不受影响。
        stream_usage=True,
    )


# 扩展口：后续新增 provider 只需在这里注册。
_PROVIDER_FACTORIES: dict[str, LlmFactory] = {
    "deepseek": _build_deepseek_chat,
}


def get_chat_model(provider: str = "deepseek") -> ChatOpenAI:
    """按 provider 返回聊天模型实例。"""
    factory = _PROVIDER_FACTORIES.get(provider)
    if not factory:
        raise ValueError(f"Unsupported llm provider: {provider}")
    return factory()
