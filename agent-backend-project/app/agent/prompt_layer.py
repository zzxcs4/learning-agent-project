"""提示词层：统一组装 Prompt、记忆与链路上下文。

功能：
1. build_stream_chain_context  - 通用链路组装
2. build_personalized_prompt   - 带用户信息注入的个性化提示词
3. inject_user_info_to_template - 提示词动态填充

升级说明（0.3.x → 1.x）：
  旧写法：用 RunnableWithMessageHistory 包装链路，通过 configurable 传 session_id 管理历史。
  新写法：历史消息直接从 DB 加载后注入 prompt，链路本身只是 prompt | llm，更简单直接。
  安装：pip install langchain==1.3.4 langchain-core==1.4.2 langchain-openai==1.2.2 langchain-community==0.4.2 langchain-classic==1.0.7
"""

from __future__ import annotations

import re
from typing import Any, Optional

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
# [0.3.x] from langchain_core.runnables.history import RunnableWithMessageHistory
# [0.3.x] from langchain_classic.schema.runnable.history import RunnableWithMessageHistory
from sqlalchemy.orm import Session

from app.agent.llm import get_chat_model
# from app.agent.memory import get_latest_knowledge
from app.services.prompt_template_service import prompt_template_manager
from app.services.memory_service import MemoryService
from app.services.user_profile_service import UserProfileService
from app.utils.prompt_logger import log_personalized_prompt_generated



def build_stream_chain_context(
    db: Session,
    user_id: int,
    session_id: int,
    current_message_id: int,
    provider: str,
    request_text: str,
    system_prompt: Optional[str] = None,
    rag_context: str = "",
    session_model: int | None = None,
) -> tuple[Runnable, dict[str, Any]]:
    """组装流式链路上下文，返回可执行链与输入变量。"""
    # 1) 统一记忆入口：返回已合并的最终历史，可能来自工作记忆、短期记忆或后续长期记忆。
    memory_context = MemoryService(db).load_context(
        user_id=user_id,
        session_id=session_id,
        before_message_id=current_message_id,
        session_model=session_model,
        query=request_text,
    )
    history_messages = memory_context["history_messages"]
    memory_system_prompts = [
        str(message.content).strip()
        for message in history_messages
        if isinstance(message, SystemMessage) and str(message.content).strip()
    ]
    if memory_system_prompts:
        history_messages = [message for message in history_messages if not isinstance(message, SystemMessage)]

    # 教学保留：这是改造前的写法，直接在 prompt_layer.py 中查询最近 10 条历史。
    # 缺点是“历史读取策略”和“Prompt 组装”耦合在一起，后续扩展工作记忆/长期记忆时会越来越乱。
    # history_limit = 10
    # history_rows = list(
    #     db.scalars(
    #         select(ChatMessage)
    #         .where(
    #             ChatMessage.user_id == user_id,
    #             ChatMessage.session_id == session_id,
    #             ChatMessage.id < current_message_id,
    #         )
    #         .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    #         .limit(history_limit)
    #     )
    # )
    # history_rows.reverse()
    # history = build_chat_history_from_rows(history_rows)
    # history_messages = history.messages

    # 2) system_prompt 为 None 时使用默认兜底提示词，确保链路始终可用。
    if system_prompt is None:
        system_prompt = "你是一个简洁、可靠的单Agent助手。优先直接回答问题，不要包装成 JSON。"
    if memory_system_prompts:
        system_prompt = "\n\n".join([system_prompt, *memory_system_prompts])
        print(f"[提示词] 已合并记忆系统提示：数量={len(memory_system_prompts)}，系统提示长度={len(system_prompt)}")

    # 转义 system_prompt 中的花括号，防止 ChatPromptTemplate 把 JSON 内容误解析为变量占位符。
    safe_system_prompt = system_prompt.replace("{", "{{").replace("}", "}}")
    if rag_context:
        request_text = rag_context + "\n\n【用户问题】\n" + request_text

    prompt = ChatPromptTemplate.from_messages(
        [
            ("system", safe_system_prompt),
            MessagesPlaceholder(variable_name="history"),
            ("human", "{user_input}"),
        ]
    )

    # [1.x 新写法] 历史消息直接注入 inputs，链路本身只是 prompt | llm，无需 RunnableWithMessageHistory 包装。
    chain = prompt | get_chat_model(provider=provider)
    stream_inputs = {
        "history": history_messages,
        "user_input": request_text,
    }

    # [0.3.x 旧写法] RunnableWithMessageHistory 包装链路，通过 configurable 传 session_id。
    # history_store = {str(session_id): history}
    # def get_session_history(session_id_key: str):
    #     return history_store.setdefault(session_id_key, history)
    # chain = RunnableWithMessageHistory(
    #     prompt | get_chat_model(provider=provider),
    #     get_session_history,
    #     input_messages_key="user_input",
    #     history_messages_key="history",
    # )
    # stream_inputs = {
    #     "user_input": request_text,
    #     "local_kb": {...},
    #     "search_result": get_latest_knowledge(request_text),
    # }

    # stream_inputs["local_kb"] = {
    #     "Python": "基础语法、循环、函数、列表字典、文件操作、异常处理",
    #     "Java": "面向对象、集合、IO、多线程、Spring基础",
    #     "前端": "HTML/CSS、JS DOM、Vue、Ajax、响应式布局"
    # }
    # stream_inputs["search_result"] = get_latest_knowledge(request_text)

    return chain, stream_inputs


