"""一次性排查脚本：解码 pip http 缓存的 msgpack 元数据，打印 URL（用完即删）。"""

from __future__ import annotations

import pathlib
import re
import sys

CACHE = pathlib.Path(r"c:\users\zzx\appdata\local\pip\cache\http-v2")
# 只看交互式探测关心的那几个（21:35:40 / 21:35:41 / 21:37:13）
TARGETS = [
    "31caa051fc1ef8023c884d9b04a04940be50a3e6d9686e9afe083162",
    "6fd9dc90279bb3ab07874d72ac7103183204997d8a64e5efae4b224a",
    "eb97658ffb63e3939cb3dc97efc785dbe1e790b6a3500bac0ec647fe",
]

URL_RE = re.compile(rb"https?://[^\x00-\x20\"'\\]{10,200}")


def show(path: pathlib.Path) -> None:
    meta = path
    body = path.with_name(path.name + ".body")
    for label, item in (("meta", meta), ("body", body)):
        if not item.exists():
            print(f"  {label}: 不存在")
            continue
        raw = item.read_bytes()
        print(f"  {label}: {item.stat().st_size} 字节")
        heads = URL_RE.findall(raw[:4096])
        for url in dict.fromkeys(heads):
            print("    URL:", url.decode("utf-8", "replace"))
        if label == "body" and b"PK\x03\x04" in raw[:8]:
            print("    -> 这是一个 zip/wheel")
        # 打印可读的 ascii 片段，帮助判断 body 是什么
        text = re.sub(rb"[^\x20-\x7e]{2,}", b" ", raw[:400])
        preview = text.decode("ascii", "replace").strip()
        if preview:
            print("    预览:", preview[:260])


def main() -> int:
    for name in TARGETS:
        matches = [p for p in CACHE.rglob(name + "*") if p.suffix == ""]
        print(f"\n=== {name[:12]}… 命中 {len(matches)} 个文件 ===")
        for path in matches:
            print(f" 文件: {path}  ({path.stat().st_size} 字节)")
            show(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
