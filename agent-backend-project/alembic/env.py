"""Alembic 迁移环境。

两条约定：

1. **连接串不写在 `alembic.ini`**，而是从 `app.core.settings` 读，与运行时共用
   `.env.<APP_ENV>` 的一份配置（原 `alembic.ini` 里硬编码了 root 明文密码，
   而且库名与 `.env` 不一致）。
2. **目标元数据 = `Base.metadata`**，所以必须 `import app.db.models` 把全部模型
   注册进 metadata —— 少 import 一个模型，autogenerate 就会认为那张表该被删掉。
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.settings import settings
from app.db.base import Base
import app.db.models  # noqa: F401  仅为把全部模型注册到 Base.metadata

config = context.config

# ConfigParser 会把 % 当插值符，URL 里的 % 需要转义后再交给 alembic。
config.set_main_option("sqlalchemy.url", settings.mysql_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 打开列类型比对，否则"改了列类型"这类迁移会被静默漏掉
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
