"""Tests for :mod:`zotai.s1.stage_04_enrich` — substages 04a, 04b, 04c.

Structure mirrors ``test_stage_03.py``: a fake ``ZoteroClient`` records
every call; fake ``OpenAlexClient`` / ``SemanticScholarClient`` return
scripted responses. Tests drive :func:`run_enrich` (sync wrapper) with
the substage under test.

Covered substages:

- **04a** — identifier regex + OpenAlex DOI retry (from PR 1/3).
- **04b** — OpenAlex fuzzy title match (PR 2/3).
- **04c** — Semantic Scholar fuzzy title match (PR 2/3).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from zotai.config import PathSettings, Settings, ZoteroSettings
from zotai.s1.stage_04_enrich import map_semantic_scholar_to_zotero, run_enrich
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
    def __init__(
        self,
        responses: dict[str, dict[str, Any] | None] | None = None,
        *,
        search_responses: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._search_responses = search_responses or {}
        self.calls: list[str] = []
        self.search_calls: list[tuple[str, int]] = []

    async def work_by_doi(self, doi: str) -> dict[str, Any] | None:
        self.calls.append(doi.lower())
        return self._responses.get(doi.lower())

    async def search_works(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        self.search_calls.append((title, per_page))
        return self._search_responses.get(title, [])


class FakeSemanticScholarClient:
    def __init__(
        self, search_responses: dict[str, list[dict[str, Any]]] | None = None
    ) -> None:
        self._search_responses = search_responses or {}
        self.search_calls: list[tuple[str, int, str]] = []

    async def search_paper(
        self,
        query: str,
        *,
        limit: int = 5,
        fields: str = "title,authors,year,venue,abstract,externalIds",
    ) -> list[dict[str, Any]]:
        self.search_calls.append((query, limit, fields))
        return self._search_responses.get(query, [])


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


def _patch_extract_probable_title(
    monkeypatch: Any, title_per_pdf: dict[str, str | None]
) -> None:
    """Replace ``extract_probable_title`` so tests don't need typeset PDFs.

    The real heuristic depends on font-size information that pdfplumber only
    emits for real PDFs; dummy fixtures don't carry that, so we substitute
    a lookup table keyed by filename.
    """

    def fake_title(path: Path) -> str | None:
        return title_per_pdf.get(path.name)

    import zotai.s1.stage_04_enrich as mod

    monkeypatch.setattr(mod, "extract_probable_title", fake_title)


def _ss_paper(
    title: str,
    *,
    doi: str | None = None,
    year: int = 2024,
    venue: str = "Journal of Fakery",
    abstract: str | None = "A fake abstract.",
) -> dict[str, Any]:
    """Build a minimal Semantic Scholar `/paper/search` result for tests."""
    payload: dict[str, Any] = {
        "title": title,
        "year": year,
        "venue": venue,
        "authors": [{"name": "Jane Doe"}, {"name": "John Smith"}],
    }
    if abstract is not None:
        payload["abstract"] = abstract
    if doi is not None:
        payload["externalIds"] = {"DOI": doi}
    return payload


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


# ─── 04b: happy path — fuzzy match → create parent → reparent ────────────


def test_04b_title_match_creates_parent_and_reparents(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b1.pdf")
    orphan_key = "ORPHAN10"
    title = "Fiscal multipliers in emerging economies: new evidence"
    doi = "10.1000/fuzzy-match-hit"
    _seed_orphan(
        settings,
        sha="1" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )
    # Perfect title match + a near-miss. The perfect one should win.
    oa = FakeOpenAlexClient(
        search_responses={
            title: [
                {
                    "title": "On something totally different",
                    "doi": "https://doi.org/10.1000/other",
                    "type": "journal-article",
                    "publication_year": 2024,
                    "authorships": [
                        {"author": {"display_name": "Alice Smith"}}
                    ],
                    "primary_location": {"source": {"display_name": "X"}},
                    "abstract_inverted_index": None,
                },
                _good_openalex_work(doi, title=title),
            ]
        }
    )

    result = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04b == 1
    assert result.items_enriched_04a == 0
    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == doi
    assert zot.created_items[0]["payload"]["title"] == title
    assert zot.item_fetch_calls == [orphan_key]
    assert len(zot.updated_items) == 1
    assert zot.updated_items[0]["parentItem"] == zot.created_items[0]["key"]
    assert oa.search_calls == [(title, 5)]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 4
    assert item.import_route == "A"
    assert item.detected_doi == doi
    assert item.zotero_item_key == zot.created_items[0]["key"]


# ─── 04b: all candidates below fuzzy threshold → no_progress ─────────────


def test_04b_no_fuzzy_match_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b2.pdf")
    _seed_orphan(
        settings,
        sha="2" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN11",
    )
    _patch_extract_probable_title(
        monkeypatch, {pdf.name: "A very specific paper title"}
    )

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient(
        search_responses={
            "A very specific paper title": [
                {
                    "title": "Something completely unrelated about pandas",
                    "doi": "https://doi.org/10.1000/p",
                    "type": "journal-article",
                    "publication_year": 2020,
                    "authorships": [
                        {"author": {"display_name": "Nobody Ever"}}
                    ],
                    "primary_location": {"source": {"display_name": "Z"}},
                    "abstract_inverted_index": None,
                }
            ]
        }
    )

    result = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert result.items_enriched_04b == 0
    assert len(zot.created_items) == 0
    assert len(zot.updated_items) == 0


# ─── 04b: extract_probable_title None → skipped_generic_title ────────────


def test_04b_generic_title_is_skipped(tmp_path: Path, monkeypatch: Any) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b3.pdf")
    _seed_orphan(
        settings,
        sha="3" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN12",
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: None})

    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient()

    result = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_skipped_generic_title == 1
    assert result.items_enriched_04b == 0
    # OpenAlex search is never called when title extraction short-circuits.
    assert oa.search_calls == []


# ─── 04b: fuzzy hit but quality gate fails → no_progress with error ──────


def test_04b_quality_gate_fail_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b4.pdf")
    title = "Fiscal multipliers in emerging economies: new evidence"
    _seed_orphan(
        settings,
        sha="4" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN13",
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    # Passes fuzzy (title identical) but has zero authorships.
    weak_work = _good_openalex_work("10.1000/weak", title=title)
    weak_work["authorships"] = []
    zot = FakeZoteroClient()
    oa = FakeOpenAlexClient(search_responses={title: [weak_work]})

    result = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert [r.error for r in result.rows] == ["quality_gate_failed"]
    assert len(zot.created_items) == 0


# ─── 04b: dedup on matched DOI (ADR 014 — existing has PDF) ──────────────


def test_04b_dedup_existing_with_pdf_links_without_reparent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b5.pdf")
    title = "Dedup match paper about macro"
    doi = "10.1000/already-in-zotero-2"
    _seed_orphan(
        settings,
        sha="5" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key="ORPHAN14",
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    existing_key = "EXISTING02"
    zot = FakeZoteroClient(
        existing=[{"key": existing_key, "data": {"DOI": doi}}],
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
    oa = FakeOpenAlexClient(
        search_responses={title: [_good_openalex_work(doi, title=title)]}
    )

    result = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04b == 1
    assert len(zot.created_items) == 0, "Dedup must not create a duplicate parent"
    assert len(zot.updated_items) == 0, "Existing has PDF → do not reparent"

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.zotero_item_key == existing_key
    assert item.import_route == "A"


# ─── 04b: idempotent re-run — second call is a no-op ─────────────────────


def test_04b_rerun_is_noop_after_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "b6.pdf")
    orphan_key = "ORPHAN15"
    title = "Idempotent 04b paper"
    doi = "10.1000/idempotent-04b"
    _seed_orphan(
        settings,
        sha="6" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )
    oa = FakeOpenAlexClient(
        search_responses={title: [_good_openalex_work(doi, title=title)]}
    )

    first = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )
    assert first.items_enriched_04b == 1

    second = run_enrich(
        substage="04b",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )
    assert second.items_enriched_04b == 0
    assert second.items_processed == 0
    assert len(second.rows) == 0


# ─── 04c: happy path via Semantic Scholar ────────────────────────────────


def test_04c_semantic_scholar_match(tmp_path: Path, monkeypatch: Any) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "c1.pdf")
    orphan_key = "ORPHAN20"
    title = "Informalidad laboral en America Latina"
    doi = "10.1000/ss-match"
    _seed_orphan(
        settings,
        sha="7" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )
    ss = FakeSemanticScholarClient(
        search_responses={title: [_ss_paper(title, doi=doi)]}
    )

    result = run_enrich(
        substage="04c",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04c == 1
    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == doi
    assert zot.created_items[0]["payload"]["itemType"] == "journalArticle"
    assert len(zot.updated_items) == 1
    # Regression guard: search_paper must be called with the fields param.
    assert ss.search_calls == [
        (title, 5, "title,authors,year,venue,abstract,externalIds")
    ]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 4
    assert item.import_route == "A"
    assert item.detected_doi == doi


# ─── 04c: Semantic Scholar paper without DOI → parent still created ──────


def test_04c_match_without_doi_creates_parent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "c2.pdf")
    orphan_key = "ORPHAN21"
    title = "Paper sin DOI en Semantic Scholar"
    _seed_orphan(
        settings,
        sha="8" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )
    ss = FakeSemanticScholarClient(
        search_responses={title: [_ss_paper(title, doi=None, abstract=None)]}
    )

    result = run_enrich(
        substage="04c",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04c == 1
    assert len(zot.created_items) == 1
    # No DOI → payload omits the DOI field.
    assert "DOI" not in zot.created_items[0]["payload"]
    # items() is only called by the dedup path; no DOI → never queried.
    assert zot.items_calls == []

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    # detected_doi stays None because the match didn't supply one.
    assert item.detected_doi is None
    assert item.import_route == "A"
    assert item.stage_completed == 4


# ─── map_semantic_scholar_to_zotero: quality gate checks ─────────────────


def test_map_ss_quality_gate_rejects_empty_title() -> None:
    assert map_semantic_scholar_to_zotero({"title": "", "authors": [{"name": "Jane"}]}) is None


def test_map_ss_quality_gate_rejects_no_authors() -> None:
    assert map_semantic_scholar_to_zotero({"title": "Good title", "authors": []}) is None


def test_map_ss_builds_payload_minimal() -> None:
    payload = map_semantic_scholar_to_zotero(_ss_paper("A fine title"))
    assert payload is not None
    assert payload["title"] == "A fine title"
    assert payload["itemType"] == "journalArticle"
    assert payload["date"] == "2024"
    assert payload["publicationTitle"] == "Journal of Fakery"
    assert len(payload["creators"]) == 2


# ─── Substage not yet implemented: 04d raises (04e also blocked) ─────────


def test_04d_not_implemented_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    try:
        run_enrich(substage="04d", settings=settings)
    except NotImplementedError as exc:
        assert "04d" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected NotImplementedError")


def test_04e_not_implemented_raises(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    try:
        run_enrich(substage="04e", settings=settings)
    except NotImplementedError as exc:
        assert "04e" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected NotImplementedError")
