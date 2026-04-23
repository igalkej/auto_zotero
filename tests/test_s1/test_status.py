"""Tests for :mod:`zotai.s1.status`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr
from sqlmodel import Session

from zotai.config import OpenAISettings, PathSettings, Settings, ZoteroSettings
from zotai.s1.status import compute_status, format_status
from zotai.state import Item, Run, init_s1, make_s1_engine


def _settings(tmp_path: Path, *, with_openai: bool = False) -> Settings:
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="42"),
        openai=OpenAISettings(api_key=SecretStr("sk-test") if with_openai else SecretStr("")),
    )


def _mk_item(
    sha: str,
    *,
    stage_completed: int = 0,
    zotero_item_key: str | None = None,
    tags_json: str | None = None,
    in_quarantine: bool = False,
    needs_review: bool = False,
    last_error: str | None = None,
) -> Item:
    return Item(
        id=sha,
        source_path=f"/data/{sha}.pdf",
        size_bytes=1,
        has_text=True,
        classification="academic",
        stage_completed=stage_completed,
        zotero_item_key=zotero_item_key,
        tags_json=tags_json,
        in_quarantine=in_quarantine,
        needs_review=needs_review,
        last_error=last_error,
    )


def test_compute_status_on_empty_db_returns_zero_counters(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    snapshot = compute_status(
        settings=settings,
        now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)],
    )
    assert snapshot.total_items == 0
    assert all(row.count == 0 for row in snapshot.items_by_stage)
    assert snapshot.cost_total_usd == 0.0
    assert snapshot.last_run_at is None
    # state_db_exists reflects the filesystem *before* compute_status runs
    # init_s1, so the first ever call on a fresh tmpdir reports False.
    assert snapshot.state_db_exists is False
    # Every stage row is present in fixed order, not just the populated ones.
    assert [row.stage for row in snapshot.items_by_stage] == [0, 1, 2, 3, 4, 5, 6]


def test_compute_status_counts_items_by_stage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    with Session(engine) as session:
        session.add(_mk_item("a" * 64, stage_completed=1))
        session.add(_mk_item("b" * 64, stage_completed=4, zotero_item_key="K"))
        session.add(
            _mk_item(
                "c" * 64,
                stage_completed=5,
                zotero_item_key="K2",
                tags_json='{"tema":["x"],"metodo":[]}',
            )
        )
        session.add(
            _mk_item("d" * 64, stage_completed=4, in_quarantine=True, last_error="boom")
        )
        session.commit()
    snapshot = compute_status(
        settings=settings,
        engine=engine,
        now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)],
    )
    per_stage = {r.stage: r.count for r in snapshot.items_by_stage}
    assert per_stage[1] == 1
    assert per_stage[4] == 2
    assert per_stage[5] == 1
    assert snapshot.items_in_quarantine == 1
    assert snapshot.items_with_last_error == 1
    assert snapshot.items_with_zotero_key == 2
    assert snapshot.items_tagged == 1


def test_compute_status_aggregates_costs_by_stage(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    now = datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(Run(stage=1, status="succeeded", cost_usd=0.12, started_at=now))
        session.add(
            Run(
                stage=4,
                status="succeeded",
                cost_usd=1.5,
                started_at=now + timedelta(minutes=5),
            )
        )
        session.add(
            Run(
                stage=4,
                status="succeeded",
                cost_usd=0.3,
                started_at=now + timedelta(minutes=10),
            )
        )
        session.commit()
    snapshot = compute_status(settings=settings, engine=engine, now=[now])
    by_stage = {row.stage: row for row in snapshot.cost_by_stage}
    assert by_stage[1].cost_usd == 0.12
    assert by_stage[4].cost_usd == 1.8
    assert by_stage[4].runs == 2
    assert abs(snapshot.cost_total_usd - 1.92) < 1e-9
    # Last run is the most recent by started_at.
    assert snapshot.last_run_stage == 4


def test_compute_status_reports_credentials_without_leaking(tmp_path: Path) -> None:
    settings = _settings(tmp_path, with_openai=True)
    snapshot = compute_status(
        settings=settings, now=[datetime(2026, 4, 23, tzinfo=UTC)]
    )
    assert snapshot.credentials.openai_configured is True
    # library_id set but api_key empty → zotero reports as not configured.
    assert snapshot.credentials.zotero_configured is False


def test_format_status_includes_each_section(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    snapshot = compute_status(
        settings=settings, now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)]
    )
    rendered = format_status(snapshot)
    assert "zotai s1 status" in rendered
    assert "items by stage_completed" in rendered
    assert "cost by stage" in rendered
    assert "total cost: $0.0000" in rendered
    assert "last run: (none)" in rendered
