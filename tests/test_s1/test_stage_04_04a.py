"""Tests for :mod:`zotai.s1.stage_04_enrich` — substage 04a.

Structure mirrors ``test_stage_03.py``: a fake ``ZoteroClient`` records
every call and can simulate connectivity / lookup responses; a fake
``OpenAlexClient`` returns scripted ``work_by_doi`` responses. Tests
drive :func:`run_enrich` (the sync wrapper) with ``substage="04a"``.

Focus of this test module:

- The regex pulls a new DOI out of page text and 04a retries Route A.
- No new DOI → item sits as ``no_progress`` (ready for 04b in a later PR).
- OpenAlex 404 → ``no_progress`` (not ``failed``, since 04b can still
  pick it up).
- Quality-gate fail on the mapped payload → ``no_progress``.
- Dedup: when the Zotero library already has the DOI, we reuse that key.
- Dedup + existing has PDF → we link the Item row but do not reparent
  the orphan (ADR 014 policy).
- Dry-run: probes OpenAlex but does not create / update / write to DB.
- Idempotency: a second ``run_enrich`` call is a no-op for items that
  already advanced to stage 4.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from zotai.config import PathSettings, Settings, ZoteroSettings
from zotai.s1.stage_04_enrich import run_enrich
from zotai.state import Item, init_s1, make_s1_engine


# ─── Fakes ─────────────────────────────────────────────────────────────────


class FakeZoteroClient:
    """Minimal in-memory stand-in for ``ZoteroClient``. Covers the 04a surface."""

    def __init__(
        self,
        *,
        existing: list[dict[str, Any]] | None = None,
        existing_children: dict[str, list[dict[str, Any]]] | None = None,
        orphans: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._existing = existing or []
        self._existing_children = existing_children or {}
        self._orphans = orphans or {}
        self.dry_run = False
        self.created_items: list[dict[str, Any]] = []
        self.items_calls: list[dict[str, Any]] = []
        self.children_calls: list[str] = []
        self.item_fetch_calls: list[str] = []
        self.updated_items: list[dict[str, Any]] = []
        self._next = 0

    def _key(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next:04d}"

    def items(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.items_calls.append(kwargs)
        q = kwargs.get("q")
        if q is None:
            return []
        lowered = str(q).lower()
        return [
            e
            for e in self._existing
            if (e.get("data") or {}).get("DOI", "").lower() == lowered
        ]

    def children(self, item_key: str, **kwargs: Any) -> list[dict[str, Any]]:
        self.children_calls.append(item_key)
        return self._existing_children.get(item_key, [])

    def item(self, item_key: str) -> dict[str, Any]:
        self.item_fetch_calls.append(item_key)
        if item_key in self._orphans:
            return {"data": self._orphans[item_key]}
        return {"data": {}}

    def create_items(self, payloads: list[dict[str, Any]]) -> dict[str, Any]:
        success: dict[str, str] = {}
        for idx, payload in enumerate(payloads):
            key = self._key("PARENT")
            self.created_items.append({"key": key, "payload": payload})
            success[str(idx)] = key
        return {"success": success, "unchanged": {}, "failed": {}}

    def update_item(self, item: dict[str, Any]) -> bool:
        self.updated_items.append(item)
        return True


class FakeOpenAlexClient:
    def __init__(self, responses: dict[str, dict[str, Any] | None]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def work_by_doi(self, doi: str) -> dict[str, Any] | None:
        self.calls.append(doi.lower())
        return self._responses.get(doi.lower())


def _no_sleep() -> Callable[[float], Awaitable[None]]:
    async def _s(_: float) -> None:
        return None

    return _s


# ─── Fixtures / helpers ───────────────────────────────────────────────────


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


def _seed_orphan(
    settings: Settings,
    *,
    sha: str,
    source_path: Path,
    detected_doi: str | None = None,
    zotero_item_key: str | None = None,
) -> None:
    """Seed one Item as a Stage-03 Route-C orphan ready for Stage 04."""
    engine = make_s1_engine(str(settings.paths.state_db))
    init_s1(engine)
    with Session(engine) as session:
        session.add(
            Item(
                id=sha,
                source_path=str(source_path),
                size_bytes=4096,
                has_text=True,
                detected_doi=detected_doi,
                classification="academic",
                stage_completed=3,
                import_route="C",
                zotero_item_key=zotero_item_key,
            )
        )
        session.commit()


def _write_pdf(path: Path, text: str = "dummy") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n%minimal\n" + text.encode("utf-8"))
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
        "abstract_inverted_index": {"This": [0], "is": [1], "a": [2], "test": [3]},
    }


def _patch_extract_text_pages(
    monkeypatch: Any, text_per_pdf: dict[str, list[str]]
) -> None:
    """Replace ``extract_text_pages`` so tests don't need real PDFs with text."""

    def fake_extract(path: Path, max_pages: int = 3) -> list[str]:
        return text_per_pdf.get(path.name, [""]) [:max_pages]

    import zotai.s1.stage_04_enrich as mod

    monkeypatch.setattr(mod, "extract_text_pages", fake_extract)


# ─── 04a: happy path — new DOI found, OpenAlex resolves, reparent ────────


