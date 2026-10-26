"""临时探针 3：拿到"板岩 ID"的方舟官方模型 ID 写法。用完即删。

同时验证一个理论：429 里方舟只回 [doubao-seedream-5-0-flash] 这个**不带日期的家族名**，
说明它把 `-260915` 当版本后缀剥掉了；那么正确的 daterange 写法可能是不带点的
`doubao-seedream-5-0-260128`。
"""

from __future__ import annotations

import httpx

from app.core.settings import settings

url = settings.seedream_base_url.rstrip("/") + "/images/generations"
headers = {
    "Authorization": f"Bearer {settings.seedream_api_key}",
    "Content-Type": "application/json",
}

for model in [
    "doubao-seedream-5-0-260128",   # 不带点 + 日期
    "doubao-seedream-5-0-flash-260915",  # 已知可用（但被限流）
    "doubao-seedream-5-0-251128",   # 备选日期
    "doubao-seedream-5-0-flash-251128",
]:
    body = {
        "model": model,
        "prompt": "一张科普信息图：什么是 GIL。竖版，简洁扁平插画风格",
        "size": "2k",
        "response_format": "url",
        "num_images": 1,
        "output_format": "png",
        "watermark": False,
    }
    r = httpx.post(url, headers=headers, json=body, timeout=120.0)
    p = r.json()
    err = p.get("error") or {}
    if err:
        print(f"[{r.status_code}] {model}\n      code={err.get('code')}\n      msg={str(err.get('message'))[:120]}")
    else:
        print(f"[{r.status_code}] {model}  OK -> {str((p.get('data') or [{}])[0].get('url'))[:90]}")
