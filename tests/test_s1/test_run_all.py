"""Tests for :mod:`zotai.s1.run_all`.

Monkeypatches every stage's ``run_*`` function with deterministic stubs
so the orchestrator is exercised in isolation from real API / Zotero
calls. The tests cover:

- Inter-stage prompts: default confirm says yes, stages advance;
  rejection at any step stops and records the reason.
- ``--yes``: confirm is never called.
- Tag mode: ``apply`` runs validate; ``preview`` skips validate with a
  clear marker.
- ``StageAbortedError`` short-circuits and the failed stage is logged.
- ``KeyboardInterrupt`` mid-pipeline produces an orderly stop with the
  next-stage-index recorded.
- ``format_summary`` renders all branches readably.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from zotai.config import PathSettings, Settings, ZoteroSettings
from zotai.s1.handler import StageAbortedError
from zotai.s1.run_all import RunAllResult, format_summary, run_all
from zotai.s1.stage_01_inventory import InventoryResult
from zotai.s1.stage_02_ocr import OcrResult
from zotai.s1.stage_03_import import ImportResult
from zotai.s1.stage_04_enrich import EnrichResult
from zotai.s1.stage_05_tag import TagResult


def _settings(tmp_path: Path) -> Settings:
    # Give run_all something to hand to stage_01_inventory — it aborts
    # early if pdf_source_folders is empty.
    src = tmp_path / "src"
    src.mkdir()
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[src],
        ),
        zotero=ZoteroSettings(library_id="42"),
    )


# ─── Stage stubs ─────────────────────────────────────────────────────────


def _fake_inventory(*_: Any, **__: Any) -> InventoryResult:
    return InventoryResult(
        run_id=1,
        rows=[],
        excluded_rows=[],
        csv_path=Path("/tmp/inv.csv"),
        excluded_csv_path=None,
        items_processed=5,
        items_failed=0,
        duplicates=0,
        invalid=0,
        excluded=0,
        llm_cost_usd=0.0,
    )


def _fake_ocr(**_: Any) -> OcrResult:
    return OcrResult(
        run_id=2,
        rows=[],
        csv_path=Path("/tmp/ocr.csv"),
        items_processed=5,
        items_failed=0,
        items_applied=3,
        items_resumed=2,
    )


def _fake_import(**_: Any) -> ImportResult:
    return ImportResult(
        run_id=3,
        rows=[],
        csv_path=Path("/tmp/imp.csv"),
        items_processed=5,
        items_failed=0,
        items_route_a=4,
        items_route_c=1,
        items_deduped=0,
        items_skipped=0,
    )


def _fake_enrich(**_: Any) -> EnrichResult:
    return EnrichResult(
        run_id=4,
        rows=[],
        csv_path=Path("/tmp/enr.csv"),
        items_processed=1,
        items_failed=0,
        items_enriched_04a=0,
        items_enriched_04b=1,
        items_enriched_04bs=0,
        items_enriched_04bd=0,
        items_enriched_04c=0,
        items_enriched_04d=0,
        items_quarantined=0,
        items_no_progress=0,
        items_skipped=0,
        items_skipped_generic_title=0,
        quarantine_csv_path=None,
    )


def _fake_tag(**_: Any) -> TagResult:
    return TagResult(
        run_id=5,
        rows=[],
        csv_path=Path("/tmp/tag.csv"),
        items_processed=5,
        items_failed=0,
        items_tagged=5,
        items_previewed=0,
        items_no_metadata=0,
        items_llm_failed=0,
        cost_usd=0.02,
    )


def _fake_validate(**_: Any) -> Any:
    from datetime import UTC, datetime

    from zotai.s1.stage_06_validate import (
        CompletenessStats,
        Stage01Filtering,
        TagDistributionStats,
        ValidationReport,
    )

    return ValidationReport(
        generated_at=datetime(2026, 4, 23, tzinfo=UTC),
        completeness=CompletenessStats(5, 5, 0, 5, 5, 5, 5),
        tag_distribution=TagDistributionStats({}, [], [], 5),
        consistency_issues=[],
        duplicate_pairs=[],
        cost_total_usd=0.02,
        cost_by_stage_service=[],
        timing_by_stage=[],
        stage_01_filtering=Stage01Filtering(0, 0, {}, None),
        html_path=Path("/tmp/validate.html"),
        csv_path=Path("/tmp/validate.csv"),
    )


@pytest.fixture()
def stub_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace every S1 stage with its deterministic stub."""
    import zotai.s1.run_all as mod

    monkeypatch.setattr(mod, "run_inventory", _fake_inventory)
    monkeypatch.setattr(mod, "run_ocr", _fake_ocr)
    monkeypatch.setattr(mod, "run_import", _fake_import)
    monkeypatch.setattr(mod, "run_enrich", _fake_enrich)
    monkeypatch.setattr(mod, "run_tag", _fake_tag)
    monkeypatch.setattr(mod, "run_validate", _fake_validate)


# ─── Happy paths ─────────────────────────────────────────────────────────


def test_run_all_yes_skips_confirmation(tmp_path: Path, stub_stages: None) -> None:
    confirms: list[str] = []

    def confirm(q: str) -> bool:
        confirms.append(q)
        return True

    settings = _settings(tmp_path)
    result = run_all(
        yes=True,
        settings=settings,
        confirm=confirm,
        echo=lambda _: None,
    )
    assert result.completed is True
    assert result.stopped_at_stage is None
    assert confirms == [], "--yes must never prompt"
    assert [s.stage for s in result.stages] == [1, 2, 3, 4, 5, 6]
    assert all(s.succeeded for s in result.stages)
    assert result.total_cost_usd == pytest.approx(0.02)


