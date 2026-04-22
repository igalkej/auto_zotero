"""SQLModel schemas for `state.db` (S1) and `candidates.db` (S2).

Both databases share `SQLModel.metadata` because they are schema-disjoint:
`state.db` only ever gets the S1 tables, `candidates.db` only the S2 tables.
Filtering is enforced via the `S1_TABLES` / `S2_TABLES` module-level tuples
and the `init_s1` / `init_s2` helpers, which pass ``tables=[...]`` to
``create_all``.

Alembic's ``target_metadata`` in ``alembic/env.py`` is ``state.metadata``
(i.e. the same global ``SQLModel.metadata``); migrations are authored against
the S1 schema. S2 tables are created on dashboard startup with ``init_s2``
and evolve via code rather than alembic in v1 (see plan_02 §5).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import DateTime as SADateTime
from sqlalchemy import MetaData, Table, TypeDecorator
from sqlalchemy.engine import Engine
from sqlmodel import Field, SQLModel, create_engine


def _utc_now() -> datetime:
    """Timezone-aware UTC timestamp used as default for `created_at` / `updated_at`."""
    return datetime.now(tz=UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite-safe UTC-aware datetime column.

    SQLite has no native datetime type and SQLAlchemy stores ``datetime``
    values as ISO-8601 text *without* timezone information, so a
    roundtrip strips the ``tzinfo`` we set on ``default_factory``. This
    decorator re-attaches UTC on read and coerces naive binds to UTC on
    write, so every datetime column behaves as if SQLite supported
    timezone-aware datetimes natively.
    """

    impl = SADateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value

    def process_result_value(
        self, value: datetime | None, dialect: Any
    ) -> datetime | None:
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


# ─── S1: state.db ───────────────────────────────────────────────────────────


class Item(SQLModel, table=True):
    """A single PDF tracked through the S1 pipeline.

    `id` is the SHA-256 of the PDF bytes, enforcing deduplication at
    inventory time (Stage 01 in plan_01).
    """

    id: str = Field(primary_key=True)
    source_path: str
    size_bytes: int
    has_text: bool = False
    detected_doi: str | None = None
    # Stage 01 classifier (plan_01 §3.1). Rejected PDFs never land in the
    # DB — only 'academic' rows exist here. ``needs_review`` flags items
    # the LLM gate was unsure about so Stage 06 surfaces them.
    classification: str = "academic"
    needs_review: bool = False
    ocr_failed: bool = False
    zotero_item_key: str | None = None
    import_route: str | None = None  # 'A' | 'C'
    stage_completed: int = 0
    in_quarantine: bool = False
    last_error: str | None = None
    metadata_json: str | None = None
    tags_json: str | None = None
    created_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)


class Run(SQLModel, table=True):
    """A single execution of an S1 stage, used for metrics + resume semantics."""

    id: int | None = Field(default=None, primary_key=True)
    stage: int
    started_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    items_processed: int = 0
    items_failed: int = 0
    cost_usd: float = 0.0
    status: str = "running"  # 'running' | 'succeeded' | 'failed' | 'aborted'


class ApiCall(SQLModel, table=True):
    """Per-call observability for external services. Used for budget enforcement."""

    id: int | None = Field(default=None, primary_key=True)
    run_id: int = Field(foreign_key="run.id")
    service: str  # 'openalex' | 'semantic_scholar' | 'openai' | 'zotero'
    cost_usd: float = 0.0
    duration_ms: int = 0
    status: str = "success"  # 'success' | 'error' | 'rate_limited'
    item_id: str | None = Field(default=None, foreign_key="item.id")
    timestamp: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)


# ─── S2: candidates.db ──────────────────────────────────────────────────────