def test_04a_finds_new_doi_and_reparents_orphan(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "p.pdf")
    orphan_key = "ORPHAN01"
    new_doi = "10.1000/new-doi-found-in-text"
    _seed_orphan(
        settings,
        sha="a" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(
        monkeypatch,
        {pdf.name: [f"This paper has DOI {new_doi} on page 1.", "", ""]},
    )

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}})
    oa = FakeOpenAlexClient({new_doi: _good_openalex_work(new_doi)})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04a == 1
    assert result.items_no_progress == 0
    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == new_doi
    # Orphan was fetched and re-parented.
    assert zot.item_fetch_calls == [orphan_key]
    assert len(zot.updated_items) == 1
    assert zot.updated_items[0]["parentItem"] == zot.created_items[0]["key"]

    # DB state updated.
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 4
    assert item.import_route == "A"
    assert item.detected_doi == new_doi
    assert item.zotero_item_key == zot.created_items[0]["key"]
    assert item.metadata_json is not None


# ─── 04a: no new DOI → no_progress ────────────────────────────────────────


def test_04a_no_new_doi_returns_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "q.pdf")
    _seed_orphan(
        settings,
        sha="b" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN02",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["no identifiers here", "", ""]})

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04a == 0
    assert result.items_no_progress == 1
    assert oa.calls == [], "OpenAlex must not be queried without a new DOI"
    assert len(zot.created_items) == 0
    assert len(zot.updated_items) == 0


# ─── 04a: DOI present but matches Stage 01's — not new ────────────────────


def test_04a_skips_doi_that_matches_stage_01(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "r.pdf")
    known_doi = "10.1000/already-known"
    _seed_orphan(
        settings,
        sha="c" * 64,
        source_path=pdf,
        detected_doi=known_doi,
        zotero_item_key="ORPHAN03",
    )
    _patch_extract_text_pages(
        monkeypatch,
        {pdf.name: [f"This paper has DOI {known_doi} as before.", "", ""]},
    )

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert oa.calls == []


# ─── 04a: OpenAlex returns None → no_progress ────────────────────────────


def test_04a_openalex_404_is_no_progress(tmp_path: Path, monkeypatch: Any) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "s.pdf")
    new_doi = "10.1000/not-in-openalex"
    _seed_orphan(
        settings,
        sha="d" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN04",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]})

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({new_doi: None})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert result.items_enriched_04a == 0
    assert len(zot.created_items) == 0
    assert [r.error for r in result.rows] == ["openalex_404"]


# ─── 04a: quality gate fail (no title) → no_progress ─────────────────────


def test_04a_quality_gate_fail_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "t.pdf")
    new_doi = "10.1000/bad-metadata"
    _seed_orphan(
        settings,
        sha="e" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN05",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]})

    work = _good_openalex_work(new_doi)
    work["title"] = ""  # forces the quality gate to fail
    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({new_doi: work})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert [r.error for r in result.rows] == ["quality_gate_failed"]


# ─── 04a: dedup — existing Zotero item with PDF → link, no reparent ──────


def test_04a_dedup_existing_parent_with_pdf_links_without_reparent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "u.pdf")
    new_doi = "10.1000/already-in-zotero"
    _seed_orphan(
        settings,
        sha="f" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN06",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]})

    existing_key = "EXISTING01"
    zot = FakeZoteroClient(
        existing=[{"key": existing_key, "data": {"DOI": new_doi}}],
        existing_children={
            existing_key: [
                {
                    "key": "OLD_PDF",
                    "data": {
                        "itemType": "attachment",
                        "contentType": "application/pdf",
                    },
                }
            ]
        },
    )
    oa = FakeOpenAlexClient({new_doi: _good_openalex_work(new_doi)})

    result = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04a == 1
    assert len(zot.created_items) == 0, "Do not create a second parent for the same DOI"
    assert len(zot.updated_items) == 0, "Existing parent has PDF — do not reparent our orphan"

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.zotero_item_key == existing_key
    assert item.import_route == "A"


# ─── 04a: dry-run ────────────────────────────────────────────────────────


def test_04a_dry_run_probes_but_writes_nothing(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "v.pdf")
    new_doi = "10.1000/dry-run-doi"
    _seed_orphan(
        settings,
        sha="g" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN07",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]})

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient({new_doi: _good_openalex_work(new_doi)})

    result = run_enrich(
        substage="04a",
        dry_run=True,
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert [r.status for r in result.rows] == ["dry_run"]
    assert len(zot.created_items) == 0
    assert len(zot.updated_items) == 0
    # CSV has the _dryrun suffix.
    assert "_dryrun" in result.csv_path.name

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 3, "Dry-run must not advance stage"
    assert item.import_route == "C"


# ─── 04a: idempotency — second run is a no-op ────────────────────────────


def test_04a_rerun_is_noop_after_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "w.pdf")
    new_doi = "10.1000/idempotent"
    orphan_key = "ORPHAN08"
    _seed_orphan(
        settings,
        sha="h" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}})
    oa = FakeOpenAlexClient({new_doi: _good_openalex_work(new_doi)})

    first = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )
    assert first.items_enriched_04a == 1

    # Re-run: the item now has stage_completed=4 + import_route='A', so
    # the _select_eligible() query filters it out.
    second = run_enrich(
        substage="04a",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )
    assert second.items_enriched_04a == 0
    assert second.items_processed == 0
    assert len(second.rows) == 0


# ─── Substage not implemented ────────────────────────────────────────────


def test_04b_not_implemented_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    try:
        run_enrich(substage="04b", settings=settings)
    except NotImplementedError as exc:
        assert "04b" in str(exc)
    else:  # pragma: no cover
        assert False, "expected NotImplementedError"
