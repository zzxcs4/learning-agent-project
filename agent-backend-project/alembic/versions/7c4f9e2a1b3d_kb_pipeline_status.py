"""知识库构建流水线状态字段

新增 4 列，把"上传后异步构建"的两个阶段变成**可查状态**（用户只看到知识库，
但要能看到"准备中 / 就绪 / 失败"）：

- resources.ingest_status / ingest_error：向量入库（解析 → 切片 → embedding → Qdrant）
- knowledge_bases.quiz_status / quiz_extracted_at：考纲提炼（每 ~5 万字 1 次 LLM）

数据补齐：已有切片的资料视为已入库；已有 quiz_core 的库视为已提炼。

Revision ID: 7c4f9e2a1b3d
Revises: 16a0cd2f9d7f
Create Date: 2026-10-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c4f9e2a1b3d"
down_revision: str | None = "16a0cd2f9d7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "resources",
        sa.Column(
            "ingest_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
            comment="向量入库状态：pending 待处理 / ingesting 进行中 / ready 就绪 / failed 失败",
        ),
    )
    op.add_column(
        "resources",
        sa.Column(
            "ingest_error",
            sa.String(length=500),
            nullable=True,
            comment="向量入库失败原因（截断 500 字）",
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "quiz_status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
            comment="考纲提炼状态：pending 待提炼 / extracting 进行中 / ready 就绪 / empty 无可提炼内容 / failed 失败",
        ),
    )
    op.add_column(
        "knowledge_bases",
        sa.Column(
            "quiz_extracted_at",
            sa.BigInteger(),
            nullable=True,
            comment="最近一次考纲提炼完成时间 Unix 秒",
        ),
    )

    # 数据补齐：不能让"已经入库/已提炼"的老数据显示成 pending
    op.execute(
        "UPDATE resources SET ingest_status = 'ready' "
        "WHERE id IN (SELECT DISTINCT resource_id FROM knowledge_chunks)"
    )
    op.execute(
        "UPDATE knowledge_bases SET quiz_status = 'ready', "
        "quiz_extracted_at = UNIX_TIMESTAMP() WHERE quiz_core IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("knowledge_bases", "quiz_extracted_at")
    op.drop_column("knowledge_bases", "quiz_status")
    op.drop_column("resources", "ingest_error")
    op.drop_column("resources", "ingest_status")
