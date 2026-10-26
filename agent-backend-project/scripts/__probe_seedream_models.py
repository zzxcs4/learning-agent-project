"""临时探针 2：换几个 Seedream 模型名，看是不是"模型名不对"而不是"账号被限"。用完即删。"""

from __future__ import annotations

import json
import os

import httpx

from app.core.settings import settings

url = settings.seedream_base_url.rstrip("/") + "/images/generations"
headers = {
    "Authorization": f"Bearer {settings.seedream_api_key}",
    "Content-Type": "application/json",
}

CANDIDATES = [
    settings.seedream_model,  # 用户当前配的
    "doubao-seedream-5.0-260128",  # 项目原始默认
    "doubao-seedream-4-0-250828",
    "doubao-seedream-3-0-t2i-250415",
    "doubao-seedream-5-0-flash",
    "doubao-seedream-4-5-251128",
]

for model in CANDIDATES:
    body = {
        "model": model,
        "prompt": "一张科普信息图：什么是 GIL。竖版，标题清晰，简洁扁平插画风格",
        "size": settings.seedream_size,
        "response_format": "url",
        "num_images": 1,
        "output_format": "png",
        "watermark": False,
    }
    try:
        r = httpx.post(url, headers=headers, json=body, timeout=120.0)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text[:300]}
        err = payload.get("error") if isinstance(payload, dict) else None
        if err:
            print(f"[{r.status_code}] {model}\n      code={err.get('code')} msg={str(err.get('message'))[:160]}")
        else:
            n = len(payload.get("data") or []) if isinstance(payload, dict) else 0
            first = (payload.get("data") or [{}])[0] if isinstance(payload, dict) else {}
            print(f"[{r.status_code}] {model}\n      OK data_len={n} url_head={str(first.get('url'))[:80]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] {model} -> {type(exc).__name__}: {exc}")
