"""Tests for ``zotai.s1.stage_01_inventory``."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Callable

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from zotai.cli import app
from zotai.config import PathSettings, Settings
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_01_inventory import run_inventory
from zotai.state import Item, Run, init_s1, make_s1_engine


def _settings(tmp_path: Path, source_folders: list[Path]) -> Settings:
    return Settings(
        paths=PathSettings(
            pdf_source_folders=source_folders,
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
        )
    )


def test_valid_pdf_with_doi_persists_item(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=False, settings=settings)

    assert result.items_processed == 1
    assert result.items_failed == 0
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        items = session.exec(select(Item)).all()
    assert len(items) == 1
    item = items[0]
    assert item.stage_completed == 1
    assert item.has_text is True
    assert item.detected_doi == "10.1234/example.2024"
    assert item.last_error is None


def test_scanned_like_has_text_false(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("scanned", directory=folder)
    settings = _settings(tmp_path, [folder])

    run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 1
    assert item.has_text is False
    assert item.detected_doi is None


def test_no_doi_pdf_persists_without_doi(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_no_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 1
    assert item.has_text is True
    assert item.detected_doi is None


def test_fake_magic_byte_skipped(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("fake", directory=folder)
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        items = session.exec(select(Item)).all()
    assert items == []
    assert len(result.rows) == 1
    assert result.rows[0].status == "invalid_magic"
    assert result.rows[0].sha256 is None
    assert result.invalid == 1


def test_corrupt_pdf_sets_last_error(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("corrupt", directory=folder)
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=False, settings=settings)

    assert result.items_failed == 1
    assert result.items_processed == 0
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 0
    assert item.last_error is not None
    assert item.last_error != ""
    assert len(result.rows) == 1
    assert result.rows[0].status == "error"


def test_duplicate_hash_reported_once_in_db(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder, name="a.pdf")
    pdf_builder("text_doi", directory=folder, name="b.pdf")
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        items = session.exec(select(Item)).all()
    assert len(items) == 1
    statuses = sorted(r.status for r in result.rows)
    assert statuses == ["duplicate", "new"]
    dup_row = next(r for r in result.rows if r.status == "duplicate")
    assert dup_row.duplicate_of is not None
    assert dup_row.duplicate_of != dup_row.source_path


def test_dry_run_makes_no_db_writes(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=True, settings=settings)

    assert result.run_id is None
    assert result.csv_path.name.endswith("_dryrun.csv")
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    with Session(engine) as session:
        assert session.exec(select(Item)).all() == []
        assert session.exec(select(Run)).all() == []


def test_rerun_is_noop(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    first = run_inventory([folder], dry_run=False, settings=settings)
    second = run_inventory([folder], dry_run=False, settings=settings)

    assert first.items_processed == 1
    assert second.items_processed == 0
    assert second.items_failed == 0
    assert [r.status for r in second.rows] == ["unchanged"]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        items = session.exec(select(Item)).all()
    assert len(items) == 1


def test_retry_errors_reprocesses_transient_failure(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A first run with a transient extraction failure, then `retry_errors=True`.

    The second run must re-invoke extraction on the existing row, clear
    ``last_error`` on success, and report the row with status ``retried``.
    """
    from zotai.s1 import stage_01_inventory as mod

    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    real_extract = mod.extract_text_pages
    calls = {"n": 0}

    def flaky(path: Path, max_pages: int = 3) -> list[str]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated transient pdfplumber failure")
        return real_extract(path, max_pages=max_pages)

    monkeypatch.setattr(mod, "extract_text_pages", flaky)

    first = run_inventory([folder], dry_run=False, settings=settings)
    assert first.items_failed == 1
    assert [r.status for r in first.rows] == ["error"]

    second_without_flag = run_inventory([folder], dry_run=False, settings=settings)
    assert [r.status for r in second_without_flag.rows] == ["unchanged"]
    assert calls["n"] == 1

    third = run_inventory(
        [folder], dry_run=False, retry_errors=True, settings=settings
    )
    assert [r.status for r in third.rows] == ["retried"]
    assert third.items_processed == 1
    assert third.items_failed == 0

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.last_error is None
    assert item.stage_completed == 1
    assert item.detected_doi == "10.1234/example.2024"
    assert item.has_text is True


def test_retry_errors_still_errors_when_failure_persists(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If extraction keeps raising, `retry_errors=True` reports ``error`` again."""
    from zotai.s1 import stage_01_inventory as mod

    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    def always_fail(path: Path, max_pages: int = 3) -> list[str]:
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(mod, "extract_text_pages", always_fail)

    run_inventory([folder], dry_run=False, settings=settings)
    retry = run_inventory(
        [folder], dry_run=False, retry_errors=True, settings=settings
    )

    assert [r.status for r in retry.rows] == ["error"]
    assert retry.items_failed == 1


def test_csv_contents_match_run_rows(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    for kind in ("text_doi", "text_no_doi", "scanned", "fake", "corrupt"):
        pdf_builder(kind, directory=folder)  # type: ignore[arg-type]
    settings = _settings(tmp_path, [folder])

    result = run_inventory([folder], dry_run=False, settings=settings)

    with result.csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames is not None
    expected_columns = {
        "source_path",
        "sha256",
        "size_bytes",
        "has_text",
        "detected_doi",
        "status",
        "duplicate_of",
        "last_error",
    }
    assert set(reader.fieldnames) == expected_columns
    assert len(rows) == 5
    statuses = sorted(r["status"] for r in rows)
    assert statuses == ["error", "invalid_magic", "new", "new", "new"]


def test_run_row_marks_succeeded(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    settings = _settings(tmp_path, [folder])

    run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        run_row = session.exec(select(Run)).one()
    assert run_row.stage == 1
    assert run_row.status == "succeeded"
    assert run_row.finished_at is not None
    assert run_row.items_processed == 1


def test_stage_abort_on_mass_failure(tmp_path: Path) -> None:
    folder = tmp_path / "pdfs"
    folder.mkdir()
    for i in range(15):
        (folder / f"corrupt_{i:02d}.pdf").write_bytes(
            b"%PDF-1.4\n" + f"garbage-{i}".encode() * 50
        )
    settings = _settings(tmp_path, [folder])

    with pytest.raises(StageAbortedError):
        run_inventory([folder], dry_run=False, settings=settings)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        run_row = session.exec(select(Run)).one()
    assert run_row.status == "aborted"
    assert run_row.items_failed >= 10


def test_cli_folder_option(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "pdfs"
    pdf_builder("text_doi", directory=folder)
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("REPORTS_FOLDER", str(tmp_path / "reports"))

    runner = CliRunner()
    result = runner.invoke(app, ["s1", "inventory", "--folder", str(folder)])

    assert result.exit_code == 0, result.output
    assert "processed=1" in result.output


def test_cli_exits_2_when_no_folders_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STATE_DB", str(tmp_path / "state.db"))
    monkeypatch.setenv("REPORTS_FOLDER", str(tmp_path / "reports"))

    runner = CliRunner()
    result = runner.invoke(app, ["s1", "inventory"])

    assert result.exit_code == 2
