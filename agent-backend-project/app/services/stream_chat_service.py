"""轻量单Agent流式对话服务。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Generator
from typing import Any

from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessageChunk
from sqlalchemy.orm import Session

from app.agent.prompt_layer import (
    build_personalized_prompt,
    build_rag_system_prompt,
    build_stream_chain_context,
    format_retrieved_chunks,
)
from app.core.logging_config import main_logger
from app.db.models import ChatMessage
from app.rag import search_similar_chunks, should_retrieve
from app.services.long_term_memory import LongTermMemory
from app.services.memory_service import MemoryService
from app.services.session_service import SessionService
from app.utils.prompt_logger import log_chain_ready


def _sse_data(payload: dict[str, Any]) -> str:
    """将事件对象编码为一帧 SSE 文本。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StreamChatService:
    """负责流式推送、历史重建与最终落库。"""

    @staticmethod
    def stream_chat(
        db: Session,
        user_id: int,
        session_id: int,
        session_model: int,
        request_text: str,
        select_model: int = 0,
        provider: str = "deepseek",
        agent_name: str | None = None,
        scene: str | None = None,
    ) -> StreamingResponse:
        # 1) 会话归属校验，不存在则抛 404。
        SessionService.get_session_or_404(
            db=db,
            user_id=user_id,
            session_id=session_id,
            session_model=session_model,
        )

        # 2) 预写入消息占位行：先落库拿到 row.id，供历史记忆截断用。
        row = ChatMessage(
            user_id=user_id,
            session_id=session_id,
            select_model=select_model,
            request_id=uuid.uuid4().hex,
            request_text=request_text,
            response_text="",
            request_segments=[],
            response_segments=[],
            response_payload=None,
            status=0,
            file_extracted_text="",
        )
        db.add(row)
        db.commit()
        db.refresh(row)

        # 3) 构建个性化提示词：有 agent_name 才查模板；否则使用通用兜底，不拼接数据库模板。
        if agent_name and scene:
            system_prompt = build_personalized_prompt(
                db=db,
                user_id=user_id,
                agent_name=agent_name,
                scene=scene,
            )
        else:
            system_prompt = None

        retrieved_chunks = []
        retrieved_text = ""
        # 3.5) RAG 检索：命中知识问询意图才触发向量检索，否则直接走兜底提示
        # 优先判断是否为画像写入意图，若是则跳过 RAG，避免意图冲突
        if LongTermMemory.should_write(request_text, user_id):
            print(f"[聊天服务] 命中画像写入意图，跳过 RAG 检索：用户ID={user_id}")
        elif should_retrieve(request_text):
            retrieved_chunks = search_similar_chunks(
                query=request_text,
                user_id=user_id,
                db=db,
                top_k=3,
            )

            retrieved_text = format_retrieved_chunks(retrieved_chunks)
            system_prompt = build_rag_system_prompt(system_prompt, retrieved_text)
            print(f"[聊天服务] 已执行 RAG 检索：命中片段数={len(retrieved_chunks)}")
        else:
            print("[聊天服务] 跳过 RAG 检索：未命中检索意图")

        log_chain_ready(user_id, session_id, agent_name, scene, provider, len(system_prompt) if system_prompt else 0)

        # 4) 组装链路上下文：历史记忆截断 + LLM 绑定 + 输入变量准备。
        chain_with_memory, stream_inputs = build_stream_chain_context(
            db=db,
            user_id=user_id,
            session_id=session_id,
            current_message_id=row.id,
            provider=provider,
            request_text=request_text,
            system_prompt=system_prompt,
            rag_context=retrieved_text,
            session_model=session_model,
        )

        def event_generator() -> Generator[str, None, None]:
            response_text = ""
            error_message = ""
            sources = [
                {
                    "chunk_id": c.get("chunk_id"),
                    "resource_id": c.get("resource_id"),
                    "chunk_index": c.get("chunk_index"),
                    "file_name": c.get("file_name", ""),
                    "score": round(c.get("score", 0), 4),
                    "text": c.get("text", ""),
                }
                for c in retrieved_chunks
            ]
            source_refs = [
                {
                    "chunk_id": c.get("chunk_id"),
                    "score": round(c.get("score", 0), 4),
                }
                for c in retrieved_chunks
                if c.get("chunk_id")
            ]
            yield _sse_data({"type": "start", "message_id": row.id})
            try:
                # [1.x 新写法] 直接 stream，历史已在 inputs 中，无需 config 传 session_id。
                for chunk in chain_with_memory.stream(stream_inputs):
                    # [0.3.x 旧写法] config 用于让 RunnableWithMessageHistory 识别会话。
                    # for chunk in chain_with_memory.stream(
                    #     stream_inputs,
                    #     config={"configurable": {"session_id": str(session_id)}},
                    # ):
                    delta = StreamChatService._extract_text_delta(chunk)
                    if not delta:
                        continue
                    response_text += delta
                    yield _sse_data({"type": "delta", "text": delta})
            except Exception as exc:
                error_message = str(exc)
                main_logger.exception("stream_chat failed", exc_info=exc)
            finally:
                # 无论成功或异常，都将已积累的文本回写到占位行。
                row.response_text = response_text
                row.reference_sources = source_refs
                db.add(row)
                db.commit()
                if response_text and not error_message:
                    MemoryService(db).working.append_turn(
                        user_id=user_id,
                        session_id=session_id,
                        user_msg=request_text,
                        ai_msg=response_text,
                    )
                    profile_context = StreamChatService._build_profile_update_context(
                        stream_inputs.get("history", []),
                    )
                    # 简单异步写入：命中画像写入意图时，后台线程更新 UserProfile。
                    LongTermMemory.submit_profile_update_async(user_id, request_text, profile_context)

            if error_message:
                yield _sse_data({"type": "error", "message": error_message})
            yield _sse_data({"type": "done", "response_text": response_text, "sources": sources})

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @staticmethod
    def _extract_text_delta(chunk: Any) -> str:
        """从 LangChain 链式调用返回的增量块中提取文本。"""
        if isinstance(chunk, AIMessageChunk):
            return chunk.content or ""
        return ""

    @staticmethod
    def _build_profile_update_context(history_messages: list[Any]) -> str:
        """为画像抽取提供用户上下文，只要提取用户的问题。"""
        lines: list[str] = []
        for message in history_messages[-4:]:
            if getattr(message, "type", "") != "human":
                continue

            content = str(getattr(message, "content", "")).strip()
            if content:
                lines.append(f"用户：{content[:300]}")

        return "\n".join(lines)
