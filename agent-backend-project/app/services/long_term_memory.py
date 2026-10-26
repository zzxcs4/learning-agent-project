"""长期记忆服务：基于 UserProfile 实现。

核心流程：
1. 读取：轻量模型判断是否需要用户画像；需要时再判断用户关心哪组字段（goal/ability/preference），只拼接对应字段注入 Prompt。
2. 写入：轻量模型判断是否需要更新画像；需要时后台线程调用 DeepSeek 抽取字段并合并入库。
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


from app.db.database import SessionLocal
from app.db.models import UserProfile
from app.services.long_term_memory_corpus import (
    ALL_SECTIONS,
    LIST_FIELDS,
    MAX_LIST_ITEMS,
    MAX_SUMMARY_CHARS,
    MAX_UPDATE_CONTEXT_CHARS,
    PROFILE_JOB_PATTERN,
    PROFILE_READ_PATTERNS,
    PROFILE_UPDATE_PATTERNS,
    PROFILE_UPDATE_PROMPT,
    READ_EXAMPLES,
    SECTION_EXAMPLES,
    TEXT_FIELDS,
    WRITE_EXAMPLES,
)
from app.utils.intent_classifier import classify_intent


_executor = ThreadPoolExecutor(max_workers=4)


class LongTermMemory:
    """长期记忆：读取画像、判断读写意图、异步更新画像。"""

    def __init__(self, db: Session):
        self.db = db

    def load_memory_context(
        self,
        user_id: int,
        session_model: int | None = None,
        query: str = "",
        enable_long_term: bool | None = None,
        limit: int = 6,
    ) -> dict:
        """按需读取用户画像，并返回可注入 Prompt 的文本。"""
        del session_model, limit
        should_load = (
            enable_long_term
            if enable_long_term is not None
            else self.should_read(query, user_id)
        )
        if not should_load:
            return {"profile": None, "memory_text": ""}

        profile = self._get_profile(user_id)
        sections = self.resolve_sections(query)
        memory_text = self.build_memory_text(profile, sections)
        print(f"[长期记忆] 读取画像：用户ID={user_id}，字段范围={sorted(sections)}，注入文本长度={len(memory_text)}")
        return {"profile": profile, "memory_text": memory_text}

    @staticmethod
    def should_read(query: str, user_id: int | None = None) -> bool:
        """判断本轮是否需要读取长期画像：明确个性化诉求优先，其次使用轻量语义分类。"""
        if LongTermMemory._looks_like_profile_read(query):
            print(f"[长期记忆] 读取意图：用户ID={user_id}，结果=read_profile，来源=规则")
            return True

        result = classify_intent(query, READ_EXAMPLES, threshold=0.55, default_label="chat")
        print(f"[长期记忆] 读取意图：用户ID={user_id}，结果={result.label}，分数={result.score:.2f}")
        return result.label == "read_profile"

    @staticmethod
    def _looks_like_profile_read(query: str) -> bool:
        """规则兜底：识别明确要求结合用户画像、实际情况、薄弱点或目标的请求。"""
        text = (query or "").strip()
        if not text:
            return False
        return any(pattern.search(text) for pattern in PROFILE_READ_PATTERNS)

    @staticmethod
    def should_write(user_message: str, user_id: int | None = None) -> bool:
        """判断本轮是否需要更新长期画像：明确画像事实优先，其次使用轻量语义分类。"""
        if LongTermMemory._looks_like_profile_update(user_message):
            print(f"[长期记忆] 写入意图：用户ID={user_id}，结果=write_profile，来源=规则")
            return True

        result = classify_intent(user_message, WRITE_EXAMPLES, threshold=0.55, default_label="chat")
        print(f"[长期记忆] 写入意图：用户ID={user_id}，结果={result.label}，分数={result.score:.2f}")
        return result.label == "write_profile"

    @staticmethod
    def _looks_like_profile_update(user_message: str) -> bool:
        """规则兜底：识别用户明确陈述的岗位、经验、目标、薄弱点、技能和学习偏好。"""
        text = (user_message or "").strip()
        if not text:
            return False
        return any(pattern.search(text) for pattern in PROFILE_UPDATE_PATTERNS)

    @staticmethod
    def resolve_sections(query: str) -> set[str]:
        """判断本轮更需要哪组画像字段；不确定时读取全部。"""
        result = classify_intent(query, SECTION_EXAMPLES, threshold=0.50, default_label="all")
        return ALL_SECTIONS if result.label == "all" else {result.label}

    @staticmethod
    def submit_profile_update_async(
        user_id: int,
        user_message: str,
        conversation_context: str = "",
    ) -> None:
        """命中写入意图时，在后台线程更新 UserProfile。"""
        if not LongTermMemory.should_write(user_message, user_id):
            return
        print(f"[长期记忆] 提交画像更新任务：用户ID={user_id}，上下文长度={len(conversation_context)}")
        _executor.submit(
            LongTermMemory._update_profile_worker,
            user_id,
            user_message,
            conversation_context,
        )

    def _get_profile(self, user_id: int) -> UserProfile | None:
        """读取用户画像。"""
        return self.db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))

    @classmethod
    def build_memory_text(cls, profile: UserProfile | None, sections: set[str]) -> str:
        """把 UserProfile 转成 SystemMessage 文本。"""
        if profile is None:
            return ""

        lines: list[str] = []
        if "goal" in sections:
            cls._append_text(lines, "目标岗位", profile.target_job)
            cls._append_text(lines, "目标级别", profile.target_level)
            cls._append_text(lines, "工作经验", profile.years_experience)
            cls._append_text(lines, "学习目标", profile.learning_goal)
        if "ability" in sections:
            cls._append_list(lines, "已掌握技能", profile.target_skills)
            cls._append_list(lines, "薄弱点", profile.weak_topics)
        if "preference" in sections:
            cls._append_text(lines, "学习偏好", profile.learning_style)
        if sections == ALL_SECTIONS:
            cls._append_text(lines, "长期摘要", profile.long_term_summary)

        if not lines:
            return ""
        return "\n".join([
            "【用户长期画像】",
            *lines,
            "请仅将这些信息作为回答背景，不要主动说明你读取了长期记忆。",
        ])

    @staticmethod
    def _update_profile_worker(user_id: int, user_message: str, conversation_context: str = "") -> None:
        """后台线程：抽取画像字段并合并入库。"""
        updates = LongTermMemory._extract_updates(
            user_message, conversation_context, user_id=user_id
        )
        if not updates:
            print(f"[长期记忆] 画像更新结束：用户ID={user_id}，没有可写入字段")
            return

        db = SessionLocal()
        try:
            LongTermMemory._apply_updates(db, user_id, updates)
        except Exception as exc:
            print(f"长期画像更新失败：{exc}")
        finally:
            db.close()

    @staticmethod
    def _extract_updates(user_message: str, conversation_context: str = "", user_id: int | None = None) -> dict[str, Any]:
        """调用 DeepSeek，把用户消息抽取成画像字段字典。"""
        from app.agent.llm import get_chat_model

        context = (conversation_context or "").strip()[:MAX_UPDATE_CONTEXT_CHARS] or "无"
        try:
            response = get_chat_model(provider="deepseek").invoke(
                PROFILE_UPDATE_PROMPT.format(context=context, message=user_message)
            )
        except Exception as exc:
            print(f"长期画像抽取失败：{exc}")
            return {}

        data = LongTermMemory._loads_json(str(response.content).strip())
        if not isinstance(data, dict):
            data = {}
        data = LongTermMemory._supplement_profile_updates(user_message, data)
        if data:
            print(f"[长期记忆] 抽取画像字段：用户ID={user_id}，字段={list(data.keys())}")

        return data

    @staticmethod
    def _supplement_profile_updates(user_message: str, updates: dict[str, Any]) -> dict[str, Any]:
        """补全模型容易漏抽的当前职业字段。"""
        if updates.get("target_job"):
            return updates
        match = PROFILE_JOB_PATTERN.search(user_message or "")
        if not match:
            return updates
        level, job = match.groups()
        job = re.sub(r"^(一名|一个|一位|初级|中级|高级|资深|专家)", "", job.strip())
        if job:
            updates["target_job"] = job
        if level and not updates.get("target_level"):
            updates["target_level"] = level
        return updates

    @staticmethod
    def _apply_updates(db: Session, user_id: int, updates: dict[str, Any]) -> None:
        """把抽取结果合并进 UserProfile。"""
        profile = db.scalar(
            select(UserProfile)
            .where(UserProfile.user_id == user_id)
            .with_for_update()
        )
        if profile is None:
            try:
                profile = UserProfile(user_id=user_id)
                db.add(profile)
                db.flush()
            except IntegrityError:
                db.rollback()
                profile = db.scalar(
                    select(UserProfile)
                    .where(UserProfile.user_id == user_id)
                    .with_for_update()
                )

        changed = False
        for field, value in updates.items():
            if field in LIST_FIELDS:
                changed = LongTermMemory._merge_list(profile, field, value) or changed
            elif field in TEXT_FIELDS:
                changed = LongTermMemory._merge_text(profile, field, value) or changed

        if changed:
            db.commit()
            print(f"[长期记忆] 画像已写入数据库：用户ID={user_id}")

    @staticmethod
    def _merge_list(profile: UserProfile, field: str, value: Any) -> bool:
        """数组字段：使用本次抽取结果覆盖旧值，去重后最多保留 MAX_LIST_ITEMS 条。"""
        if not isinstance(value, list):
            return False
        new_items: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in new_items:
                new_items.append(text)
        if not new_items:
            return False
        new_items = new_items[:MAX_LIST_ITEMS]
        if list(getattr(profile, field) or []) == new_items:
            return False
        setattr(profile, field, new_items)
        return True

    @staticmethod
    def _merge_text(profile: UserProfile, field: str, value: Any) -> bool:
        """字符串字段：非空则直接覆盖。"""
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text:
            return False
        if field == "long_term_summary":
            text = text[:MAX_SUMMARY_CHARS]
        if getattr(profile, field) == text:
            return False
        setattr(profile, field, text)
        return True

    @staticmethod
    def _loads_json(text: str) -> Any:
        """解析 JSON；若模型多输出了文字，则截取首个 JSON 对象。"""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                print("[长期记忆] 画像抽取结果不是有效 JSON")
                return None
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                print("[长期记忆] 画像抽取结果 JSON 解析失败")
                return None

    @staticmethod
    def _append_text(lines: list[str], label: str, value: str | None) -> None:
        text = (value or "").strip()
        if text:
            lines.append(f"{label}：{text}")

    @staticmethod
    def _append_list(lines: list[str], label: str, value: list[str] | None) -> None:
        if value:
            lines.append(f"{label}：{'、'.join(value)}")
