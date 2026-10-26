"""一次性排查脚本：在 pip 的 http 缓存里找 openai 各版本串（用完即删）。"""

from __future__ import annotations

import datetime
import pathlib
import re

CACHE = pathlib.Path(r"c:\users\zzx\appdata\local\pip\cache\http-v2")
PATTERN = re.compile(rb"openai[-_/](\d+\.\d+\.\d+)")


def main() -> None:
    found: dict[str, list[tuple[str, float]]] = {}
    scanned = 0
    for path in CACHE.rglob("*"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size < 200:
            continue
        scanned += 1
        try:
            head = path.read_bytes()[:16384]
        except OSError:
            continue
        for match in PATTERN.finditer(head):
            version = match.group(1).decode()
            found.setdefault(version, []).append((str(path), path.stat().st_mtime))

    print(f"扫描缓存文件 {scanned} 个")
    print("缓存里出现过的 openai 版本串（按版本倒序）：")
    versions = sorted(found, key=lambda s: [int(x) for x in s.split(".")], reverse=True)
    for version in versions[:15]:
        times = sorted(
            {
                datetime.datetime.fromtimestamp(t).strftime("%m-%d %H:%M")
                for _, t in found[version]
            }
        )
        print(f"  {version:10s} 命中 {len(found[version]):3d} 处  文件时间 {times[:4]}")

    # 顺带看看缓存里最近写入的文件（判断那次 pip 到底下载了什么）
    print("\n缓存里最近的 15 个文件（按写入时间）：")
    recent: list[tuple[float, int, str]] = []
    for path in CACHE.rglob("*"):
        if path.is_file():
            try:
                st = path.stat()
            except OSError:
                continue
            recent.append((st.st_mtime, st.st_size, str(path)))
    recent.sort(reverse=True)
    for mtime, size, name in recent[:15]:
        stamp = datetime.datetime.fromtimestamp(mtime).strftime("%m-%d %H:%M:%S")
        print(f"  {stamp}  {size:>10d}  {name}")


if __name__ == "__main__":
    main()
