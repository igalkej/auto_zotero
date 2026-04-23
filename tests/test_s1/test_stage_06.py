"""Tests for :mod:`zotai.s1.stage_06_validate`.

Seeds a ``state.db`` with a mix of items in different post-pipeline
states and checks each aggregator in isolation, plus the full
``run_validate`` smoke path (HTML + CSV on disk).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlmodel import Session

from zotai.config import PathSettings, Settings, ZoteroSettings
from zotai.s1.stage_06_validate import (
    _compute_completeness,
    _compute_consistency,
    _compute_cost_breakdown,
    _compute_duplicates,
    _compute_stage_01_filtering,
    _compute_tag_distribution,
    _latest_csv,
    run_validate,
)
from zotai.state import ApiCall, Item, Run, init_s1, make_s1_engine

# ─── Fixtures ─────────────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="42", library_type="user"),
    )


def _mk_item(
    sha: str,
    *,
    zotero_item_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    tags: dict[str, list[str]] | None = None,
    has_text: bool = True,
    in_quarantine: bool = False,
    needs_review: bool = False,
    stage_completed: int = 5,
) -> Item:
    return Item(
        id=sha,
        source_path=f"/data/{sha}.pdf",
        size_bytes=4096,
        has_text=has_text,
        classification="academic",
        stage_completed=stage_completed,
        zotero_item_key=zotero_item_key,
        in_quarantine=in_quarantine,
        needs_review=needs_review,
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        tags_json=json.dumps(tags) if tags is not None else None,
    )


# ─── Aggregator unit tests ───────────────────────────────────────────────


def test_compute_completeness_counts_each_field() -> None:
    items = [
        _mk_item("a" * 64, zotero_item_key="K1", metadata={"title": "A"}, tags={"tema": ["t"], "metodo": []}),
        _mk_item("b" * 64, zotero_item_key="K2", metadata={"title": "B"}),
        _mk_item("c" * 64, has_text=False, in_quarantine=True),
    ]
    c = _compute_completeness(items)
    assert c.total_items == 3
    assert c.items_in_main == 2
    assert c.items_in_quarantine == 1
    assert c.with_zotero_key == 2
    assert c.with_metadata == 2
    assert c.with_tags == 1
    assert c.with_fulltext == 2


def test_compute_tag_distribution_flags_orphan_and_dominant() -> None:
    # One dominant tag (used on 100% of tagged items) and one orphan (1 use).
    items = [
        _mk_item(f"{i}" * 64, tags={"tema": ["macro-fiscal"], "metodo": []})
        for i in range(10)
    ]
    items[0] = _mk_item("0" * 64, tags={"tema": ["macro-fiscal", "rare"], "metodo": []})
    stats = _compute_tag_distribution(items)
    assert stats.items_tagged == 10
    assert stats.tag_counts["macro-fiscal"] == 10
    assert stats.tag_counts["rare"] == 1
    assert "rare" in stats.orphan_tags
    assert "macro-fiscal" in stats.dominant_tags


def test_compute_tag_distribution_ignores_empty_and_malformed_json() -> None:
    items = [
        _mk_item("a" * 64, tags={"tema": ["x"], "metodo": []}),
        Item(
            id="b" * 64,
            source_path="/x",
            size_bytes=1,
            has_text=True,
            classification="academic",
            tags_json="{garbage",
        ),
        _mk_item("c" * 64, tags=None),
    ]
    stats = _compute_tag_distribution(items)
    assert stats.items_tagged == 2  # malformed still counts as "has tags_json"
    assert stats.tag_counts == {"x": 1}


def test_compute_consistency_flags_missing_title_zero_authors_bad_year() -> None:
    items = [
        _mk_item("a" * 64, metadata={"title": "", "creators": [{"firstName": "J"}]}),
        _mk_item("b" * 64, metadata={"title": "Has title", "creators": []}),
        _mk_item(
            "c" * 64,
            metadata={"title": "T", "creators": [{"firstName": "J"}], "date": "1789"},
        ),
        _mk_item(
            "d" * 64,
            metadata={"title": "Valid", "creators": [{"firstName": "J"}], "date": "2024"},
        ),
        # Quarantined items are skipped even if they have issues.
        _mk_item("e" * 64, metadata={"title": ""}, in_quarantine=True),
    ]
    issues = _compute_consistency(items, now_year=2026)
    reasons = {issue.reason for issue in issues}
    assert "missing_title" in reasons
    assert "zero_authors" in reasons
    assert any(r.startswith("year_out_of_range") for r in reasons)
    # Item 'd' is clean; item 'e' is quarantined — both should be absent.
    ids = {issue.sha256 for issue in issues}
    assert "d" * 64 not in ids
    assert "e" * 64 not in ids


def test_compute_duplicates_finds_same_year_high_similarity() -> None:
    items = [
        _mk_item(
            "a" * 64,
            zotero_item_key="KA",
            metadata={
                "title": "Fiscal policy in emerging economies",
                "creators": [{"firstName": "J"}],
                "date": "2023",
            },
        ),
        _mk_item(
            "b" * 64,
            zotero_item_key="KB",
            metadata={
                "title": "Fiscal policy in emerging economies.",  # near-identical
                "creators": [{"firstName": "J"}],
                "date": "2023",
            },
        ),
        # Same title but different year → not a duplicate pair.
        _mk_item(
            "c" * 64,
            metadata={
                "title": "Fiscal policy in emerging economies",
                "creators": [{"firstName": "J"}],
                "date": "2020",
            },
        ),
        # Quarantined → ignored.
        _mk_item(
            "d" * 64,
            metadata={
                "title": "Fiscal policy in emerging economies",
                "creators": [{"firstName": "J"}],
                "date": "2023",
            },
            in_quarantine=True,
        ),
    ]
    pairs = _compute_duplicates(items)
    assert len(pairs) == 1
    pair = pairs[0]
    assert {pair.sha_a, pair.sha_b} == {"a" * 64, "b" * 64}
    assert pair.year == 2023
    assert pair.score > 90.0


def test_compute_cost_breakdown_aggregates_by_stage_service() -> None:
    runs = [
        Run(id=1, stage=1, status="succeeded", cost_usd=0.2),
        Run(id=2, stage=4, status="succeeded", cost_usd=0.5),
    ]
    api_calls = [
        ApiCall(run_id=1, service="openai", cost_usd=0.2, duration_ms=0, status="success"),
        ApiCall(run_id=2, service="openai", cost_usd=0.3, duration_ms=0, status="success"),
        ApiCall(run_id=2, service="openai", cost_usd=0.2, duration_ms=0, status="success"),
    ]
    total, rows = _compute_cost_breakdown(api_calls, runs)
    assert total == 0.7
    per = {(row.stage, row.service): row for row in rows}
    assert per[(1, "openai")].calls == 1
    assert per[(4, "openai")].calls == 2
    assert abs(per[(4, "openai")].cost_usd - 0.5) < 1e-9


def test_latest_csv_prefers_non_dryrun(tmp_path: Path) -> None:
    folder = tmp_path / "r"
    folder.mkdir()
    (folder / "excluded_report_20260422_100000_dryrun.csv").write_text(
        "x\n", encoding="utf-8"
    )
    # Touch a non-dryrun file later.
    older = folder / "excluded_report_20260421_090000.csv"
    older.write_text("y\n", encoding="utf-8")
    assert _latest_csv(folder, "excluded_report") == older


def test_compute_stage_01_filtering_reads_latest_excluded_csv(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    reports.mkdir()
    csv_path = reports / "excluded_report_20260420_120000.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "source_path",
                "sha256",
                "size_bytes",
                "page_count",
                "rejection_reason",
                "classifier_branch",
                "llm_reason",
            ],
        )
        writer.writeheader()
        writer.writerow({"rejection_reason": "billing_keyword", "classifier_branch": "negative"})
        writer.writerow({"rejection_reason": "billing_keyword", "classifier_branch": "negative"})
        writer.writerow({"rejection_reason": "too_few_pages", "classifier_branch": "negative"})
    items = [_mk_item("a" * 64, needs_review=True), _mk_item("b" * 64)]
    stats = _compute_stage_01_filtering(items, reports)
    assert stats.excluded_count == 3
    assert stats.excluded_by_reason == {"billing_keyword": 2, "too_few_pages": 1}
    assert stats.needs_review_count == 1
    assert stats.excluded_csv_path == csv_path


# ─── End-to-end: run_validate writes HTML + CSV ──────────────────────────


def test_run_validate_writes_html_and_csv(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    # Two items with overlapping titles to force a duplicate pair.
    with Session(engine) as session:
        session.add(
            _mk_item(
                "a" * 64,
                zotero_item_key="KA",
                metadata={
                    "title": "Inflation dynamics in Latin America",
                    "creators": [{"firstName": "J", "lastName": "D"}],
                    "date": "2022",
                },
                tags={"tema": ["macro-inflacion"], "metodo": ["empirico-obs"]},
            )
        )
        session.add(
            _mk_item(
                "b" * 64,
                zotero_item_key="KB",
                metadata={
                    "title": "Inflation dynamics in Latin America.",
                    "creators": [{"firstName": "J", "lastName": "D"}],
                    "date": "2022",
                },
                tags={"tema": ["macro-inflacion"], "metodo": []},
            )
        )
        # A quarantined item — excluded from completeness "main" count.
        session.add(_mk_item("c" * 64, in_quarantine=True))
        # A Run with a cost so the ApiCall breakdown has something to show.
        run = Run(stage=1, status="succeeded", cost_usd=0.5)
        session.add(run)
        session.flush()
        session.add(
            ApiCall(
                run_id=run.id,  # type: ignore[arg-type]
                service="openai",
                cost_usd=0.5,
                duration_ms=100,
                status="success",
            )
        )
        session.commit()

    report = run_validate(
        settings=settings,
        engine=engine,
        now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)],
    )
    assert report.html_path is not None
    assert report.csv_path is not None
    assert report.html_path.exists()
    assert report.csv_path.exists()
    assert report.completeness.total_items == 3
    assert report.completeness.items_in_quarantine == 1
    assert len(report.duplicate_pairs) == 1
    assert report.cost_total_usd == 0.5

    html_content = report.html_path.read_text(encoding="utf-8")
    assert "S1 Validation Report" in html_content
    assert "Inflation dynamics in Latin America" in html_content
    # Zotero links point to the configured library.
    assert "zotero.org/users/42/items/KA" in html_content

    # Summary CSV has the expected shape.
    with report.csv_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    by_metric = {(r["section"], r["metric"]): r["value"] for r in rows}
    assert by_metric[("completeness", "total_items")] == "3"
    assert by_metric[("duplicates", "pairs_total")] == "1"
    assert by_metric[("costs", "total_usd")] == "0.500000"


def test_run_validate_on_empty_db_produces_zeroes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    report = run_validate(
        settings=settings,
        engine=engine,
        now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)],
    )
    assert report.completeness.total_items == 0
    assert report.tag_distribution.items_tagged == 0
    assert report.duplicate_pairs == []
    assert report.cost_total_usd == 0.0
    assert report.html_path is not None and report.html_path.exists()
    # HTML still contains the standard sections even with no data.
    html_content = report.html_path.read_text(encoding="utf-8")
    assert "1. Completeness" in html_content
    assert "No consistency issues detected." in html_content


def test_run_validate_timing_captures_finished_runs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    started = datetime(2026, 4, 23, 10, 0, 0, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            Run(
                stage=4,
                started_at=started,
                finished_at=started + timedelta(seconds=45),
                status="succeeded",
                items_processed=3,
            )
        )
        session.commit()
    report = run_validate(
        settings=settings,
        engine=engine,
        now=[datetime(2026, 4, 23, 12, 0, 0, tzinfo=UTC)],
    )
    assert len(report.timing_by_stage) == 1
    assert report.timing_by_stage[0].duration_seconds == 45.0
