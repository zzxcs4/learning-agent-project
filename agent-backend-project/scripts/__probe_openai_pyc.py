"""一次性排查脚本：用残留的 .pyc 判断"上次 pip 安装时的 openai 是哪一代"（用完即删）。

原理：pip 升级时**不会**删 `__pycache__/*.pyc`。若旧的 `.pyc` 与当前安装版本重新编译出的
字节码**不一致**，说明磁盘上那份 `.pyc` 是**另一个版本**编译的（够证明"换过版本"，
不够直接给出旧版本号）。只比较同步编译结果，运行期不执行任何 openai 代码。
"""

from __future__ import annotations

import importlib.util
import py_compile
import sys
import tempfile
from pathlib import Path

SITE = Path(r"D:\conda_envs\zxlc_agent\Lib\site-packages")
PKG = SITE / "openai"


def compile_to_bytes(source: Path) -> bytes | None:
    try:
        with tempfile.NamedTemporaryFile(suffix=".pyc", delete=False) as tmp:
            target = Path(tmp.name)
        py_compile.compile(str(source), cfile=str(target), doraise=True, quiet=2)
        data = target.read_bytes()
        target.unlink(missing_ok=True)
        return data
    except Exception:
        return None


def main() -> int:
    print("MAGIC:", importlib.util.MAGIC_NUMBER.hex())
    same = diff = missing = 0
    samples: list[str] = []
    for source in sorted(PKG.rglob("*.py")):
        pyc = source.parent / "__pycache__" / f"{source.stem}.cpython-312.pyc"
        if not pyc.exists():
            missing += 1
            continue
        old = pyc.read_bytes()
        new = compile_to_bytes(source)
        if new is None:
            missing += 1
            continue
        # 跳过 16 字节头（magic + flags + mtime + size）；mtime 会随 pip 解包时间变化
        if old[16:] == new[16:]:
            same += 1
        else:
            diff += 1
            if len(samples) < 12:
                samples.append(str(source.relative_to(PKG)))

    total = same + diff
    print(f"比对 .py 数：{total}（无 .pyc {missing}）")
    print(f"字节码一致：{same}    不一致：{diff}")
    if diff:
        print("不一致样本：")
        for item in samples:
            print("   -", item)
    verdict = (
        "旧 .pyc 与当前 3.35.1 一致 → 升级前**很可能也是 3.x**"
        if total and diff == 0
        else "旧 .pyc 与当前 3.35.1 不一致 → 升级前是**另一个版本**"
        if diff
        else "无 .pyc 可比对 → 该线索不可用"
    )
    print("结论:", verdict)
    # 顺带给出 .pyc 的时间范围（判断它是不是这次升级后新生成的）
    stamps = sorted(
        {
            (p.parent.parent / f"{p.stem}.py").stat().st_mtime
            for p in PKG.rglob("*.pyc")
        }
    )
    import datetime

    if stamps:
        print(
            "openai 源码 mtime 范围:",
            datetime.datetime.fromtimestamp(stamps[0]).strftime("%m-%d %H:%M"),
            "~",
            datetime.datetime.fromtimestamp(stamps[-1]).strftime("%m-%d %H:%M"),
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
