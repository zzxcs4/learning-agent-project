
"""记忆服务：封装短期会话记忆、工作记忆和长期记忆读取。"""

import json
import threading

from langchain_core.chat_history import InMemoryChatMessageHistory as ChatMessageHistory
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging_config import main_logger
from app.core.redis_client import RedisClient
from app.core.settings import settings
from app.db.models import ChatMessage
from app.services.long_term_memory import LongTermMemory


MESSAGES_TO_KEEP_IN_RAW = 6      # 工作记忆中保留的最近消息数量
MAX_SUMMARY_CHARS = 500          # 单次摘要最大字符长度，避免越合并越长
MAX_HISTORY_CHARS = 8000         # 最终进入 Prompt 的短期历史字符预算


def quick_summarize(messages_to_summarize: list[dict], old_summary: str) -> str:
    """把旧轮次合并进现有摘要。"""
    from app.agent.llm import get_chat_model

    new_text = "\n".join(
        f"{'用户' if m.get('role') == 'user' else 'AI'}：{m.get('content', '')}"
        for m in messages_to_summarize
    )

    prompt = (
        f"旧摘要：{old_summary or '无'}\n"
        f"待总结的旧对话：\n{new_text}\n"
        f"请合并为一句 {MAX_SUMMARY_CHARS} 字以内的中文摘要，"
        "保留任务主题和关键信息，只输出摘要文本。"
    )
    print(f"[工作记忆] 触发滚动摘要：待摘要消息数={len(messages_to_summarize)}，旧摘要长度={len(old_summary or '')}")
    message = get_chat_model(provider="deepseek").invoke(prompt)
    summary = str(message.content).strip()
    print(f"[工作记忆] 滚动摘要完成：新摘要长度={len(summary)},{summary}")
    return summary


def clip_messages_by_chars(
    messages: list[BaseMessage],
    max_chars: int = MAX_HISTORY_CHARS,
) -> list[BaseMessage]:
    """最终 Prompt 级裁剪：从最老开始成对裁剪历史消息，保证 user/assistant 不错位。"""
    total = sum(len(getattr(m, "content", "") or "") for m in messages)
    clipped = list(messages)
    while total > max_chars and len(clipped) >= 2:
        dropped = clipped[:2]
        clipped = clipped[2:]
        total -= sum(len(getattr(m, "content", "") or "") for m in dropped)
    if len(clipped) != len(messages):
        print(f"[短期记忆] 上下文裁剪：原消息数={len(messages)}，保留消息数={len(clipped)}，字符预算={max_chars}")
    return clipped


def build_chat_history_from_rows(rows: list[ChatMessage]) -> ChatMessageHistory:
    """把数据库问答记录转换成模型可读取的历史消息。"""
    history = ChatMessageHistory()
    for row in rows:
        if row.request_text:
            history.add_user_message(row.request_text)
        if row.response_text:
            history.add_ai_message(row.response_text)
    return history


