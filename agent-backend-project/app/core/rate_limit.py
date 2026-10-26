import threading
import time

from fastapi import Request

from app.core import raise_error

# 全局锁：保证并发请求下计数更新的原子性
_LOCK = threading.Lock()
# 计数存储：key -> (请求时所在的“时间段的开始时间”, 已经请求了多少次)
_WINDOWS: dict[str, tuple[int, int]] = {}


def rate_limit_dep(limit: int, window_seconds: int, key_prefix: str):
    """内存限流依赖：固定窗口 + IP 维度。

    实现要点：
    1) 以 (key_prefix + 客户端IP) 作为限流键，区分不同接口与客户端。
    2) 固定窗口：把时间划分为 [window_seconds] 秒一段的桶。
    3) 每个桶内最多允许 [limit] 次请求，超出直接返回 429。
    """

    async def dependency(request: Request) -> None:
        # 生产常见：优先取代理头部，其次回退到直连 IP
        forwarded_for = request.headers.get("x-forwarded-for", "")
        client_host = forwarded_for.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )
        key = f"{key_prefix}:{client_host}"
        now = int(time.time())
        # 计算当前时间所在的时间窗口起点（固定窗口）
        window_start = now - (now % window_seconds)
        with _LOCK:
            # 读取当前窗口的起始时间和请求次数，不存在则初始化
            last_start, count = _WINDOWS.get(key, (window_start, 0))
            # 跨窗口则重置计数
            if last_start != window_start:
                last_start, count = window_start, 0
            # 超过限制直接抛错
            if count >= limit:
                raise_error(
                    code=429,
                    message="请求过于频繁，请稍后再试",
                    detail={"limit": limit, "window_seconds": window_seconds},
                )
            # 计数 +1 并写回
            _WINDOWS[key] = (last_start, count + 1)

    return dependency
