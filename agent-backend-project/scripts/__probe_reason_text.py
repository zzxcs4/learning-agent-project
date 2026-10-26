"""临时探针 8：验证错误翻译与"用户看到的那句话"是否干净。用完即删。"""

from __future__ import annotations

import os

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.services import seedream_image_service as sis  # noqa: E402
from app.services.difficulty_learner_service import _degraded_reason  # noqa: E402

CASES = [
    ("429 额度", "Error code: 429 - {'error': {'code': 'SetLimitExceeded', "
                 "'message': 'Your account [2107851299] has reached the set usage limit "
                 "for the [doubao-seedream-5-0-flash] model, and the model service has been "
                 "paused. To continue using this model, please visit the Model Activation "
                 "page to adjust or close the \"Safe Experience Mode\". Request id: 02179250abc'}}"),
    ("404 未开通", "Error code: 404 - {'error': {'code': 'ModelNotOpen', "
                   "'message': 'Your account 2107851299 has not activated the model "
                   "doubao-seedream-5-0-pro-260628. Request id: 02179250def'}}"),
    ("404 ID 写错", "Error code: 404 - {'error': {'code': 'InvalidEndpointOrModel.NotFound', "
                    "'message': 'The model or endpoint doubao-seedream-5.0-260128 does not "
                    "exist or you do not have access to it.'}}"),
    ("401 密钥", "Error code: 401 - {'error': {'code': 'AuthenticationError', "
                 "'message': 'The API key is invalid.'}}"),
    ("没配 Key", "未配置 SEEDREAM_API_KEY"),
    ("超时", "HTTPSConnectionPool: Read timed out. (read timeout=120)"),
    ("未知", "something totally unexpected"),
    ("空", ""),
]

print("=" * 72)
print("A. describe_failure —— 给人看的那句")
print("=" * 72)
for label, raw in CASES:
    print(f"\n[{label}]\n  -> {sis.describe_failure(raw)}")

print()
print("=" * 72)
print("B. _failure_with_detail —— 异常文本（进日志）")
print("=" * 72)
rich = sis._failure_with_detail(CASES[0][1])
print(f"  {rich[:200]}…")
assert "SetLimitExceeded" in rich, "服务端原文丢了"
assert "Request id" in rich, "Request id 丢了"

print()
print("=" * 72)
print("C. _degraded_reason —— 真正落库给前端的（必须干净）")
print("=" * 72)
for label, raw in CASES:
    out = _degraded_reason({"degraded": True, "reason": sis._failure_with_detail(raw) if raw else raw})
    print(f"  [{label}] -> {out[:110]}")
    assert "Request id" not in out, f"{label}: Request id 漏给用户了"
    assert "Error code" not in out, f"{label}: SDK 原文漏给用户了"

print()
print("D. 有图时 degraded_reason 必须为空串")
print(f"  -> {_degraded_reason({'degraded': False, 'reason': 'whatever'})!r}")
assert _degraded_reason({"degraded": False, "reason": "whatever"}) == ""

print()
print("全部断言通过 ✅")
