"""Tests for :mod:`zotai.s1.stage_02_ocr`.

These tests run sequentially (``parallel=1``) so pytest monkeypatches
apply: ``multiprocessing.Pool`` spawns fresh processes that would lose
module-level patches. The sequential path exercises the same worker
function (:func:`_process_one`) so the coverage is the same.
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from zotai.cli import app
from zotai.config import OcrSettings, PathSettings, Settings
from zotai.s1 import stage_02_ocr as mod
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_02_ocr import run_ocr
from zotai.state import Item, Run, init_s1, make_s1_engine


def _settings(tmp_path: Path, *, parallel: int = 1) -> Settings:
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        ocr=OcrSettings(languages="spa+eng", parallel_processes=parallel),
    )


def _seed_item(
    settings: Settings,
    pdf_path: Path,
    *,
    has_text: bool = False,
    stage_completed: int = 1,
    sha: str | None = None,
    size_bytes: int = 4096,
) -> str:
    """Insert one Item row that Stage 02 will pick up; return its sha256."""
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    item_id = sha or ("a" * 64)
    with Session(engine) as session:
        session.add(
            Item(
                id=item_id,
                source_path=str(pdf_path),
                size_bytes=size_bytes,
                has_text=has_text,
                stage_completed=stage_completed,
            )
        )
        session.commit()
    return item_id


def _fake_ocr_success(*_: Any, **__: Any) -> int:
    """Stand-in for ``ocrmypdf.ocr`` that silently succeeds."""
    return 0


def _fake_ocr_failure(*_: Any, **__: Any) -> int:
    raise RuntimeError("tesseract exploded")


# ─── happy paths ────────────────────────────────────────────────────────────


def test_eligible_item_gets_ocr_applied(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    monkeypatch.setattr("ocrmypdf.ocr", _fake_ocr_success)
    # Simulate that post-OCR the staging copy has text.
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)

    result = run_ocr(settings=settings, parallel=1)

    assert result.items_processed == 1
    assert result.items_failed == 0
    assert result.items_applied == 1

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.has_text is True
    assert item.ocr_failed is False
    assert item.stage_completed == 2
    assert (settings.paths.staging_folder / f"{item.id}.pdf").exists()


def test_ocr_failure_advances_stage_and_marks_ocr_failed(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    monkeypatch.setattr("ocrmypdf.ocr", _fake_ocr_failure)
    # has_text_layer wouldn't be called on the failure path, but be safe.
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: False)

    result = run_ocr(settings=settings, parallel=1)

    assert result.items_processed == 0
    assert result.items_failed == 1
    assert result.items_applied == 0

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    # Spec: advance stage_completed anyway so Stage 03 sees the item.
    assert item.stage_completed == 2
    assert item.ocr_failed is True
    assert item.has_text is False
    assert item.last_error is not None and "tesseract" in item.last_error


def test_ocr_produces_no_text_is_failure(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR call returns cleanly but post-verify finds no text → ``failed``."""
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    monkeypatch.setattr("ocrmypdf.ocr", _fake_ocr_success)
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: False)

    result = run_ocr(settings=settings, parallel=1)

    assert result.items_failed == 1
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.ocr_failed is True
    assert item.last_error == "no_text_after_ocr"


# ─── filtering / no-op paths ────────────────────────────────────────────────


def test_no_eligible_items_is_noop(tmp_path: Path) -> None:
    """Empty DB → no rows, no CSV error, run marked succeeded."""
    settings = _settings(tmp_path)
    # Create empty DB.
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)

    result = run_ocr(settings=settings, parallel=1)

    assert result.items_processed == 0
    assert result.items_failed == 0
    assert result.rows == []
    assert result.csv_path.exists()


def test_item_with_has_text_true_is_skipped(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    """Stage 01 already extracted text → OCR must not touch it."""
    source_pdf = pdf_builder("text_doi", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf, has_text=True)

    result = run_ocr(settings=settings, parallel=1)

    assert result.rows == []

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 1  # unchanged — Stage 02 skipped it


def test_item_at_stage_2_is_skipped(
    pdf_builder: Callable[..., Path], tmp_path: Path
) -> None:
    """Re-running the stage on already-processed items is a no-op."""
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf, stage_completed=2)

    result = run_ocr(settings=settings, parallel=1)

    assert result.rows == []


# ─── resume-safety ──────────────────────────────────────────────────────────


