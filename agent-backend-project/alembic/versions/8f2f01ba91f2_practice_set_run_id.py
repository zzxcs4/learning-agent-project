"""practice_sets 增加 run_id 列，关联工作流 run

背景（第 27 轮）：练习记录（`practice_sets`）此前没有与 `workflow_runs` 的关联键，
导致两件事做不了：

1. **练习记录里回看总评价 / 反思**：整套的 `review` 和 `reflection` 只活在
   `workflow_runs.state_json`，而详情页只有 practice_sets + attempts，没有 run_id
   就查不到那份 state。
2. **归档工具定位"某一场练习"**：`list_practice_history` 工具因为列表项没有 run_id，
   模型只能猜；加了 run_id 就能对任意一场（不止最近一次的）练习进行归档。

做法：

- 新增 `practice_sets.run_id`，存出题时对应 workflow run 的 run_id。
- **不设外键**（与 `notes.source_run_id` 同一处理口径）：run_id 只是字符串索引，
  避免把 practice_sets 和 workflow_runs 的生命周期绑死（workflow run 随时可能
  被清理/重建，练习记录是更持久的用户资产）。
- 存量数据不回填（老练习本来就没有独立 run，回填不出可靠的 run_id）。

Revision ID: 8f2f01ba91f2
Revises: 5c2e7b9f4a13
Create Date: 2026-10-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f2f01ba91f2"
down_revision: str | None = "5c2e7b9f4a13"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "practice_sets",
        sa.Column(
            "run_id",
            sa.String(length=64),
            nullable=True,
            comment="关联的学习测评 workflow run_id（无外键）。用于回看该场练习的 review/reflection，也供归档工具定位",
        ),
    )
    op.create_index("ix_practice_sets_run_id", "practice_sets", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_practice_sets_run_id", table_name="practice_sets")
    op.drop_column("practice_sets", "run_id")
