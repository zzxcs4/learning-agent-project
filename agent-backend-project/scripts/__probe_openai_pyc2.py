"""一次性排查：读 openai/__pycache__ 里 .pyc 的 magic 与 mtime，判断它们是否有效线索（用完即删）。

用途：把"1982 个 .pyc 全部不一致"这件事查清楚——是"升级前的另一个版本"，
还是"别的 Python 版本留下的废弃字节码"（magic 不同就与当前安装无关）。
"""

from __future__ import annotations

import collections
import datetime
import importlib.util
import pathlib
import struct
import sys

PKG = pathlib.Path(r"D:\conda_envs\zxlc_agent\Lib\site-packages\openai")


def main() -> int:
    current_magic = importlib.util.MAGIC_NUMBER
    print("当前解释器 magic:", current_magic.hex(), "(python", sys.version.split()[0], ")")

    magics: collections.Counter[str] = collections.Counter()
    newer_than_py = 0
    older_than_py = 0
    total = 0
    pyc_times: list[float] = []

    for pyc in PKG.rglob("*.pyc"):
        total += 1
        raw = pyc.read_bytes()[:16]
        magic = raw[:4]
        flags = struct.unpack("<I", raw[4:8])[0]
        mtime = struct.unpack("<I", raw[8:12])[0]
        magics[magic.hex()] += 1
        pyc_times.append(pyc.stat().st_mtime)
        # 该 .pyc 对应的源文件：<pkg>/x/y.pyc -> <pkg>/x/y.py
        source = pyc.parent.parent / f"{pyc.stem}.py"
        if source.exists():
            if pyc.stat().st_mtime > source.stat().st_mtime:
                newer_than_py += 1
            else:
                older_than_py += 1
        _ = flags, mtime

    print(f"openai 下 .pyc 总数: {total}")
    print("magic 分布（magic -> 个数）:")
    for magic, count in magics.most_common():
        mark = "  <= 当前解释器" if bytes.fromhex(magic) == current_magic else ""
        print(f"  {magic}  {count}{mark}")
    print(f".pyc 比 .py 新: {newer_than_py}   比 .py 旧: {older_than_py}")
    if pyc_times:
        lo = datetime.datetime.fromtimestamp(min(pyc_times)).strftime("%m-%d %H:%M:%S")
        hi = datetime.datetime.fromtimestamp(max(pyc_times)).strftime("%m-%d %H:%M:%S")
        print(f".pyc 时间范围: {lo} ~ {hi}")

    verdict = (
        "全部 .pyc 的 magic 与当前解释器相同，且都比 .py 新 → 是这次升级后新生成的，"
        "不能用来判定旧版本（源码 mtime 在 pip 解包时会变，字节码必然重算）"
        if magics and set(magics) == {current_magic.hex()}
        else "存在与当前解释器 **不同 magic** 的 .pyc → 那些是别的 Python 版本留下的废弃字节码"
    )
    print("结论:", verdict)
    return 0


if __name__ == "__main__":
    sys.exit(main())