class Candidate(SQLModel, table=True):
    """A paper surfaced by the S2 worker and pending triage by the user."""

    id: str = Field(primary_key=True)  # hash of DOI or URL
    source_feed_id: str = Field(foreign_key="feed.id")
    doi: str | None = None
    title: str
    authors_json: str
    abstract: str | None = None
    venue: str
    published_at: datetime = Field(sa_type=UTCDateTime)
    url: str | None = None

    # Scoring (each in [0, 1])
    score_tags: float = 0.0
    score_semantic: float = 0.0
    score_queries: float = 0.0
    score_composite: float = 0.0
    scoring_explanation: str = "{}"

    # Triage
    status: str = "pending"  # 'pending' | 'accepted' | 'rejected' | 'deferred'
    decided_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    decided_by: str | None = None
    decision_note: str | None = None

    # Zotero integration — set once push succeeds
    zotero_item_key: str | None = None
    pushed_at: datetime | None = Field(default=None, sa_type=UTCDateTime)

    created_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)
    updated_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)


class Feed(SQLModel, table=True):
    """A journal RSS feed configured in `config/feeds.yaml` or via the dashboard."""

    id: str = Field(primary_key=True)  # slug
    name: str
    rss_url: str
    issn: str | None = None
    active: bool = True
    last_fetched_at: datetime | None = Field(default=None, sa_type=UTCDateTime)
    last_fetch_status: str | None = None
    items_fetched_total: int = 0


class PersistentQuery(SQLModel, table=True):
    """A user-authored query that contributes to the composite score of every candidate."""

    id: int | None = Field(default=None, primary_key=True)
    query_text: str
    active: bool = True
    weight: float = 1.0
    created_at: datetime = Field(default_factory=_utc_now, sa_type=UTCDateTime)


class TriageMetric(SQLModel, table=True):
    """Weekly precision/volume snapshot used on the dashboard's metrics page."""

    id: int | None = Field(default=None, primary_key=True)
    week_start: date
    candidates_shown: int = 0
    candidates_accepted: int = 0
    candidates_rejected: int = 0
    candidates_deferred: int = 0
    precision_observed: float = 0.0


# ─── Table groupings + engine helpers ───────────────────────────────────────

metadata: MetaData = SQLModel.metadata

S1_TABLES: tuple[Table, ...] = (
    Item.__table__,  # type: ignore[attr-defined]
    Run.__table__,  # type: ignore[attr-defined]
    ApiCall.__table__,  # type: ignore[attr-defined]
)

S2_TABLES: tuple[Table, ...] = (
    Candidate.__table__,  # type: ignore[attr-defined]
    Feed.__table__,  # type: ignore[attr-defined]
    PersistentQuery.__table__,  # type: ignore[attr-defined]
    TriageMetric.__table__,  # type: ignore[attr-defined]
)


def _sqlite_url(path: str) -> str:
    """Normalize a filesystem path into a SQLite URL understood by SQLAlchemy."""
    if path.startswith("sqlite"):
        return path
    return f"sqlite:///{path}"


def make_s1_engine(db_path: str, echo: bool = False) -> Engine:
    """Return a SQLAlchemy engine bound to the S1 database (typically `state.db`)."""
    return create_engine(_sqlite_url(db_path), echo=echo, connect_args={"check_same_thread": False})


def make_s2_engine(db_path: str, echo: bool = False) -> Engine:
    """Return a SQLAlchemy engine bound to the S2 database (typically `candidates.db`)."""
    return create_engine(_sqlite_url(db_path), echo=echo, connect_args={"check_same_thread": False})


def init_s1(engine: Engine) -> None:
    """Create S1 tables in the given engine (idempotent)."""
    metadata.create_all(engine, tables=list(S1_TABLES))


def init_s2(engine: Engine) -> None:
    """Create S2 tables in the given engine (idempotent)."""
    metadata.create_all(engine, tables=list(S2_TABLES))


__all__ = [
    "S1_TABLES",
    "S2_TABLES",
    "ApiCall",
    "Candidate",
    "Feed",
    "Item",
    "PersistentQuery",
    "Run",
    "TriageMetric",
    "init_s1",
    "init_s2",
    "make_s1_engine",
    "make_s2_engine",
    "metadata",
]
