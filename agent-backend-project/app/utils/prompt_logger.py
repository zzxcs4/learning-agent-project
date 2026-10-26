"""提示词系统的集中日志输出管理。只打印重点数据，避免混淆。"""


def log_redis_status(use_redis: bool, error: str = None):
    """打印 Redis 连接状态"""
    if not use_redis:
        print(f"[提示词系统] Redis 不可用，将使用数据库查询: {error}")
    else:
        print("[提示词系统] Redis 已连接，使用缓存")


def log_personalized_prompt_generated(prompt: str, user_id: int, agent_name: str, scene: str):
    """打印生成的完整提示词 - 最关键的输出！"""
    print("\n" + "="*80)
    print(f"[个性化提示词生成] user_id={user_id}, agent={agent_name}, scene={scene}")
    print("="*80)
    print(f"【发送给 LLM 的完整提示词】({len(prompt)} 字符)")
    print("-"*80)
    print(prompt)
    print("-"*80 + "\n")


def log_chain_ready(user_id: int, session_id: int, agent_name: str, scene: str, provider: str, prompt_length: int):
    """打印链路就绪信息 - 关键统计"""
    print("\n" + "="*80)
    print("[LangChain 链路已就绪]")
    print("="*80)
    print(f"  用户ID: {user_id}")
    print(f"  会话ID: {session_id}")
    print(f"  Agent: {agent_name}")
    print(f"  场景: {scene}")
    print(f"  LLM: {provider}")
    print(f"  提示词长度: {prompt_length} 字符")
    print("="*80 + "\n")
