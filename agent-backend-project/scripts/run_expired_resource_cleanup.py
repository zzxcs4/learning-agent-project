"""每天凌晨 3 点清理过期资源的调度脚本。"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.logging_config import main_logger
from app.db.database import SessionLocal
from app.services import ResourceCleanupService


def _next_run_delay_seconds(target_hour: int, target_minute: int, timezone_name: str) -> int:
    """计算距离下一次调度执行的秒数。"""
    now = datetime.now(ZoneInfo(timezone_name))
    next_run = now.replace(hour=target_hour, minute=target_minute, second=0, microsecond=0)
    if next_run <= now:
        next_run = next_run + timedelta(days=1)
    return int((next_run - now).total_seconds())


def _run_cleanup_once() -> None:
    """执行一次过期资源清理。"""
    db = SessionLocal()
    try:
        summary = ResourceCleanupService.cleanup_expired_resources(db=db)
        main_logger.info(
            "过期资源清理完成: deleted={}, failed={}",
            summary["deleted"],
            summary["failed"],
        )
    finally:
        db.close()


def main() -> None:
    """脚本入口：默认每天 03:00 执行，可通过 --once 立即执行一次。"""
    parser = argparse.ArgumentParser(description="定时清理过期资源")
    parser.add_argument("--once", action="store_true", help="立即执行一次后退出")
    parser.add_argument("--hour", type=int, default=3, help="每天执行小时，默认 3")
    parser.add_argument("--minute", type=int, default=0, help="每天执行分钟，默认 0")
    parser.add_argument("--timezone", type=str, default="Asia/Shanghai", help="时区，默认 Asia/Shanghai")
    args = parser.parse_args()

    if args.once:
        _run_cleanup_once()
        return

    main_logger.info(
        "启动过期资源定时清理任务: schedule={:02d}:{:02d}, timezone={}",
        args.hour,
        args.minute,
        args.timezone,
    )
    while True:
        sleep_seconds = _next_run_delay_seconds(args.hour, args.minute, args.timezone)
        main_logger.info("下一次清理将在 {} 秒后执行", sleep_seconds)
        time.sleep(max(sleep_seconds, 1))
        _run_cleanup_once()


if __name__ == "__main__":
    main()
