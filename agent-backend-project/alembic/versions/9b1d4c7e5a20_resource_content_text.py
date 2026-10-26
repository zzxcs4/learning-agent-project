"""resources 增加解析全文列（知识库阅读器用）

新增 2 列：

- `resources.content_text`：入库时解析清洗后的**完整正文**。为什么要单独存一份——
  `knowledge_chunks` 是按字数递归切的、块间有重叠，拼回来会损失排版；
  "打开库里的文档看原文"需要一份完整可读的文本。
- `resources.content_chars`：上面那列的字数，列表页直接读它，避免为了显示字数拉全文。

存量数据：老记录这列为空，阅读器会**用切片拼回**（`app/rag/merge_text.py`，已消除块间重叠），
因此本次迁移不做数据回填——回填有两条路，都比在这里做更合适：

1. 让老资料在下一次上传/重传时自然补上；
2. 跑 `python scripts/backfill_resource_content.py --all`（从已有切片拼回，离线、零成本）。

Revision ID: 9b1d4c7e5a20
Revises: 7c4f9e2a1b3d
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "9b1d4c7e5a20"
down_revision: str | None = "7c4f9e2a1b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "resources",
        sa.Column(
            "content_text",
            mysql.MEDIUMTEXT(),
            nullable=True,
            comment="解析清洗后的全文（入库时落库，供知识库阅读器查看原文；为空时用 knowledge_chunks 拼回）",
        ),
    )
    op.add_column(
        "resources",
        sa.Column(
            "content_chars",
            sa.Integer(),
            nullable=True,
            comment="content_text 字符数（列表页展示，避免拉全文）",
        ),
    )


def downgrade() -> None:
    op.drop_column("resources", "content_chars")
    op.drop_column("resources", "content_text")
