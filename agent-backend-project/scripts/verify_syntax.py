"""离线语法闸门：逐文件 `compile()`，不写 `.pyc`。

为什么不用 `compileall`：本项目在 DSH 沙箱下**不允许写 `__pycache__`**（踩坑记录第 2 条），
`compileall` 会把每个文件的 PermissionError 都打出来，把真正的语法错误淹没。
本脚本只读源码、只用 `compile()` 做语法检查，输出一行结论 + 失败清单。

用法：
    python scripts/verify_syntax.py            # 检查 app/ 与 scripts/
    python scripts/verify_syntax.py app tests  # 只检查指定目录
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TARGETS = ("app", "scripts", "tests", "alembic")

# 结论行里有 ✅/❌。Windows 中文控制台默认是 GBK，打不出来会抛 UnicodeEncodeError——
# 那会让"语法全过"的脚本以退出码 1 结束，**把通过报成失败**（实测踩到过）。
# 所以先把 stdout 切到 UTF-8；切不动（被重定向到不支持重配置的对象）就算了。
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover - 极少数流不支持
        pass


def iter_python_files(target: Path) -> list[Path]:
    """收集目标下的全部 .py（跳过 __pycache__ 与虚拟环境目录）。"""
    skip = {"__pycache__", ".venv", "venv", "node_modules", ".git"}
    return sorted(
        path
        for path in target.rglob("*.py")
        if not any(part in skip for part in path.parts)
    )


def main(argv: list[str]) -> int:
    targets = argv[1:] or list(DEFAULT_TARGETS)
    failures: list[tuple[str, str]] = []
    checked = 0

    for name in targets:
        target = PROJECT_ROOT / name
        if not target.exists():
            print(f"[skip] 目录不存在：{name}")
            continue
        for path in iter_python_files(target):
            checked += 1
            relative = path.relative_to(PROJECT_ROOT)
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                failures.append((str(relative), f"编码错误: {exc}"))
                continue
            try:
                compile(source, str(relative), "exec")
            except SyntaxError as exc:
                failures.append(
                    (str(relative), f"SyntaxError: 第 {exc.lineno} 行 {exc.msg}")
                )
            except ValueError as exc:  # 源码里出现 NUL 等
                failures.append((str(relative), f"ValueError: {exc}"))

    print(f"检查文件数：{checked}")
    if failures:
        print(f"❌ 语法失败：{len(failures)}")
        for path, reason in failures:
            print(f"   - {path}: {reason}")
        return 1
    print("✅ 全部通过（0 失败）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
