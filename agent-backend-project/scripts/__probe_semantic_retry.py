"""临时探针：验证语义分类器的「有限重试 + 冷却 + 自动恢复」。用完即删。"""

from __future__ import annotations

import os

os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

from app.utils.intent_classifier import SemanticIntentClassifier  # noqa: E402

CHECKS: list[tuple[str, bool]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok)))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


class FakeModel:
    pass


def make_cls(behaviour: list) -> SemanticIntentClassifier:
    """behaviour: 每次调用 _load_model 时的行为，'ok' 或 'fail'。"""
    cls = SemanticIntentClassifier("fake-model")
    calls = {"n": 0}

    def fake_load():
        idx = calls["n"]
        calls["n"] += 1
        action = behaviour[idx] if idx < len(behaviour) else behaviour[-1]
        if action == "fail":
            raise PermissionError("fake download denied")
        return FakeModel()

    cls._load_model = fake_load  # type: ignore[method-assign]
    cls.calls = calls  # type: ignore[attr-defined]
    return cls


print("[1] 一直失败：前 3 次每次真试，之后进冷却不再试")
cls = make_cls(["fail"])
for i in range(6):
    cls._get_model()
check("共尝试 3 次后停止", cls.calls["n"] == 3, f"实际 {cls.calls['n']} 次")
check("标记为不可用", cls._load_failed is True)

print()
print("[2] 冷却期结束后自动重试（不需要重启进程）")
cls = make_cls(["fail"])
for _ in range(3):
    cls._get_model()
check("已进冷却", cls._load_failed is True)
cls._last_failure_at -= cls.RETRY_COOLDOWN_SECONDS + 1  # 快进冷却
cls._get_model()
check("冷却后确实又试了一次", cls.calls["n"] == 4, f"实际 {cls.calls['n']} 次")

print()
print("[3] 先失败后恢复：冷却结束后能拿到模型")
cls = make_cls(["fail", "fail", "fail", "ok"])
for _ in range(3):
    cls._get_model()
check("此时无模型", cls._get_model() is None)
cls._last_failure_at -= cls.RETRY_COOLDOWN_SECONDS + 1
got = cls._get_model()
check("冷却后恢复成功拿到模型", got is not None)
check("恢复后清空失败标记", cls._load_failed is False)

print()
print("[4] 恢复后不再重复尝试（模型已缓存）")
before = cls.calls["n"]
cls._get_model()
cls._get_model()
check("命中缓存不再加载", cls.calls["n"] == before, f"{before} -> {cls.calls['n']}")

failed = [n for n, ok in CHECKS if not ok]
print()
print(f"断言 {len(CHECKS)}，失败 {len(failed)}")
for n in failed:
    print(f"  FAILED: {n}")
raise SystemExit(1 if failed else 0)
