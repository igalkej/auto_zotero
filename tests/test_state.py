"""Tests for `zotai.state` — schema creation + basic CRUD."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import inspect
from sqlmodel import Session, select

from zotai.state import (
    ApiCall,
    Candidate,
    Feed,
    Item,
    PersistentQuery,
    Run,
    S1_TABLES,
    S2_TABLES,
    TriageMetric,
    init_s1,
    init_s2,
    make_s1_engine,
    make_s2_engine,
)


def test_s1_init_creates_only_s1_tables(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    engine = make_s1_engine(str(db))
    init_s1(engine)

    names = set(inspect(engine).get_table_names())
    expected = {t.name for t in S1_TABLES}
    assert expected.issubset(names)
    for s2_table in S2_TABLES:
        assert s2_table.name not in names


def test_s2_init_creates_only_s2_tables(tmp_path: Path) -> None:
    db = tmp_path / "candidates.db"
    engine = make_s2_engine(str(db))
    init_s2(engine)

    names = set(inspect(engine).get_table_names())
    expected = {t.name for t in S2_TABLES}
    assert expected.issubset(names)
    for s1_table in S1_TABLES:
        assert s1_table.name not in names


def test_s1_item_roundtrip(tmp_path: Path) -> None:
    engine = make_s1_engine(str(tmp_path / "state.db"))
    init_s1(engine)

    with Session(engine) as session:
        run = Run(stage=1)
        session.add(run)
        session.commit()
        session.refresh(run)

        item = Item(id="a" * 64, source_path="/tmp/x.pdf", size_bytes=1234)
        session.add(item)
        session.commit()

        call = ApiCall(
            run_id=run.id or 0,
            service="openai",
            cost_usd=0.001,
            duration_ms=42,
        )
        session.add(call)
        session.commit()

        fetched = session.exec(select(Item).where(Item.id == item.id)).one()
        assert fetched.source_path == "/tmp/x.pdf"
        assert fetched.stage_completed == 0
        assert fetched.created_at.tzinfo is not None


def test_s2_candidate_roundtrip(tmp_path: Path) -> None:
    engine = make_s2_engine(str(tmp_path / "candidates.db"))
    init_s2(engine)

    with Session(engine) as session:
        feed = Feed(id="aer", name="AER", rss_url="https://example.com/rss")
        session.add(feed)
        session.commit()

        candidate = Candidate(
            id="deadbeef",
            source_feed_id=feed.id,
            title="Test paper",
            authors_json="[]",
            venue="AER",
            published_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        session.add(candidate)

        query = PersistentQuery(query_text="fiscal multiplier")
        session.add(query)

        metric = TriageMetric(
            week_start=date(2026, 1, 5),
            candidates_shown=10,
            candidates_accepted=5,
            candidates_rejected=3,
            candidates_deferred=2,
            precision_observed=5 / 8,
        )
        session.add(metric)
        session.commit()

        fetched = session.exec(
            select(Candidate).where(Candidate.id == candidate.id)
        ).one()
        assert fetched.title == "Test paper"
        assert fetched.status == "pending"
