"""对齐 knowledge_bases.quiz_core 的列注释（历史遗留漂移）

`alembic check` 报这条漂移：模型里的注释是"出题知识库（考纲）：从原始资料提炼出的知识边界…"，
而库里还是更早那版"出题知识库：从原始资料提炼出的可考要点清单 JSON 数组…"。
两者只是**注释文案**不同，列类型/可空性都一致，功能毫无影响——但它会让 `alembic check`
永远 FAILED，等于把"模型与库是否一致"这道闸门废掉了。顺手对齐。

Revision ID: 5c2e7b9f4a13
Revises: 3f8a1c6d2e04
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = "5c2e7b9f4a13"
down_revision: str | None = "3f8a1c6d2e04"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 app/db/models.py::KnowledgeBase.quiz_core 的 comment 逐字一致，别再手改其中一边。
NEW_COMMENT = (
    "出题知识库（考纲）：从原始资料提炼出的知识边界，不是摘要。"
    "结构 {title, frequently_tested[], followup_paths[], "
    "sections[{heading, topics[], source_ref{resource_id,chunk_from,chunk_to}, "
    "asked_count, last_asked_at}]}；不被掌握度引用"
)
OLD_COMMENT = (
    "出题知识库：从原始资料提炼出的可考要点清单 JSON 数组（不被掌握度引用）："
    '[{"point","summary","source_ref","asked_count","last_asked_at"}]'
)


def _set_comment(comment: str) -> None:
    op.alter_column(
        "knowledge_bases",
        "quiz_core",
        existing_type=mysql.JSON(),
        existing_nullable=True,
        comment=comment,
    )


def upgrade() -> None:
    _set_comment(NEW_COMMENT)


def downgrade() -> None:
    _set_comment(OLD_COMMENT)
