"""临时探针 6：完整列出方舟 Seedream 家族（状态不假设非空）。用完即删。"""

from __future__ import annotations

from collections import Counter

import httpx

from app.core.settings import settings

base = settings.seedream_base_url.rstrip("/")
r = httpx.get(base + "/models", headers={"Authorization": f"Bearer {settings.seedream_api_key}"}, timeout=30.0)
r.raise_for_status()
items = r.json().get("data") or []
print(f"总计 {len(items)} 条\n")

print("=== 全部 seedream 条目（不丢状态为空的）===")
for it in items:
    if "seedream" in str(it.get("id", "")).lower():
        print(f"  id={str(it.get('id')):<42} status={str(it.get('status')):<12} "
              f"domain={it.get('domain')!r} task={it.get('task_type')!r}")

print("\n=== 全部 ImageGeneration 域条目 ===")
for it in items:
    if it.get("domain") == "ImageGeneration":
        print(f"  id={str(it.get('id')):<42} status={str(it.get('status')):<12} task={it.get('task_type')!r}")

print("\n=== status 分布 ===")
print(Counter(str(it.get("status")) for it in items))
