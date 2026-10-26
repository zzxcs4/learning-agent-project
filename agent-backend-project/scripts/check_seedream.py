"""Seedream 出图链路诊断：配置 → 账号模型是否可用 → 真调一次。

**什么时候用**：难点讲解"只出文字、没有配图"时。出图是**静默降级**（失败不影响讲解
落库），所以线上表现永远是"卡有了、图没有"，真正的原因只在日志里。这个脚本把原因
从服务端逼出来，并给出"你该做什么"。

与 `verify_difficulty_card.py` 的分工：

- `verify_difficulty_card.py` —— **离线**验收编排逻辑（假 LLM / 假 Seedream），证明代码对；
- 本脚本 —— **在线**诊断第三方服务（真 Key / 真方舟），回答"今天为什么没图"。

用法（在 `agent-backend-project` 下）：

    python scripts/check_seedream.py              # 配置 + 账号目录 + 实调一次
    python scripts/check_seedream.py --no-generate  # 只查配置与账号目录（不花额度）

退出码：0=出图可用；1=不可用（`原因` 那行就是给用户看的话）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):  # pragma: no cover
        pass

import httpx  # noqa: E402

from app.core.settings import settings  # noqa: E402
from app.services import seedream_image_service as sis  # noqa: E402

# 出图是慢操作，诊断用的探针图要短、要稳
PROBE_PROMPT = "一张竖版科普信息图：什么是 GIL，标题清晰，简洁扁平插画风格"


def _redact(value: str) -> str:
    """日志/输出里绝不回显完整密钥——只露前缀和长度，够判断"配没配/像不像"。"""
    if not value:
        return "<EMPTY>"
    return f"<set len={len(value)} head={value[:8]}…>"


def _print_config() -> None:
    print("=" * 72)
    print("[1] 配置")
    print("=" * 72)
    print(f"  app_env                  = {settings.app_env}")
    print(f"  SEEDREAM_API_KEY         = {_redact(settings.seedream_api_key)}")
    print(f"  SEEDREAM_BASE_URL        = {settings.seedream_base_url}")
    print(f"  SEEDREAM_MODEL           = {settings.seedream_model}")
    print(f"  SEEDREAM_SIZE            = {settings.seedream_size}")
    print(f"  SEEDREAM_TIMEOUT_SECONDS = {settings.seedream_timeout_seconds}")
    print(f"  is_enabled()             = {sis.is_enabled()}")
    if "5.0-" in settings.seedream_model:
        print()
        print("  ⚠️ 模型 ID 里出现了 `5.0-`（带点）。方舟的 API id 是 `5-0-`（连字符），")
        print("     带点的写法实测必 404 InvalidEndpointOrModel.NotFound。")


def _check_catalog() -> bool:
    """查账号目录 `/v3/models`，确认配置的模型在不在（在≠已开通，仅作前置判断）。"""
    print()
    print("=" * 72)
    print("[2] 方舟账号模型目录（GET /v3/models）")
    print("=" * 72)
    url = settings.seedream_base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {settings.seedream_api_key}"},
            timeout=30.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data") or []
    except Exception as exc:  # noqa: BLE001 - 诊断脚本要报告而不是崩
        print(f"  查询失败（不影响实调结论）: {type(exc).__name__}: {exc}")
        return False

    seedream = [it for it in items if "seedream" in str(it.get("id", "")).lower()]
    print(f"  账号可见模型 {len(items)} 个，其中 Seedream {len(seedream)} 个：")
    hit = False
    for it in seedream:
        mid = str(it.get("id"))
        mark = " ← 当前配置" if mid == settings.seedream_model else ""
        if mark:
            hit = True
        print(f"    {mid:<36} status={str(it.get('status')):<10}{mark}")
    if not hit:
        print()
        print(f"  ⚠️ 配置的 `{settings.seedream_model}` **不在**账号目录里 → 调用会 404。")
    return hit


def _probe_once() -> tuple[bool, str]:
    """真调一次出图，返回 (是否成功, 给人看的原因)。"""
    print()
    print("=" * 72)
    print("[3] 实调一次出图（走项目真实函数 generate_image_bytes）")
    print("=" * 72)
    try:
        data, suffix = sis.generate_image_bytes(PROBE_PROMPT)
    except sis.ImageGenerationUnavailable as exc:
        # 异常文本本身已含"给人看的原因 + 服务端原文"，所以只打一次
        reason = sis.describe_failure(exc)
        print("  ❌ 不可用")
        print(f"  {exc}")
        return False, reason
    except Exception as exc:  # noqa: BLE001 - 非"能力不可用"是代码 bug，要暴露
        print(f"  ❌ 未预期的异常（这属于 bug，不是降级）: {type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"

    print(f"  ✅ 出图成功: {len(data)} bytes, 后缀 {suffix}（未上传 COS，仅内存）")
    return True, ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Seedream 出图链路诊断")
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="只查配置与账号目录，不真调出图（不花额度）",
    )
    args = parser.parse_args()

    _print_config()
    _check_catalog()

    if args.no_generate:
        print()
        print("（--no-generate：跳过实调）")
        return 0

    ok, reason = _probe_once()
    print()
    print("=" * 72)
    if ok:
        print("结论: 出图链路可用 ✅")
        return 0
    print("结论: 出图不可用 ❌ —— 难点讲解会降级成'只有文字与知识卡'")
    print(f"要做的事: {reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