def test_run_all_prompts_between_stages(tmp_path: Path, stub_stages: None) -> None:
    answers = iter([True, True, True, True, True])

    def confirm(_: str) -> bool:
        return next(answers)

    settings = _settings(tmp_path)
    result = run_all(settings=settings, confirm=confirm, echo=lambda _: None)
    assert result.completed is True
    # 5 prompts: between stages 1→2, 2→3, 3→4, 4→5, 5→6. Stage 6 is the
    # last; no prompt after it.
    assert [s.stage for s in result.stages] == [1, 2, 3, 4, 5, 6]


def test_run_all_stops_when_user_declines(
    tmp_path: Path, stub_stages: None
) -> None:
    answers = iter([True, False])

    def confirm(_: str) -> bool:
        return next(answers)

    settings = _settings(tmp_path)
    result = run_all(settings=settings, confirm=confirm, echo=lambda _: None)
    assert result.completed is False
    assert result.stopped_at_stage == 2  # declined after ocr (stage 2)
    assert result.stopped_reason == "user declined to continue"
    # Stages 1 and 2 ran; nothing beyond.
    assert [s.stage for s in result.stages] == [1, 2]


def test_run_all_preview_mode_stops_before_validate(
    tmp_path: Path, stub_stages: None
) -> None:
    settings = _settings(tmp_path)
    result = run_all(
        yes=True,
        tag_mode="preview",
        settings=settings,
        echo=lambda _: None,
    )
    assert result.completed is True
    assert [s.stage for s in result.stages] == [1, 2, 3, 4, 5, 6]
    # Stage 6 is recorded as skipped.
    stage_6 = result.stages[-1]
    assert stage_6.skipped is True
    assert "skipped" in stage_6.summary.lower()


# ─── Error paths ─────────────────────────────────────────────────────────


def test_run_all_surfaces_stage_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stage raising StageAbortedError is captured as stopped_at_stage."""
    import zotai.s1.run_all as mod

    monkeypatch.setattr(mod, "run_inventory", _fake_inventory)

    def boom_ocr(**_: Any) -> OcrResult:
        raise StageAbortedError("disk full")

    monkeypatch.setattr(mod, "run_ocr", boom_ocr)

    settings = _settings(tmp_path)
    messages: list[str] = []
    result = run_all(
        yes=True,
        settings=settings,
        echo=messages.append,
    )
    assert result.completed is False
    assert result.stopped_at_stage == 2
    assert result.stopped_reason is not None and "disk full" in result.stopped_reason
    assert any("aborted" in m.lower() for m in messages)


def test_run_all_handles_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C between stages surfaces an orderly stop."""
    import zotai.s1.run_all as mod

    monkeypatch.setattr(mod, "run_inventory", _fake_inventory)

    def interrupted_ocr(**_: Any) -> OcrResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(mod, "run_ocr", interrupted_ocr)

    settings = _settings(tmp_path)
    messages: list[str] = []
    result = run_all(
        yes=True,
        settings=settings,
        echo=messages.append,
    )
    assert result.completed is False
    assert result.stopped_at_stage == 2
    assert result.stopped_reason == "keyboard_interrupt"
    assert any("Interrupted" in m for m in messages)


def test_run_all_rejects_empty_source_folders(tmp_path: Path) -> None:
    """Stage 01 needs folders to scan; run_all fails fast with a clear reason."""
    settings = Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="42"),
    )
    messages: list[str] = []
    result = run_all(
        yes=True,
        settings=settings,
        echo=messages.append,
    )
    assert result.completed is False
    assert result.stopped_at_stage == 1
    assert result.stopped_reason is not None and "PDF_SOURCE_FOLDERS" in result.stopped_reason


# ─── format_summary ──────────────────────────────────────────────────────


def test_format_summary_prints_every_recorded_stage() -> None:
    from zotai.s1.run_all import StageOutcome

    result = RunAllResult(
        stages=[
            StageOutcome(1, "inventory", "processed=5", True),
            StageOutcome(2, "ocr", "processed=5", True),
            StageOutcome(3, "import", "processed=5", True),
            StageOutcome(4, "enrich", "processed=1", True),
            StageOutcome(5, "tag", "tagged=5 cost=$0.0200", True),
            StageOutcome(6, "validate", "skipped (tag_mode=preview)", True, skipped=True),
        ],
        total_cost_usd=0.02,
        completed=True,
    )
    rendered = format_summary(result)
    assert "[01/06] inventory" in rendered
    assert "[06/06] validate" in rendered
    assert "skipped" in rendered
    assert "total cost: $0.0200" in rendered
    assert "status: completed" in rendered


def test_format_summary_reports_stopped_stage() -> None:
    from zotai.s1.run_all import StageOutcome

    result = RunAllResult(
        stages=[StageOutcome(1, "inventory", "processed=5", True)],
        total_cost_usd=0.0,
        completed=False,
        stopped_at_stage=2,
        stopped_reason="user declined",
    )
    rendered = format_summary(result)
    assert "status: stopped at stage 02" in rendered
    assert "user declined" in rendered
