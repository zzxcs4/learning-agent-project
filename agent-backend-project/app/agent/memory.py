"""教学保留：旧记忆层示例。

当前运行时代码已迁移到 app.services.memory_service。
联网搜索工具已停用，避免在业务链路中引入不必要的外部检索依赖。
"""

# [0.3.x] from langchain_community.chat_message_histories import ChatMessageHistory
# from langchain_core.chat_history import InMemoryChatMessageHistory as ChatMessageHistory
# from langchain_community.tools import DuckDuckGoSearchRun
#
# from app.db.models import ChatMessage
#
# # 轻量搜索工具：用于补充“外部记忆/最新信息”。
# _search = DuckDuckGoSearchRun(max_results=3)
#
#
# def build_chat_history_from_rows(rows: list[ChatMessage]) -> ChatMessageHistory:
#     """把数据库问答记录转换成模型可读取的历史消息。"""
#     history = ChatMessageHistory()
#     for row in rows:
#         if row.request_text:
#             history.add_user_message(row.request_text)
#         if row.response_text:
#             history.add_ai_message(row.response_text)
#     return history
#
#
# def get_latest_knowledge(topic: str) -> str:
#     """检索与当前问题相关的最新信息，失败时返回空串。"""
#     query = f"{topic} 2026年 大厂面试题 高频考点 技术官网 招聘要求 排除广告"
#     try:
#         result = _search.run(query)
#     except Exception:
#         return ""
#     if not result or len(result) < 30:
#         return ""
#     return result
