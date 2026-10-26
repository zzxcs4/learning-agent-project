# app/core/redis_client.py
"""Redis 客户端封装：用于工作记忆等轻量状态存取。"""

import json

import redis
from redis.exceptions import RedisError

from app.core.logging_config import main_logger
from app.core.settings import settings


class RedisClient:
    """Redis 轻量封装；Redis 异常不向上抛出，避免阻断聊天主链路。"""

    def __init__(self):
        """初始化 Redis 连接，使用配置中的参数。"""
        self.client = redis.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            db=settings.redis_db,
            password=settings.redis_password or None,
            decode_responses=True,
        )

    def get_json(self, key: str) -> dict:
        """
        从 Redis 获取指定键的 JSON 数据并反序列化为字典。

        参数:
            key (str): 要获取的键名。

        返回:
            dict: 反序列化后的字典，若键不存在或发生错误则返回空字典。
        """
        try:
            data = self.client.get(key)
            return json.loads(data) if data else {}
        except (RedisError, json.JSONDecodeError) as exc:
            main_logger.warning("Redis get_json 失败：键={}，错误={}", key, exc)
            return {}

    def set_json(self, key: str, value: dict, ttl: int) -> None:
        """
        将字典序列化为 JSON 并存入 Redis，同时设置过期时间。

        参数:
            key (str): 存储的键名。
            value (dict): 待存储的字典数据。
            ttl (int): 过期时间（秒）。
        """
        try:
            self.client.setex(key, ttl, json.dumps(value, ensure_ascii=False))
        except RedisError as exc:
            main_logger.warning("Redis set_json 失败：键={}，错误={}", key, exc)

    def hgetall(self, key: str) -> dict:
        """
        获取 Redis 哈希表中指定键的所有字段和值。

        参数:
            key (str): 哈希表的键名。

        返回:
            dict: 包含所有字段及值的字典，若键不存在或发生错误则返回空字典。
        """
        try:
            return self.client.hgetall(key) or {}
        except RedisError as exc:
            main_logger.warning("Redis hgetall 失败：键={}，错误={}", key, exc)
            return {}

    def hset(self, key: str, mapping: dict) -> None:
        """
        将映射字典设置到 Redis 哈希表中。

        参数:
            key (str): 哈希表的键名。
            mapping (dict): 要设置的字段-值映射。
        """
        if not mapping:
            return
        try:
            self.client.hset(key, mapping=mapping)
        except RedisError as exc:
            main_logger.warning("Redis hset 失败：键={}，错误={}", key, exc)

    def expire(self, key: str, ttl: int) -> None:
        """
        为指定键设置过期时间。

        参数:
            key (str): 键名。
            ttl (int): 过期时间（秒）。
        """
        try:
            self.client.expire(key, ttl)
        except RedisError as exc:
            main_logger.warning("Redis expire 失败：键={}，错误={}", key, exc)

    def delete(self, key: str) -> None:
        """
        删除 Redis 中的指定键。

        参数:
            key (str): 要删除的键名。
        """
        try:
            self.client.delete(key)
        except RedisError as exc:
            main_logger.warning("Redis delete 失败：键={}，错误={}", key, exc)