from sqlalchemy.orm import DeclarativeBase

# 声明式基类：
# 所有 ORM 模型都应继承该基类，用于定义数据库表结构。
class Base(DeclarativeBase):
    pass
