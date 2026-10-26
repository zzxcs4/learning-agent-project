"""临时探针：查语义意图模型为何加载失败（fastembed / HuggingFace 下载）。用完即删。"""

from __future__ import annotations

import os
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

print("=" * 72)
print("1. 相关环境变量")
print("=" * 72)
for key in (
    "HF_ENDPOINT",
    "HF_HOME",
    "HF_HUB_OFFLINE",
    "TRANSFORMERS_OFFLINE",
    "HUGGINGFACE_HUB_TOKEN",
    "HF_TOKEN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
    "NO_PROXY",
):
    v = os.environ.get(key)
    print(f"  {key:<26} = {v if v is None else ('<set len=%d>' % len(v))}")

print()
print("=" * 72)
print("2. fastembed 缓存目录状态")
print("=" * 72)
cache = Path(tempfile.gettempdir()) / "fastembed_cache"
print(f"  {cache}  exists={cache.exists()}")
if cache.exists():
    for p in sorted(cache.rglob("*"))[:25]:
        rel = p.relative_to(cache)
        size = p.stat().st_size if p.is_file() else 0
        print(f"    {'DIR ' if p.is_dir() else 'FILE'} {str(rel):<60} {size}")

print()
print("=" * 72)
print("3. 直接尝试加载模型，拿完整异常")
print("=" * 72)
try:
    from fastembed import TextEmbedding

    from app.utils.intent_classifier import DEFAULT_MODEL_NAME

    print(f"  模型名: {DEFAULT_MODEL_NAME}")
    model = TextEmbedding(DEFAULT_MODEL_NAME)
    print(f"  ✅ 加载成功: {type(model)}")
    vecs = list(model.embed(["大模型参数的演化"]))
    print(f"  向量维度: {len(vecs[0])}")
except Exception:
    print("  ❌ 加载失败，完整堆栈：")
    traceback.print_exc()
