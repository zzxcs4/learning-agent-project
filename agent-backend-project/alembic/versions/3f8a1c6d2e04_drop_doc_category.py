"""删除 resources.doc_category（取消「文档业务分类」这个维度）

为什么删：这一列从上线起就**没有任何功能**——写入方是上传时的三选一（简历/学习资料/通用文档），
读取方只有 `search_similar_chunks(doc_category=...)`，而那个参数从来没有调用方传过。
它既不影响切分、向量化、检索，也不影响考纲提炼，纯粹是给用户增加了一步"必须选一个"的操作。
经确认后整维删除，而不是保留成"只读标签"。

删掉的东西（同一批改动里一起清）：

- 本列与它的索引 `idx_user_id_doc_category`。注意那个索引同时是 `resources.user_id`
  外键的**唯一支撑索引**，所以本迁移会先补一个独立的 `ix_resources_user_id` 再删它
  （否则 MySQL 报 errno 1553 拒绝删除）；
- `app/rag/core.py` 里的 `infer_doc_category()` 与内容关键字计分表（连同 `CATEGORY_*` 常量）；
- Qdrant payload 里的 `doc_category` 键（新写入的点不再带这个键）；
- `prompt_template_service.AGENT_CONFIG` 里的 `resume_parser`（那条配置没有调用方，
  且它的存在理由就是"按简历分类路由"）。

**存量数据**：列删掉后，库里已有的 12 个 Qdrant 点仍带着旧的 `doc_category` 键——
这无害（已无任何过滤器读它），只是冗余。要一并清掉可以重灌向量，或等这些资料重新入库时自然消失。

Revision ID: 3f8a1c6d2e04
Revises: 9b1d4c7e5a20
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3f8a1c6d2e04"
down_revision: str | None = "9b1d4c7e5a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _index_exists(name: str) -> bool:
    """查 information_schema 看索引是否真的在。

    为什么不用 try/except 无脑兜底：那把"表被锁 / 权限不足"这类真错误也一起吞了，
    迁移会以"成功"结束却留下半成品状态。这里只对"索引不存在"这一种情况放行。

    ⚠️ 离线模式（`alembic upgrade head --sql`）拿不到真连接，`sa.inspect()` 会抛
    `NoInspectionAvailable`——此时按"存在"处理，直接生成删除语句，保证预演可用。
    """
    if op.get_context().as_sql:
        return True
    inspector = sa.inspect(op.get_bind())
    return name in {idx["name"] for idx in inspector.get_indexes("resources")}


def upgrade() -> None:
    # ⚠️ 顺序不能反：`idx_user_id_doc_category` 是 `resources.user_id` 外键**唯一的支撑索引**，
    # 直接删会被 MySQL 拒绝（errno 1553 Cannot drop index ... needed in a foreign key constraint）。
    # 所以先给 user_id 补一个独立索引，再删复合索引。
    if not _index_exists("ix_resources_user_id"):
        op.create_index("ix_resources_user_id", "resources", ["user_id"])
    if _index_exists("idx_user_id_doc_category"):
        op.drop_index("idx_user_id_doc_category", table_name="resources")
    op.drop_column("resources", "doc_category")


def downgrade() -> None:
    op.add_column(
        "resources",
        sa.Column(
            "doc_category",
            sa.String(length=32),
            nullable=True,
            comment="文档业务分类：resume/study_material/general（仅文件类型有意义）",
        ),
    )
    op.create_index("idx_user_id_doc_category", "resources", ["user_id", "doc_category"])
    if _index_exists("ix_resources_user_id"):
        op.drop_index("ix_resources_user_id", table_name="resources")
