"""临时探针 4：问方舟有没有"列可用模型"的接口。用完即删。"""

from __future__ import annotations

import httpx

from app.core.settings import settings

base = settings.seedream_base_url.rstrip("/")
headers = {"Authorization": f"Bearer {settings.seedream_api_key}"}

for path in ["/models", "/endpoints", "/foundation_models"]:
    try:
        r = httpx.get(base + path, headers=headers, timeout=30.0)
        print(f"--- GET {path} -> HTTP {r.status_code}")
        print(r.text[:1200])
    except Exception as exc:  # noqa: BLE001
        print(f"--- GET {path} -> {type(exc).__name__}: {exc}")
    print()
