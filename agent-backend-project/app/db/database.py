from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import settings

# pool_pre_ping=True: 每次从连接池取连接时先探活，减少 MySQL 空闲断连问题。
engine = create_engine(settings.mysql_url, pool_pre_ping=True)
# autocommit=False/autoflush=False 是后端 API 常见配置：
# - 由业务层显式 commit，事务边界更清晰
# - 避免 SQLAlchemy 在不必要时自动 flush
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, class_=Session)


def get_db() -> Generator[Session, None, None]:
    # FastAPI 依赖注入函数：
    # 为每个请求创建独立 Session，请求结束后确保关闭连接。
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
