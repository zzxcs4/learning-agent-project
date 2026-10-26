"""临时探针：直连 Seedream，把被服务层吞掉的真实报错逼出来。用完即删。"""

from __future__ import annotations

import json
import os
import traceback

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

import httpx  # noqa: E402

from app.core.settings import settings  # noqa: E402
from app.services import seedream_image_service as sis  # noqa: E402

print("=" * 70)
print("[1] 配置是否真的读进来了")
print("=" * 70)
print(f"app_env            = {settings.app_env!r}")
print(f"seedream_api_key   = {'<set, len=%d, head=%s>' % (len(settings.seedream_api_key), settings.seedream_api_key[:8]) if settings.seedream_api_key else '<EMPTY>'}")
print(f"seedream_base_url  = {settings.seedream_base_url!r}")
print(f"seedream_model     = {settings.seedream_model!r}")
print(f"seedream_size      = {settings.seedream_size!r}")
print(f"is_enabled()       = {sis.is_enabled()}")

print()
print("=" * 70)
print("[2] 原始 HTTP 打一次 /images/generations（跳过 SDK，看服务端原话）")
print("=" * 70)
url = settings.seedream_base_url.rstrip("/") + "/images/generations"
body = {
    "model": settings.seedream_model,
    "prompt": "一张科普信息图：GIL 与多线程。竖版，标题清晰，简洁扁平插画风格",
    "size": settings.seedream_size,
    "response_format": "url",
    "num_images": 1,
    "output_format": "png",
    "watermark": False,
}
try:
    r = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.seedream_api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=120.0,
    )
    print(f"HTTP {r.status_code}")
    try:
        payload = r.json()
        print(json.dumps(payload, ensure_ascii=False, indent=2)[:4000])
    except Exception:
        print(r.text[:4000])
except Exception:
    print("!! 连请求都没发出去")
    traceback.print_exc()

print()
print("=" * 70)
print("[3] 走项目真实函数 generate_image_bytes（看最终降级原因）")
print("=" * 70)
try:
    data, suffix = sis.generate_image_bytes(
        "一张科普信息图：GIL 与多线程。竖版，标题清晰，简洁扁平插画风格"
    )
    print(f"OK bytes={len(data)} suffix={suffix}")
except sis.ImageGenerationUnavailable as exc:
    print(f"ImageGenerationUnavailable: {exc}")
except Exception:
    traceback.print_exc()
