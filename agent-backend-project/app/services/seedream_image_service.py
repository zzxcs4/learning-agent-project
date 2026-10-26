"""豆包 Seedream 文生图服务：难点讲解知识卡的"出图"这一步。

定位与纪律：

1. **只做一件事**：把一段已经拼好的图像 Prompt 变成图片字节。提示词怎么拼在
   `app/agent/difficulty_learner.py::build_image_prompt`，落库与访问地址在
   `app/services/difficulty_learner_service.py`——这里既不碰数据库也不碰 COS。
2. **没配 Key 就是"没这个能力"，不是报错**（与 DashScope / 火山 OCR 同一策略）：
   抛 `ImageGenerationUnavailable` 让调用方降级成"只出文字讲解"。用户没填
   `SEEDREAM_API_KEY` 时，难点讲解仍然完整可用，只是少一张图。
3. **走 OpenAI 兼容协议**：Seedream 挂在火山方舟的 `/api/v3` 上，与项目既有的
   DeepSeek 中转同一套协议；`num_images` / `output_format` / `watermark` 是方舟
   的**非标准**字段，必须走 `extra_body` 透传，直接当具名参数传会被 SDK 拒掉。
4. **出图是慢操作**：单张实测数十秒，所以调用方（服务层）要按"可能超时"来设计
   （重试 + 超时收敛），不能让它把整条讲解链路拖死。

为什么不用 `langchain_openai.ChatOpenAI` 那套：图像生成不在 LangChain 的聊天模型
抽象里，硬套只会多一层没用的转接。这里直接用官方 `openai` SDK。

⚠️ **版本纪律**：`openai` 必须落在 `2.26.0 <= x < 3.0.0`（见 `requirements.txt` 里那条）。
3.x 把 HTTP 客户端换成了 `httpx2`，与 `langchain-core` 传进去的普通 `httpx.Client`
类型不兼容（`SyncByteStream` 是两个不同的类），会让**所有走 langchain 的链路**在发请求时
断言失败——本模块反而察觉不到（它自己建的客户端走的是 httpx2，是自洽的）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
from openai import OpenAI

from app.core.settings import settings

# 出图格式：Seedream 的 output_format 只认 png/jpeg，本项目白名单里 png 最稳（无损、带透明通道）
OUTPUT_FORMAT = "png"
# 下载生成图时的 UA：方舟返回的是 CDN 直链，裸 httpx 有时被挡
DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0"}
# 兜底后缀：拿不到 URL 后缀时按 png 处理（与 OUTPUT_FORMAT 一致）
DEFAULT_SUFFIX = ".png"


class ImageGenerationUnavailable(RuntimeError):
    """出图能力不可用（没配 Key / 服务端拒了请求 / 下载失败）。

    单独成类是为了让调用方**只对"能力不可用"降级**，而不是把所有异常都吞掉：
    真出了别的 bug 时仍然要往上抛，不要被"优雅降级"掩盖成"静默没有图"。
    """


# 方舟的错误码 → 给人看的原因。分这几类的意义不同，用户要采取的动作也不同：
# - 额度/未开通：**改代码没用**，得去控制台充值或开通模型，所以要直说；
# - 模型 ID 不存在：配错了，指出正确的写法；
# - 其余：保留原文，方便排查。
_ARK_ERROR_REASONS: dict[str, str] = {
    "SetLimitExceeded": (
        "图像服务额度已用尽（火山方舟「安全体验模式」免费额度耗尽，模型已被暂停）——"
        "请到方舟控制台的模型开通页面调整额度或关闭安全体验模式"
    ),
    "ModelNotOpen": "该图像模型尚未在火山方舟控制台开通",
    "InvalidEndpointOrModel.NotFound": (
        "图像模型 ID 不存在或无权访问——模型 ID 要写方舟的 API id"
        "（形如 doubao-seedream-5-0-flash-260915），不是文档里的展示名"
    ),
    "AuthenticationError": "图像服务密钥无效（检查 SEEDREAM_API_KEY）",
    "QuotaExceeded": "图像服务计费额度不足",
    "RateLimitExceeded": "图像服务触发限流，请稍后再试",
}


def describe_failure(exc_or_message: object) -> str:
    """把出图异常/错误文本翻成**给用户看的**原因，失败时原样返回（不抛异常）。

    为什么需要它：降级是静默的（出图失败不影响讲解落库），用户只看到"没有图"。
    之前前端只能显示"图像服务未配置或调用失败"——把"没配 Key"和"额度耗尽"混成一句，
    等于逼用户来问人。这里把错误码映射成"要做什么"，让提示自己可行动。

    匹配策略是**先按错误码、再按关键词**：SDK 抛出的字符串里有 `code`，但不同 SDK
    版本格式不同，所以不能只依赖正则；拿不到码时退回包含判断。
    """
    text = str(exc_or_message or "").strip()
    if not text:
        return "出图失败（未返回原因）"
    for code, reason in _ARK_ERROR_REASONS.items():
        if code in text:
            return reason
    lowered = text.lower()
    if "未配置 seedream_api_key" in lowered:
        return "图像服务未配置（缺少 SEEDREAM_API_KEY）"
    if "timeout" in lowered or "timed out" in lowered:
        return "图像服务调用超时（单张出图通常需要数十秒，可调大 SEEDREAM_TIMEOUT_SECONDS）"
    return text


def _failure_with_detail(exc: object) -> str:
    """`describe_failure` 的结果 + **服务端原文**。

    为什么两者都要：给人看的那句必须可行动（"去控制台调额度"），而排障需要错误码与
    Request id（提工单时方舟只认 Request id）。用户看的是前端那句，运维看的是日志里
    这条——两边都不能丢。
    """
    reason = describe_failure(exc)
    detail = str(exc or "").strip()
    if not detail or detail == reason:
        return reason
    return f"{reason}（服务端原文: {detail}）"


def generate_image_bytes(prompt: str) -> tuple[bytes, str]:
    """调用 Seedream 出图并下载，返回 `(图片字节, 后缀)`。

    后缀带点（如 `.png`），与 `upload_service_cos.ALLOWED_IMAGE_EXTENSIONS` 的键一致。

    失败一律收敛成 `ImageGenerationUnavailable`：缺 Key、请求被拒、返回里没有图片、
    下载失败都算"这次拿不到图"。调用方据此降级。
    """
    text = (prompt or "").strip()
    if not text:
        raise ImageGenerationUnavailable("图像 Prompt 为空，无法出图")
    if not settings.seedream_api_key:
        raise ImageGenerationUnavailable("未配置 SEEDREAM_API_KEY")

    client = OpenAI(
        base_url=settings.seedream_base_url,
        api_key=settings.seedream_api_key,
        timeout=settings.seedream_timeout_seconds,
    )
    try:
        resp = client.images.generate(
            model=settings.seedream_model,
            prompt=text,
            # 尺寸是**档位**（2k/4k），不是像素值——方舟会拒掉 "1024x1024" 这类 OpenAI 写法
            size=settings.seedream_size,
            response_format="url",
            # 方舟私有字段必须走 extra_body 透传，否则被 SDK 的参数校验挡下
            extra_body={
                "num_images": 1,
                "output_format": OUTPUT_FORMAT,
                # 训练/演示用途，水印只会干扰知识卡阅读
                "watermark": False,
            },
        )
    except Exception as exc:  # noqa: BLE001 - 上游 SDK 异常种类多，统一收敛成"这次没图"
        raise ImageGenerationUnavailable(_failure_with_detail(exc)) from exc

    if not resp.data:
        raise ImageGenerationUnavailable("文生图未返回图片")

    image_url = str(resp.data[0].url or "").strip()
    if not image_url:
        raise ImageGenerationUnavailable("文生图未返回可下载地址")

    suffix = guess_suffix(image_url)
    try:
        with httpx.Client(
            timeout=settings.seedream_download_timeout_seconds, follow_redirects=True
        ) as http:
            download = http.get(image_url, headers=DOWNLOAD_HEADERS)
            download.raise_for_status()
            image_bytes = download.content
    except Exception as exc:  # noqa: BLE001 - 网络层异常同样按"这次没图"处理
        raise ImageGenerationUnavailable(
            f"文生图结果下载失败: {describe_failure(exc)}"
        ) from exc

    if not image_bytes:
        raise ImageGenerationUnavailable("文生图下载结果为空")

    print(
        f"[Seedream] model={settings.seedream_model} size={settings.seedream_size} "
        f"bytes={len(image_bytes)}"
    )
    return image_bytes, suffix


def guess_suffix(image_url: str) -> str:
    """从生成图 URL 猜后缀；拿不到或不在白名单内时回退 `.png`。

    CDN 直链常带查询串（`...?sign=xxx`），所以先剥掉 `?` 之后的部分再取后缀。
    """
    raw = str(image_url or "").split("?", 1)[0]
    suffix = Path(raw).suffix.strip().lower()
    if not suffix:
        return DEFAULT_SUFFIX
    return suffix


def is_enabled() -> bool:
    """出图能力是否已配置（供路由/前端文案判断"这次会不会有卡图"）。"""
    return bool(settings.seedream_api_key and settings.seedream_model)


__all__ = [
    "DEFAULT_SUFFIX",
    "ImageGenerationUnavailable",
    "OUTPUT_FORMAT",
    "describe_failure",
    "generate_image_bytes",
    "guess_suffix",
    "is_enabled",
]
