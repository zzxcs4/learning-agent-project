import sys
from pathlib import Path
from loguru import logger

from app.core.settings import settings

def _default_log_levels_by_env(app_env: str) -> tuple[str, str, str]:
    # 开发环境仅保留 "DEBUG(调试)", "WARNING(警告)", "ERROR(错误)"
    if app_env == "development":
        return ("DEBUG", "WARNING", "ERROR")
    # 生产环境仅保留 WARNING+/ERROR
    return ("WARNING", "ERROR", "ERROR")


def _resolve_log_dir() -> Path:
    """日志目录：`settings.log_dir` 非空则以它为准（相对路径按项目根解析），否则用项目根/logs。

    默认值刻意保持"项目根/logs"不变——绝大多数环境不需要配 `LOG_DIR`。
    可配的意义在于：容器里挂只读代码盘、或验收脚本要在受限权限下 `import app` 时，
    可以不写项目目录而把日志指到别处。
    """
    project_root = Path(__file__).resolve().parents[2]
    raw = str(getattr(settings, "log_dir", "") or "").strip()
    if not raw:
        return project_root / "logs"
    candidate = Path(raw)
    return candidate if candidate.is_absolute() else project_root / candidate


def _build_log_path(log_dir: Path, filename: str) -> str:
    """构建日志文件的完整路径（默认写入项目 logs 目录，可由 `LOG_DIR` 覆盖）。"""
    target = log_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    return str(target)


def setup_loguru():
    logger.remove()  # 移除 Loguru 的默认设置，完全自定义日志输出

    # 根据 APP_ENV 确定默认日志级别
    app_env = settings.app_env
    # 根据应用环境获取默认日志级别 (控制台, 主文件, 错误文件)
    console_log_level, main_file_log_level, error_file_log_level = _default_log_levels_by_env(app_env)

    # 配置日志存储目录
    log_dir = _resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)

    # 1. 添加控制台 handler。
    logger.add(
        sys.stderr,
        level=console_log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=app_env != "production",
    )

    # 2. 添加主日志文件 handler（级别由 main_file_log_level 决定）。
    main_log_file_path = _build_log_path(log_dir, "app.log")
    logger.add(
        main_log_file_path,
        level=main_file_log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="7 days",
        compression="zip",
    )

    # 3. 添加错误日志文件 handler（级别由 error_file_log_level 决定）。
    error_log_file_path = _build_log_path(log_dir, "app_errors.log")
    logger.add(
        error_log_file_path,
        level=error_file_log_level,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} - {message}",
        rotation="10 MB",
        retention="30 days",
        compression="zip",
    )

    return logger


# 在模块加载时执行一次日志配置
main_logger = setup_loguru()
