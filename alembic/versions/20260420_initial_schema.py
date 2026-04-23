"""Initial S1 schema — item, run, apicall.

Revision ID: 20260420_initial_schema
Revises:
Create Date: 2026-04-20

Creates the three tables ``init_s1`` creates in code (``item``, ``run``,
``apicall``) as they existed at the end of Phase 1 (shared
infrastructure). The Stage 01 classifier columns (``classification``
and ``needs_review``) are deliberately *absent* here — they come in
the next migration (``20260422_classifier_columns``), which now lists
this file as its ``down_revision``.

The schema here mirrors the state that a fresh ``init_s1()`` produces
when only the scaffolding code (issues #1 + #2) has landed, so running
``alembic upgrade head`` from an empty DB reaches the same shape as
``init_s1()`` on current main.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260420_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "item",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("source_path", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "has_text", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("detected_doi", sa.String(), nullable=True),
        sa.Column(
            "ocr_failed", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("zotero_item_key", sa.String(), nullable=True),
        sa.Column("import_route", sa.String(), nullable=True),
        sa.Column(
            "stage_completed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "in_quarantine",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.Column("metadata_json", sa.String(), nullable=True),
        sa.Column("tags_json", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "run",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("stage", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column(
            "items_processed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "items_failed", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "cost_usd",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="running"
        ),
    )
    op.create_table(
        "apicall",
        sa.Column(
            "id",
            sa.Integer(),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column(
            "run_id",
            sa.Integer(),
            sa.ForeignKey("run.id"),
            nullable=False,
        ),
        sa.Column("service", sa.String(), nullable=False),
        sa.Column(
            "cost_usd",
            sa.Float(),
            nullable=False,
            server_default="0.0",
        ),
        sa.Column(
            "duration_ms", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "status", sa.String(), nullable=False, server_default="success"
        ),
        sa.Column(
            "item_id",
            sa.String(),
            sa.ForeignKey("item.id"),
            nullable=True,
        ),
        sa.Column("timestamp", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("apicall")
    op.drop_table("run")
    op.drop_table("item")
