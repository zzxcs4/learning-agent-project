"""临时探针 7：把账号目录里 7 个 Seedream 全部实打一遍，定死"哪些真能调"。用完即删。"""

from __future__ import annotations

import httpx

from app.core.settings import settings

url = settings.seedream_base_url.rstrip("/") + "/images/generations"
headers = {
    "Authorization": f"Bearer {settings.seedream_api_key}",
    "Content-Type": "application/json",
}

CATALOG = [
    "doubao-seedream-3-0-t2i-250415",
    "doubao-seedream-4-0-250828",
    "doubao-seedream-4-5-251128",
    "doubao-seedream-5-0-260128",
    "doubao-seedream-4-0-20260415",
    "doubao-seedream-5-0-pro-260628",
    "doubao-seedream-5-0-flash-260915",
]

print(f"{'模型 ID':<34} {'HTTP':<5} 结果")
print("-" * 100)
for model in CATALOG:
    body = {
        "model": model,
        "prompt": "一张竖版科普信息图：什么是 GIL，简洁扁平插画风",
        "size": "2k",
        "response_format": "url",
        "num_images": 1,
        "output_format": "png",
        "watermark": False,
    }
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=120.0)
        p = r.json()
        err = p.get("error") or {}
        if err:
            short = str(err.get("message", ""))[:70]
            print(f"{model:<34} {r.status_code:<5} {err.get('code')} | {short}")
        else:
            data = p.get("data") or []
            u = (data[0].get("url") if data else None) or "<no url>"
            print(f"{model:<34} {r.status_code:<5} ✅ OK  url={str(u)[:50]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{model:<34} {'ERR':<5} {type(exc).__name__}: {str(exc)[:60]}")
