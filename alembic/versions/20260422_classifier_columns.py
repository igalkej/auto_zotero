"""add Item.classification + Item.needs_review (Stage 01 classifier)

Revision ID: 20260422_classifier_columns
Revises: 20260420_initial_schema
Create Date: 2026-04-22

Introduces the two columns that back the Stage 01 academic / non-academic
classifier (plan_01 §3.1). Backfills existing rows with the conservative
defaults so this migration is safe on DBs created by earlier
``init_s1()`` calls.

Re-parented onto ``20260420_initial_schema`` (Phase 8 / issue #9), which
creates the rest of the S1 tables. Running ``alembic upgrade head`` on
an empty DB now reaches the same shape as ``init_s1()``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260422_classifier_columns"
down_revision: str | None = "20260420_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