def test_resumed_path_skips_ocr_when_staging_has_text(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    sha = _seed_item(settings, source_pdf)

    # Pre-populate staging with an arbitrary PDF and pretend it has text.
    settings.paths.staging_folder.mkdir(parents=True, exist_ok=True)
    (settings.paths.staging_folder / f"{sha}.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)

    calls: dict[str, int] = {"ocr": 0}

    def _ocr_counted(*_: Any, **__: Any) -> int:
        calls["ocr"] += 1
        return 0

    monkeypatch.setattr("ocrmypdf.ocr", _ocr_counted)

    result = run_ocr(settings=settings, parallel=1)

    assert calls["ocr"] == 0, "ocrmypdf must not run on resumed path"
    assert result.items_resumed == 1
    assert result.items_processed == 1


# ─── disk-space guard ──────────────────────────────────────────────────────


def test_insufficient_disk_space_aborts(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf, size_bytes=10_000_000)

    monkeypatch.setattr(mod, "disk_space_available", lambda _p: 1024)

    with pytest.raises(StageAbortedError):
        run_ocr(settings=settings, parallel=1)

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        run_row = session.exec(select(Run)).one()
    assert run_row.status == "aborted"


# ─── dry-run ───────────────────────────────────────────────────────────────


def test_dry_run_makes_no_filesystem_or_db_writes(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    def _forbidden(*_: Any, **__: Any) -> int:
        raise AssertionError("ocrmypdf must not be called in dry-run")

    monkeypatch.setattr("ocrmypdf.ocr", _forbidden)

    result = run_ocr(settings=settings, parallel=1, dry_run=True)

    assert result.csv_path.name.endswith("_dryrun.csv")
    # No staging copy written.
    assert not (settings.paths.staging_folder / f"{'a' * 64}.pdf").exists()
    # DB row unchanged.
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 1
    assert item.has_text is False


# ─── CSV shape ──────────────────────────────────────────────────────────────


def test_csv_has_expected_columns(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    monkeypatch.setattr("ocrmypdf.ocr", _fake_ocr_success)
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)

    result = run_ocr(settings=settings, parallel=1)

    with result.csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames is not None
    assert set(reader.fieldnames) == {
        "sha256",
        "source_path",
        "staging_path",
        "status",
        "has_text_post",
        "duration_ms",
        "error",
    }
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"


# ─── force-ocr flag ────────────────────────────────────────────────────────


def test_force_ocr_sets_force_flag_and_skips_skip_text(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--force-ocr passes force_ocr=True and omits skip_text (they're exclusive)."""
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    captured_kwargs: dict[str, Any] = {}

    def _capture(*args: Any, **kwargs: Any) -> int:
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("ocrmypdf.ocr", _capture)
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)

    run_ocr(settings=settings, parallel=1, force_ocr=True)

    assert captured_kwargs.get("force_ocr") is True
    assert "skip_text" not in captured_kwargs


def test_default_mode_uses_skip_text(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    captured_kwargs: dict[str, Any] = {}

    def _capture(*args: Any, **kwargs: Any) -> int:
        captured_kwargs.update(kwargs)
        return 0

    monkeypatch.setattr("ocrmypdf.ocr", _capture)
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)

    run_ocr(settings=settings, parallel=1, force_ocr=False)

    assert captured_kwargs.get("skip_text") is True
    assert "force_ocr" not in captured_kwargs
    # Languages are split on "+" because ocrmypdf's Python API wants a list.
    assert captured_kwargs.get("language") == ["spa", "eng"]


# ─── CLI ───────────────────────────────────────────────────────────────────


def test_cli_runs_stage_02(
    pdf_builder: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_pdf = pdf_builder("scanned", directory=tmp_path / "pdfs")
    settings = _settings(tmp_path)
    _seed_item(settings, source_pdf)

    monkeypatch.setattr("ocrmypdf.ocr", _fake_ocr_success)
    monkeypatch.setattr(mod, "has_text_layer", lambda _p: True)
    monkeypatch.setenv("STATE_DB", str(settings.paths.state_db))
    monkeypatch.setenv("REPORTS_FOLDER", str(settings.paths.reports_folder))
    monkeypatch.setenv("STAGING_FOLDER", str(settings.paths.staging_folder))
    monkeypatch.setenv("OCR_PARALLEL_PROCESSES", "1")

    runner = CliRunner()
    result = runner.invoke(app, ["s1", "ocr"])

    assert result.exit_code == 0, result.output
    assert "processed=1" in result.output
    assert "applied=1" in result.output
