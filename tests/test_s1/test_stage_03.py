"""Tests for :mod:`zotai.s1.stage_03_import`.

Structure: a fake ``ZoteroClient`` records every call and can simulate
connectivity failures, existing DOI matches, and attachment errors. A
fake ``OpenAlexClient`` returns scripted ``work_by_doi`` responses. The
tests drive ``run_import`` (the sync wrapper) so the asyncio.run
plumbing is exercised end-to-end.
"""

from __future__ import annotations

import csv
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, select
from typer.testing import CliRunner

from zotai.cli import app
from zotai.config import PathSettings, Settings, ZoteroSettings
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_03_import import (
    map_openalex_to_zotero,
    run_import,
)
from zotai.state import Item, Run, init_s1, make_s1_engine

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeZoteroClient:
    """Minimal in-memory stand-in for ``ZoteroClient``.

    Only the methods Stage 03 touches. Records every call for assertions.
    """

    def __init__(
        self,
        *,
        connectivity_ok: bool = True,
        existing: list[dict[str, Any]] | None = None,
        existing_children: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.connectivity_ok = connectivity_ok
        self._existing = existing or []
        self._existing_children = existing_children or {}
        self.dry_run = False
        self.created_items: list[dict[str, Any]] = []
        self.attachments: list[dict[str, Any]] = []
        self.items_calls: list[dict[str, Any]] = []
        self.children_calls: list[str] = []
        self._next = 0

    def _key(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next:04d}"

    def items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.items_calls.append(kwargs)
        if not self.connectivity_ok:
            raise ConnectionError("zotero unreachable")
        q = kwargs.get("q")
        if q is None:
            # Connectivity probe — return empty list.
            return []
        lowered = str(q).lower()
        return [
            e
            for e in self._existing
            if (e.get("data") or {}).get("DOI", "").lower() == lowered
        ]

    def create_items(
        self, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        success: dict[str, str] = {}
        for idx, payload in enumerate(items):
            key = self._key("ITEM")
            self.created_items.append({"key": key, "payload": payload})
            success[str(idx)] = key
        return {"success": success, "unchanged": {}, "failed": {}}

    def attachment_simple(
        self, paths: list[str], parent_key: str | None = None
    ) -> dict[str, Any]:
        key = self._key("ATT")
        self.attachments.append(
            {"paths": paths, "parent_key": parent_key, "key": key}
        )
        return {"success": {"0": key}, "unchanged": {}, "failed": {}}

    def children(self, item_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.children_calls.append(item_key)
        return self._existing_children.get(item_key, [])


class FakeOpenAlexClient:
    def __init__(self, responses: dict[str, dict[str, Any] | None]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def work_by_doi(self, doi: str) -> dict[str, Any] | None:
        self.calls.append(doi)
        return self._responses.get(doi)


def _no_sleep() -> Callable[[float], Awaitable[None]]:
    calls: list[float] = []

    async def _s(seconds: float) -> None:
        calls.append(seconds)

    _s.calls = calls  # type: ignore[attr-defined]
    return _s


# ── Fixtures / helpers ─────────────────────────────────────────────────────


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="123", library_type="user", local_api=True),
    )


def _seed(
    settings: Settings,
    *,
    sha: str,
    source_path: Path,
    detected_doi: str | None = None,
    has_text: bool = True,
    stage_completed: int = 2,
    classification: str = "academic",
    zotero_item_key: str | None = None,
) -> None:
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    with Session(engine) as session:
        session.add(
            Item(
                id=sha,
                source_path=str(source_path),
                size_bytes=4096,
                has_text=has_text,
                detected_doi=detected_doi,
                classification=classification,
                stage_completed=stage_completed,
                zotero_item_key=zotero_item_key,
            )
        )
        session.commit()


def _write_pdf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%minimal\n")
    return path


def _good_openalex_work(doi: str, title: str = "A fiscal paper") -> dict[str, Any]:
    return {
        "title": title,
        "doi": f"https://doi.org/{doi}",
        "type": "journal-article",
        "publication_year": 2024,
        "authorships": [
            {"author": {"display_name": "Jane Doe"}},
            {"author": {"display_name": "John Smith"}},
        ],
        "primary_location": {
            "source": {"display_name": "American Economic Review"},
        },
        "abstract_inverted_index": {"This": [0], "is": [1], "an": [2], "abstract": [3]},
    }


# ── map_openalex_to_zotero unit tests ──────────────────────────────────────


def test_map_openalex_full_record() -> None:
    work = _good_openalex_work("10.1257/aer.2024.01")
    payload = map_openalex_to_zotero(work)
    assert payload is not None
    assert payload["itemType"] == "journalArticle"
    assert payload["title"] == "A fiscal paper"
    assert payload["DOI"] == "10.1257/aer.2024.01"
    assert payload["date"] == "2024"
    assert payload["publicationTitle"] == "American Economic Review"
    assert payload["abstractNote"] == "This is an abstract"
    assert len(payload["creators"]) == 2
    assert payload["creators"][0] == {
        "creatorType": "author",
        "firstName": "Jane",
        "lastName": "Doe",
    }


def test_map_openalex_rejects_missing_title() -> None:
    work = _good_openalex_work("10.1/x")
    work["title"] = ""
    assert map_openalex_to_zotero(work) is None


def test_map_openalex_rejects_no_authors() -> None:
    work = _good_openalex_work("10.1/x")
    work["authorships"] = []
    assert map_openalex_to_zotero(work) is None


def test_map_openalex_maps_book_chapter() -> None:
    work = _good_openalex_work("10.1/x")
    work["type"] = "book-chapter"
    payload = map_openalex_to_zotero(work)
    assert payload is not None
    assert payload["itemType"] == "bookSection"


def test_map_openalex_unknown_type_defaults_to_journal_article() -> None:
    work = _good_openalex_work("10.1/x")
    work["type"] = "dataset"
    payload = map_openalex_to_zotero(work)
    assert payload is not None
    assert payload["itemType"] == "journalArticle"


def test_map_openalex_single_name_token() -> None:
    work = _good_openalex_work("10.1/x")
    work["authorships"] = [{"author": {"display_name": "Plato"}}]
    payload = map_openalex_to_zotero(work)
    assert payload is not None
    assert payload["creators"][0] == {
        "creatorType": "author",
        "firstName": "",
        "lastName": "Plato",
    }


# ── Route A: DOI present, OpenAlex returns good metadata ──────────────────


def test_route_a_imports_with_metadata(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1257/aer.2024.01"
    _seed(settings, sha="a" * 64, source_path=pdf, detected_doi=doi)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_processed == 1
    assert result.items_route_a == 1
    assert result.items_route_c == 0

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.import_route == "A"
    assert item.zotero_item_key is not None
    assert item.stage_completed == 3
    assert item.last_error is None

    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == doi
    assert len(zot.attachments) == 1
    assert zot.attachments[0]["parent_key"] == item.zotero_item_key
    assert zot.attachments[0]["paths"] == [str(pdf)]


def test_route_a_falls_to_c_on_openalex_404(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.9999/not-in-openalex"
    _seed(settings, sha="b" * 64, source_path=pdf, detected_doi=doi)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({doi: None})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_route_a == 0
    assert result.items_route_c == 1
    assert len(zot.created_items) == 0
    assert len(zot.attachments) == 1
    assert zot.attachments[0]["parent_key"] is None


def test_route_a_falls_to_c_on_missing_title(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/empty-title"
    work = _good_openalex_work(doi)
    work["title"] = ""
    _seed(settings, sha="c" * 64, source_path=pdf, detected_doi=doi)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({doi: work})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_route_c == 1
    assert len(zot.created_items) == 0


def test_route_a_dedup_attaches_when_existing_has_no_pdf(tmp_path: Path) -> None:
    """ADR 014: existing Zotero item with no PDF child → we attach ours."""
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/already-in-zotero"
    _seed(settings, sha="d" * 64, source_path=pdf, detected_doi=doi)

    existing_key = "EXISTING001"
    zot = FakeZoteroClient(
        existing=[{"key": existing_key, "data": {"DOI": doi}}],
        # No children at all → metadata-only prior import, we add the PDF.
        existing_children={existing_key: []},
    )
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_deduped == 1
    assert result.items_route_a == 1
    assert len(zot.created_items) == 0, "dedup must skip create_items"
    assert len(zot.attachments) == 1
    assert zot.attachments[0]["parent_key"] == existing_key
    assert zot.children_calls == [existing_key]
    assert [r.status for r in result.rows] == ["deduped_pdf_added"]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.zotero_item_key == existing_key
    assert item.stage_completed == 3


def test_route_a_dedup_skips_attach_when_existing_has_pdf(tmp_path: Path) -> None:
    """ADR 014: existing Zotero item with a PDF child → do not duplicate."""
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/already-in-zotero-with-pdf"
    _seed(settings, sha="g" * 64, source_path=pdf, detected_doi=doi)

    existing_key = "EXISTING002"
    zot = FakeZoteroClient(
        existing=[{"key": existing_key, "data": {"DOI": doi}}],
        existing_children={
            existing_key: [
                {
                    "key": "PRE_PDF_001",
                    "data": {
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                    },
                }
            ]
        },
    )
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_deduped == 1
    assert result.items_route_a == 1
    assert len(zot.attachments) == 0, (
        "existing PDF child must prevent a duplicate attach"
    )
    assert zot.children_calls == [existing_key]
    assert [r.status for r in result.rows] == ["deduped"]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.zotero_item_key == existing_key
    assert item.stage_completed == 3


def test_route_a_dedup_skips_non_pdf_attachment(tmp_path: Path) -> None:
    """An HTML snapshot on the existing item does not count as a PDF."""
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/has-snapshot-but-no-pdf"
    _seed(settings, sha="h" * 64, source_path=pdf, detected_doi=doi)

    existing_key = "EXISTING003"
    zot = FakeZoteroClient(
        existing=[{"key": existing_key, "data": {"DOI": doi}}],
        existing_children={
            existing_key: [
                {
                    "key": "SNAPSHOT_001",
                    "data": {
                        "itemType": "attachment",
                        "contentType": "text/html",
                    },
                },
                {
                    "key": "NOTE_001",
                    "data": {
                        "itemType": "note",
                    },
                },
            ]
        },
    )
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    # HTML snapshot ≠ PDF — we still attach our PDF.
    assert len(zot.attachments) == 1
    assert [r.status for r in result.rows] == ["deduped_pdf_added"]


# ── Route C: no DOI ────────────────────────────────────────────────────────


def test_route_c_no_doi_creates_orphan(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(settings, sha="e" * 64, source_path=pdf, detected_doi=None)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_route_c == 1
    assert oa.calls == [], "OpenAlex must not be called without a DOI"
    assert len(zot.attachments) == 1
    assert zot.attachments[0]["parent_key"] is None

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.import_route == "C"
    assert item.stage_completed == 3


# ── Staging-copy preference ────────────────────────────────────────────────


def test_attaches_staging_copy_when_available(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    source = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    # Simulate Stage 02 having produced an OCR'd copy under staging/.
    sha = "f" * 64
    staging = settings.paths.staging_folder / f"{sha}.pdf"
    _write_pdf(staging)
    _seed(settings, sha=sha, source_path=source, detected_doi=None)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({})
    run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert zot.attachments[0]["paths"] == [str(staging)]


# ── Eligibility filters ────────────────────────────────────────────────────


def test_already_imported_items_are_skipped(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(
        settings,
        sha="1" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ALREADY",
    )

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({})
    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.rows == []
    assert len(zot.attachments) == 0


def test_items_without_text_are_not_imported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(settings, sha="2" * 64, source_path=pdf, has_text=False)

    zot = FakeZoteroClient()
    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=FakeOpenAlexClient({}),  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.rows == []


def test_items_at_earlier_stages_are_not_imported(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(settings, sha="3" * 64, source_path=pdf, stage_completed=1)

    zot = FakeZoteroClient()
    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=FakeOpenAlexClient({}),  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.rows == []


# ── Connectivity guard ─────────────────────────────────────────────────────


def test_connectivity_failure_aborts_before_processing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(settings, sha="4" * 64, source_path=pdf, detected_doi=None)

    zot = FakeZoteroClient(connectivity_ok=False)
    with pytest.raises(StageAbortedError):
        run_import(
            batch_pause_seconds=0,
            settings=settings,
            zotero_client=zot,  # type: ignore[arg-type]
            openalex_client=FakeOpenAlexClient({}),  # type: ignore[arg-type]
            sleep=_no_sleep(),
        )
    assert len(zot.attachments) == 0

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        run_row = session.exec(select(Run)).one()
    assert run_row.status == "aborted"


# ── Dry run ────────────────────────────────────────────────────────────────


def test_dry_run_no_writes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/dr"
    _seed(settings, sha="5" * 64, source_path=pdf, detected_doi=doi)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        dry_run=True,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.csv_path.name.endswith("_dryrun.csv")
    assert len(zot.created_items) == 0, "dry_run must not create items"
    assert len(zot.attachments) == 0, "dry_run must not attach"

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.zotero_item_key is None
    assert item.stage_completed == 2


# ── Batching ───────────────────────────────────────────────────────────────


def test_batching_sleeps_between_batches(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    # 3 items, batch_size=2 → 2 batches → 1 sleep between them.
    for i in range(3):
        pdf = _write_pdf(tmp_path / "pdfs" / f"p{i}.pdf")
        _seed(
            settings,
            sha=str(i) * 64,
            source_path=pdf,
            detected_doi=None,
        )

    zot = FakeZoteroClient()
    sleep = _no_sleep()

    run_import(
        batch_size=2,
        batch_pause_seconds=30.0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=FakeOpenAlexClient({}),  # type: ignore[arg-type]
        sleep=sleep,
    )

    assert sleep.calls == [30.0]  # type: ignore[attr-defined]


# ── CSV shape ──────────────────────────────────────────────────────────────


def test_csv_has_expected_columns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    doi = "10.1/csv"
    _seed(settings, sha="7" * 64, source_path=pdf, detected_doi=doi)

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({doi: _good_openalex_work(doi)})

    result = run_import(
        batch_pause_seconds=0,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    with result.csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames is not None
    assert set(reader.fieldnames) == {
        "sha256",
        "source_path",
        "attached_path",
        "detected_doi",
        "import_route",
        "zotero_item_key",
        "status",
        "error",
    }
    assert len(rows) == 1
    assert rows[0]["import_route"] == "A"
    assert rows[0]["status"] == "imported"


# ── CLI ────────────────────────────────────────────────────────────────────


def test_cli_stub_invoke_aborts_when_zotero_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI builds a real ZoteroClient; connectivity fails → exit 2."""
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    _seed(settings, sha="8" * 64, source_path=pdf, detected_doi=None)

    monkeypatch.setenv("STATE_DB", str(settings.paths.state_db))
    monkeypatch.setenv("REPORTS_FOLDER", str(settings.paths.reports_folder))
    monkeypatch.setenv("STAGING_FOLDER", str(settings.paths.staging_folder))
    # No live Zotero in the test environment → `ZoteroClient.items(limit=1)`
    # raises, the stage aborts cleanly, and the CLI exits 2.

    runner = CliRunner()
    result = runner.invoke(app, ["s1", "import", "--batch-pause-seconds", "0"])

    assert result.exit_code == 2
    assert "Stage aborted" in result.output or "Cannot reach" in result.output
