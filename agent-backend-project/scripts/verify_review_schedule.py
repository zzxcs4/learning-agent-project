"""验收：第 3 期 SM-2 复习调度（next_review / ease_factor 滚动 + 画像页今日到期复习）。

对应 `升级规划.md` §八 第 3 期第 3 条的后半（调度算法 + "今日到期复习"）。
零 token 消耗；[2] 需要真 MySQL（临时数据自建自清）：

| 段 | 验的是什么 | 手段 |
|---|---|---|
| [1] | 质量映射 + SM-2 阶梯/失败重置/EF 下限 | 纯函数 |
| [2] | 归并挂点（新建首复/复现重置/缺席进阶）+ 到期查询口径 | 真 MySQL `user_profiles.weak_points` |

用法（在 agent-backend-project 目录下）：

    python scripts/verify_review_schedule.py
    python scripts/verify_review_schedule.py --keep     # 保留临时数据
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete as sql_delete
from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import User, UserProfile
from app.security import hash_password
from app.services.mastery_service import MasteryService
from app.services.review_service import ReviewService

TEMP_PASSWORD = "verify_review_123"

passed = 0
failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ---------------------------------------------------------------------------
# [1] 纯函数：质量映射 + SM-2 滚动
# ---------------------------------------------------------------------------
def verify_pure(today: date) -> None:
    print("\n[1] 质量映射与 SM-2 滚动（纯函数）")

    check("复现 → q=2（与整场分数无关）", ReviewService.quality_for(True, 95.0) == 2)
    check("缺席且 ≥80 → q=5", ReviewService.quality_for(False, 85.0) == 5)
    check("缺席且 60–79 → q=4", ReviewService.quality_for(False, 70.0) == 4)
    check("缺席且 <60 → q=3", ReviewService.quality_for(False, 40.0) == 3)

    entry: dict[str, Any] = {"point": "GIL", "ease_factor": 2.5, "repetition": 0, "interval_days": 0}
    ReviewService.schedule(entry, quality=5, today=today)
    check("第 1 次成功：间隔 1 天", entry["interval_days"] == 1 and entry["next_review"] == (today + timedelta(days=1)).isoformat(), str(entry))
    check("EF 随 q=5 上调到 2.6", entry["ease_factor"] == 2.6, str(entry["ease_factor"]))

    ReviewService.schedule(entry, quality=5, today=today)
    check("第 2 次成功：间隔 6 天", entry["interval_days"] == 6 and entry["repetition"] == 2, str(entry))

    ReviewService.schedule(entry, quality=5, today=today)
    check("第 3 次成功：间隔 = round(6 × 2.8) = 17 天", entry["interval_days"] == 17, f"{entry['interval_days']} / EF={entry['ease_factor']}")

    failed_entry: dict[str, Any] = {"point": "MVCC", "ease_factor": 2.5, "repetition": 3, "interval_days": 30}
    ReviewService.schedule(failed_entry, quality=2, today=today)
    check(
        "失败（q=2）：连击归零、间隔回 1 天",
        failed_entry["repetition"] == 0 and failed_entry["interval_days"] == 1,
        str(failed_entry),
    )
    check("失败 EF 下调且不低于 1.3（2.5 - 1.22 -> 1.3）", failed_entry["ease_factor"] == 1.3, str(failed_entry["ease_factor"]))

    clamped: dict[str, Any] = {"point": "MVCC", "ease_factor": 1.3, "repetition": 0, "interval_days": 0}
    ReviewService.schedule(clamped, quality=0, today=today)
    check("连败不穿透 EF 下限 1.3", clamped["ease_factor"] == 1.3, str(clamped["ease_factor"]))

    fresh: dict[str, Any] = {"point": "新弱点", "ease_factor": 2.5}
    ReviewService.schedule_new(fresh, today=today)
    check(
        "新建首复排明天（learn today, review tomorrow）",
        fresh["next_review"] == (today + timedelta(days=1)).isoformat() and fresh["repetition"] == 0,
        str(fresh),
    )


# ---------------------------------------------------------------------------
# [2] 归并挂点 + 到期查询（真 MySQL）
# ---------------------------------------------------------------------------
def verify_integration(db: Any, user_id: int, today: date) -> None:
    print("\n[2] 归并挂点与到期查询（真 MySQL user_profiles.weak_points）")

    summary = MasteryService.merge_weak_points(
        db,
        user_id=user_id,
        knowledge_base_id=3,
        points=["GIL 与多线程的关系"],
        practice_set_id=910001,
        practice_score=50.0,
    )
    check("新建弱点正常入账", summary["added"] == 1, str(summary))

    profile = db.scalar(select(UserProfile).where(UserProfile.user_id == user_id))
    entry = profile.weak_points[0]
    check(
        "新建即排程：明天首复、EF 默认",
        entry["next_review"] == (today + timedelta(days=1)).isoformat() and entry["ease_factor"] == 2.5,
        str({k: entry.get(k) for k in ("next_review", "ease_factor", "interval_days", "repetition")}),
    )
    check("明天才到期 → 今天到期视图为空", ReviewService.due_reviews(db, user_id=user_id, today=today)["total"] == 0)

    # 时间快进：把应复习日期拨回昨天，模拟"到期没复习"
    entry["next_review"] = (today - timedelta(days=1)).isoformat()
    # SQLAlchemy 需要新对象才稳定侦测到 JSON 列变更（见 mastery_service 同款注释）
    profile.weak_points = list(profile.weak_points)
    db.commit()

    due = ReviewService.due_reviews(db, user_id=user_id, today=today)
    check("到期后出现在今日复习视图", due["total"] == 1 and due["items"][0]["point"] == "GIL 与多线程的关系", str(due["total"]))

    # 复现（同一条弱点又犯了）→ q=2：EF 下调夹到 1.3、间隔重置、排明天
    # （merge_weak_points 自身不 commit——提交属于 update_after_practice 的"一次 commit"契约，
    #   验收脚本直接调它时必须自己 commit，否则 db.refresh 会把挂起改动刷掉）
    MasteryService.merge_weak_points(
        db,
        user_id=user_id,
        knowledge_base_id=3,
        points=["GIL与多线程的关系"],  # 归一化文本相同 → 必然命中同一条（不依赖向量）
        practice_set_id=910002,
        practice_score=70.0,
    )
    db.commit()
    db.refresh(profile)
    entry = profile.weak_points[0]
    check(
        "复现 → 失败分支：连击归零、EF 夹到 1.3、明天再复习",
        entry["repetition"] == 0
        and entry["interval_days"] == 1
        and entry["ease_factor"] == 1.3
        and entry["next_review"] == (today + timedelta(days=1)).isoformat()
        and entry["times_seen"] == 2,
        str({k: entry.get(k) for k in ("times_seen", "ease_factor", "repetition", "interval_days")}),
    )

    # 缺席 + 高分（85）→ q=5：EF 上调、进阶连击
    MasteryService.merge_weak_points(
        db,
        user_id=user_id,
        knowledge_base_id=3,
        points=["MVCC 与隔离级别"],
        practice_set_id=910003,
        practice_score=85.0,
    )
    db.commit()
    db.refresh(profile)
    gil = next(item for item in profile.weak_points if "GIL" in str(item.get("point")))
    check(
        "缺席 + 高分 → 成功分支：EF 1.3 + 0.1 = 1.4、连击 1",
        gil["ease_factor"] == 1.4 and gil["repetition"] == 1 and gil["absent_streak"] == 1,
        str({k: gil.get(k) for k in ("ease_factor", "repetition", "absent_streak")}),
    )
    check("新弱点 MVCC 也是明天首复", next(item for item in profile.weak_points if "MVCC" in str(item.get("point")))["next_review"] == (today + timedelta(days=1)).isoformat())

    # 到期视图的排除口径：毕业条目不进（否则会永久占满视图）
    profile.weak_points = profile.weak_points + [
        {"point": "已归档的旧弱点", "knowledge_base_id": 3, "times_seen": 3, "archived": True,
         "next_review": (today - timedelta(days=3)).isoformat(), "ease_factor": 2.5, "axis": "knowledge"},
        {"point": "已改进的旧弱点", "knowledge_base_id": 3, "times_seen": 2, "improved": True,
         "next_review": (today - timedelta(days=2)).isoformat(), "ease_factor": 2.5, "axis": "knowledge"},
    ]
    db.commit()
    due = ReviewService.due_reviews(db, user_id=user_id, today=today)
    check("归档/已改进条目不进到期视图（口径见 review_service 模块注释）", due["total"] == 0, str(due["total"]))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="第 3 期 SM-2 复习调度验收")
    parser.add_argument("--keep", action="store_true", help="跑完不清理临时数据")
    args = parser.parse_args()

    db = SessionLocal()
    suffix = str(int(time.time()))[-8:]
    username = f"vrev{suffix}"
    user_id = None
    try:
        user = User(username=username, password=hash_password(TEMP_PASSWORD))
        db.add(user)
        db.commit()
        user_id = user.id
        print(f"临时数据：user={user_id}（{username}）")

        today = date.today()
        verify_pure(today)
        verify_integration(db, user_id, today)

        print(f"\n结果：{passed} 通过 / {failed} 失败")
        return 1 if failed else 0
    finally:
        if args.keep:
            print(f"（--keep）保留临时数据：user={user_id}")
        else:
            try:
                if user_id:
                    db.execute(sql_delete(UserProfile).where(UserProfile.user_id == user_id))
                    db.execute(sql_delete(User).where(User.id == user_id))
                db.commit()
                print(f"已清理临时数据：user={user_id}")
            except Exception as exc:  # noqa: BLE001
                db.rollback()
                print(f"清理失败（可手工删）：{exc}")
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