class InstantMemory:
    """短期记忆：从 MySQL 读取当前会话最近问答，并转换为 LangChain 消息列表。"""

    def __init__(self, db: Session):
        self.db = db

    def load(self, user_id: int, session_id: int, before_message_id: int) -> list[BaseMessage]:
        """
        加载当前会话短期记忆。

        当前项目的 ChatMessage 是“一行一轮问答”，所以这里先读取最近 N 轮，
        再复用 build_chat_history_from_rows() 拆成 user/assistant 消息。
        """
        rows_desc = list(
            self.db.scalars(
                select(ChatMessage)
                .where(
                    ChatMessage.user_id == user_id,
                    ChatMessage.session_id == session_id,
                    ChatMessage.id < before_message_id,
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(8)
            )
        )

        rows = self._fit_char_budget(rows_desc)
        history = build_chat_history_from_rows(rows)
        return history.messages

    def _fit_char_budget(self, rows_desc: list[ChatMessage]) -> list[ChatMessage]:
        """数据库候选集级裁剪：在字符预算内保留尽可能新的历史轮次，并按时间升序返回。"""
        selected: list[ChatMessage] = []
        total_chars = 0
        skipped_count = 0

        for row in rows_desc:
            row_chars = len(row.request_text or "") + len(row.response_text or "")
            # 即使最新一轮超长，也不能强行塞进 Prompt，否则字符预算会失效。
            if total_chars + row_chars > settings.instant_memory_max_chars:
                skipped_count += 1
                continue
            selected.append(row)
            total_chars += row_chars

        selected.reverse()
        main_logger.debug(
            "短期记忆加载完成：selected_rounds={}, skipped_rounds={}, chars={}/{}",
            len(selected),
            skipped_count,
            total_chars,
            settings.instant_memory_max_chars,
        )
        return selected


class WorkingMemory:
    """工作记忆：Redis 保存最近 6 轮原文 + 滚动摘要，支持任务恢复并压缩 Token。"""

    def __init__(self, redis_client: RedisClient, ttl: int):
        self.redis = redis_client
        self.ttl = ttl

    def _key(self, user_id: int, session_id: int) -> str:
        return f"memory:working:{user_id}:{session_id}"

    def get(self, user_id: int, session_id: int) -> dict:
        """读取工作记忆；Redis 不可用或数据异常时返回空结构。"""
        key = self._key(user_id, session_id)
        data = self.redis.hgetall(key)
        if not data:
            print(f"[工作记忆] Redis 未命中：key={key}")
            return {"raw": [], "summary": ""}
        self.redis.expire(key, self.ttl)
        raw = self._decode_raw(data.get("raw", "[]"))
        summary = data.get("summary", "")
        print(f"[工作记忆] Redis 命中：key={key}，原文消息数={len(raw)}，摘要长度={len(summary)}")
        return {
            "raw": raw,
            "summary": summary,
        }

    def append_turn(
        self,
        user_id: int,
        session_id: int,
        user_msg: str,
        ai_msg: str,
    ) -> None:
        """
        追加一轮对话。

        当 raw 内容超出 MAX_HISTORY_CHARS 时，
        会把旧消息进行总结，并只保留最近的 MESSAGES_TO_KEEP_IN_RAW 条消息。
        """
        key = self._key(user_id, session_id)
        data = self.redis.hgetall(key)
        raw = self._decode_raw(data.get("raw", "[]"))
        old_summary = data.get("summary", "")

        raw.append({"role": "user", "content": user_msg})
        raw.append({"role": "assistant", "content": ai_msg})

        total_chars = sum(len(m.get("content", "") or "") for m in raw)

        if total_chars > MAX_HISTORY_CHARS and len(raw) > MESSAGES_TO_KEEP_IN_RAW:
            messages_to_summarize = raw[:-MESSAGES_TO_KEEP_IN_RAW]
            messages_to_keep = raw[-MESSAGES_TO_KEEP_IN_RAW:]

            new_summary = quick_summarize(messages_to_summarize, old_summary)

            self.redis.hset(
                key,
                {
                    "raw": json.dumps(messages_to_keep, ensure_ascii=False),
                    "summary": new_summary,
                },
            )
            print(f"[工作记忆] 已写入 Redis：触发摘要，保留消息数={len(messages_to_keep),}，key={key}")
        else:
            self.redis.hset(
                key,
                {
                    "raw": json.dumps(raw, ensure_ascii=False),
                    "summary": old_summary,
                },
            )
            print(f"[工作记忆] 已写入 Redis：原文消息数={len(raw)}，总字符数={total_chars}，key={key}")

        self.redis.expire(key, self.ttl)

    def clear(self, user_id: int, session_id: int) -> None:
        """任务完成或切换话题时主动清理工作记忆。"""
        self.redis.delete(self._key(user_id, session_id))

    @staticmethod
    def _decode_raw(raw_text: str) -> list[dict]:
        try:
            # 将原始JSON字符串解析为字典列表
            raw = json.loads(raw_text or "[]")
        except json.JSONDecodeError:
            return []
        return raw if isinstance(raw, list) else []


class MemoryService:
    """
    统一记忆服务入口。

    当前启用短期记忆、工作记忆和长期记忆。
    """

    def __init__(self, db: Session):
        self.instant = InstantMemory(db)
        self.working = WorkingMemory(RedisClient(), settings.working_memory_ttl_seconds)
        self.long_term = LongTermMemory(db)

    def load_context(
        self,
        user_id: int,
        session_id: int,
        before_message_id: int,
        session_model: int | None = None,
        query: str = "",
        agent_role: str = "general_assistant",
        enable_long_term: bool | None = None,
        profile_limit: int = 6,
    ) -> dict:
        """
        加载本轮对话需要的记忆上下文。

        集成策略：优先从 Redis 工作记忆取 `raw`，若为空（新会话/已过期/Redis异常），
        则回退到从 MySQL 短期记忆加载；长期记忆按需作为 SystemMessage 注入历史消息。
        """
        working_memory = self.working.get(user_id, session_id)
        working_raw = working_memory.get("raw", [])
        working_summary = working_memory.get("summary", "")

        if working_raw:
            # Redis 工作记忆中有数据，优先使用；若存在滚动摘要，也一并注入上下文。
            history_messages = []
            if working_summary:
                history_messages.append(SystemMessage(content=f"以下是较早对话摘要：{working_summary}"))
            history_messages.extend(
                HumanMessage(content=m.get("content", ""))
                if m.get("role") == "user"
                else AIMessage(content=m.get("content", ""))
                for m in working_raw
            )
            print(f"[记忆服务] 使用 Redis 工作记忆：原文消息数={len(working_raw)}，有摘要={bool(working_summary)}")
        else:
            # Redis 为空，回退到 MySQL 短期记忆作为冷启动/降级方案。
            mysql_history = self.instant.load(user_id, session_id, before_message_id)
            # instant.load() 已按数据库候选预算筛过一层；这里再按最终 Prompt 预算做兜底裁剪。
            history_messages = clip_messages_by_chars(mysql_history)
            print(f"[记忆服务] Redis 为空，使用数据库短期记忆：消息数={len(history_messages)}")

        long_term_context = self.long_term.load_memory_context(
            user_id=user_id,
            session_model=session_model,
            query=query,
            enable_long_term=enable_long_term,
            limit=profile_limit,
        )
        if long_term_context["memory_text"]:
            history_messages = [SystemMessage(content=long_term_context["memory_text"]), *history_messages]

        return {
            "history_messages": history_messages,
        }