def inject_user_info_to_template(
    template_content: str, # 参数1：完整的模板内容（含占位符）
    user_info_dict: dict,# 参数2：用户信息字典（格式化后的）
) -> str:
    """第三层：执行提示词注入 - 替换模板中的占位符为用户信息。"""
    prompt_content = template_content
    
    # 遍历字典的每个键值对
    for key, value in user_info_dict.items():
        # 构造占位符：{target_job}
        placeholder = "{" + key + "}"
        # 检查占位符是否存在
        if placeholder in prompt_content:
            # 替换占位符
            prompt_content = prompt_content.replace(placeholder, str(value))
    
    return prompt_content # 返回替换完成的提示词


def validate_user_info(user_info_dict: dict) -> dict:
    """第一层：验证 - 检查用户信息的有效性和安全性。"""
    FORBIDDEN_KEYWORDS = ["<script", "javascript:", "onclick", "prompt(", "eval("]
    MAX_LENGTH = {
        "target_job": 128,
        "years_experience": 64,
        "target_level": 32,
    }
    
    validated = {}
    for key, value in user_info_dict.items():
        if value is None:
            validated[key] = ""
            continue
        
        value_str = str(value)
        
        if key in MAX_LENGTH:
            max_len = MAX_LENGTH[key]
            if len(value_str) > max_len:
                value_str = value_str[:max_len]
        
        for keyword in FORBIDDEN_KEYWORDS:
            if keyword.lower() in value_str.lower():
                raise ValueError(f"用户信息包含禁用内容: {keyword}")
        
        validated[key] = value_str
    
    return validated


def build_personalized_prompt(
    db: Session,
    user_id: int,
    agent_name: str,
    scene: str,
) -> Optional[str]:
    """构建个性化提示词 - 完整的三层处理流程。
    
    流程：验证 → 格式化 → 注入
    """
    try:
        user_info = UserProfileService.get_profile_dict(db, user_id)
        validated_info = validate_user_info(user_info)
        formatted_info = UserProfileService.format_profile_for_injection(validated_info)
        
        full_prompt = prompt_template_manager.get_full_prompt(agent_name, scene, db)
        if not full_prompt:
            return None
        
        personalized_prompt = inject_user_info_to_template(full_prompt, formatted_info)
        
        log_personalized_prompt_generated(personalized_prompt, user_id, agent_name, scene)
        
        return personalized_prompt
        
    except Exception as e:
        raise RuntimeError(f"构建个性化提示词失败: {e}")


def format_retrieved_chunks(chunks: list[dict]) -> str:
    """把检索结果格式化为本轮用户问题的参考资料上下文。"""
    if not chunks:
        return ""

    lines = ["【参考资料】"]
    for i, chunk in enumerate(chunks, start=1):
        lines.append(f"[来源 {i}] 文件：{chunk['file_name']}")
        lines.append(chunk["text"])
        lines.append("")
    return "\n".join(lines).strip()


def build_rag_system_prompt(
    base_prompt: Optional[str],
    retrieved_text: str,
) -> Optional[str]:
    """在用户画像 prompt 后追加知识库参考资料使用规则，不直接拼接资料内容。"""
    if not base_prompt:
        base_prompt = "你是一个简洁、可靠的通用助手。请用自然语言直接回答，不要包装成 JSON。"

    if not retrieved_text:
        return (
            base_prompt
            + "\n\n本次未检索到相关参考资料。"
            + "请基于上下文常识回答；若问题需要具体资料但你不确定，请明确告知用户。"
        )

    return (
        base_prompt
        + "\n\n本轮用户问题前会提供【参考资料】。"
        + "参考资料仅作为事实依据，请优先基于参考资料回答用户问题。"
        + "如果参考资料中没有相关信息，请直接告知用户而不要编造。"
        + "若回答中引用了参考资料，请在末尾加上一句引导语，例如‘我的回答基于您提供的资料，您可以在下方查看来源’。"
    )
