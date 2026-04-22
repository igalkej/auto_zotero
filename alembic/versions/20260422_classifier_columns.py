"""add Item.classification + Item.needs_review (Stage 01 classifier)

Revision ID: 20260422_classifier_columns
Revises:
Create Date: 2026-04-22

Introduces the two columns that back the Stage 01 academic / non-academic
classifier (plan_01 §3.1). Backfills existing rows with the conservative
defaults so this migration is safe on DBs created by earlier
``init_s1()`` calls.

The eventual initial-schema migration from Phase 8 (issue #9) should
land as a new revision with ``down_revision=None`` and this file should
then be re-parented to it.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260422_classifier_columns"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("item") as batch:
        batch.add_column(
            sa.Column(
                "classification",
                sa.String(),
                nullable=False,
                server_default="academic",
            )
        )
        batch.add_column(
            sa.Column(
                "needs_review",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("item") as batch:
        batch.drop_column("needs_review")
        batch.drop_column("classification")
