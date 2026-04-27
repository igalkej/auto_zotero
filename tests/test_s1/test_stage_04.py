"""Tests for :mod:`zotai.s1.stage_04_enrich` — substages 04a-04e + cascade.

Structure mirrors ``test_stage_03.py``: a fake ``ZoteroClient`` records
every call; fake ``OpenAlexClient`` / ``SemanticScholarClient`` /
``OpenAIClient`` return scripted responses. Tests drive :func:`run_enrich`
(sync wrapper) with the substage under test.

Covered substages:

- **04a** — identifier regex + OpenAlex DOI retry (from PR 1/3).
- **04b** — OpenAlex fuzzy title match (PR 2/3).
- **04c** — Semantic Scholar fuzzy title match (PR 2/3).
- **04d** — LLM JSON extraction via ``gpt-4o-mini`` (PR 3/3).
- **04e** — Quarantine: tag + collection + separate report CSV (PR 3/3).
- **all** — Per-item cascade through every substage with budget awareness (PR 3/3).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from sqlmodel import Session, select

from zotai.api.doaj import map_doaj_to_zotero
from zotai.api.openai_client import BudgetExceededError
from zotai.api.scielo import map_scielo_to_zotero
from zotai.config import BehaviorSettings, PathSettings, Settings, ZoteroSettings
from zotai.s1.stage_04_enrich import (
    LLMExtractedMetadata,
    map_llm_extraction_to_zotero,
    map_semantic_scholar_to_zotero,
    run_enrich,
)
from zotai.state import Item, init_s1, make_s1_engine

# ─── Fakes ─────────────────────────────────────────────────────────────────


class FakeZoteroClient:
    """Minimal in-memory stand-in for ``ZoteroClient``. Covers 04a-04e surface."""

    def __init__(
        self,
        *,
        existing: list[dict[str, Any]] | None = None,
        existing_children: dict[str, list[dict[str, Any]]] | None = None,
        orphans: dict[str, dict[str, Any]] | None = None,
        existing_collections: list[dict[str, Any]] | None = None,
    ) -> None:
        self._existing = existing or []
        self._existing_children = existing_children or {}
        self._orphans = orphans or {}
        self._collections: list[dict[str, Any]] = list(existing_collections or [])
        self.dry_run = False
        self.created_items: list[dict[str, Any]] = []
        self.items_calls: list[dict[str, Any]] = []
        self.children_calls: list[str] = []
        self.item_fetch_calls: list[str] = []
        self.updated_items: list[dict[str, Any]] = []
        self.created_collections: list[dict[str, Any]] = []
        self.addto_collection_calls: list[tuple[str, str]] = []
        self.add_tags_calls: list[tuple[str, list[str]]] = []
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
            # pyzotero's shape: top-level ``key`` mirrored inside ``data``.
            return {"key": item_key, "data": self._orphans[item_key]}
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

    def collections(self, **kwargs: Any) -> list[dict[str, Any]]:
        return list(self._collections)

    def create_collections(
        self, payload: list[dict[str, Any]]
    ) -> dict[str, Any]:
        success: dict[str, str] = {}
        for idx, entry in enumerate(payload):
            key = self._key("COLL")
            created = {"key": key, "data": {"key": key, "name": entry["name"]}}
            self._collections.append(created)
            self.created_collections.append(created)
            success[str(idx)] = key
        return {"success": success, "unchanged": {}, "failed": {}}

    def addto_collection(
        self, collection_key: str, item: dict[str, Any]
    ) -> bool:
        item_key = item.get("key") or ""
        self.addto_collection_calls.append((collection_key, item_key))
        return True

    def add_tags(self, item: dict[str, Any], tags: list[str]) -> bool:
        item_key = item.get("key") or ""
        self.add_tags_calls.append((item_key, list(tags)))
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


class FakeOpenAIClient:
    """Mimics ``OpenAIClient.extract_metadata`` — returns queued responses.

    Each queued entry is either a JSON-string body (wrapped into a minimal
    response object on demand), a ``BudgetExceededError`` instance (raised),
    or an arbitrary exception (raised) so tests can exercise retry + error
    paths.
    """

    def __init__(self, queue: list[str | Exception] | None = None) -> None:
        self._queue: list[str | Exception] = list(queue or [])
        self.extract_calls: list[str] = []

    async def extract_metadata(
        self, *, text: str, model: str = "gpt-4o-mini"
    ) -> Any:
        self.extract_calls.append(text)
        if not self._queue:
            raise AssertionError("extract_metadata called more times than queued")
        nxt = self._queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _fake_usage_record(nxt, model=model)


def _fake_usage_record(content: str, *, model: str) -> Any:
    """Build an object that duck-types what ``_parse_llm_response`` reads."""

    class _Message:
        def __init__(self, c: str) -> None:
            self.content = c

    class _Choice:
        def __init__(self, c: str) -> None:
            self.message = _Message(c)

    class _Response:
        def __init__(self, c: str) -> None:
            self.choices = [_Choice(c)]
            self.usage = type(
                "U", (), {"prompt_tokens": 100, "completion_tokens": 50}
            )()

    class _Usage:
        def __init__(self, resp: Any) -> None:
            self.response = resp
            self.model = model
            self.prompt_tokens = 100
            self.completion_tokens = 50
            self.cost_usd = 0.0

    return _Usage(_Response(content))


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


class FakeSciELoClient:
    """Fake :class:`zotai.api.scielo.SciELoClient` for the 04bs tests.

    Returns scripted responses keyed by query title, or raises a queued
    exception (used to simulate Cloudflare 403, rate-limit 429, etc.).
    """

    def __init__(
        self,
        search_responses: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raise_on_search: Exception | None = None,
    ) -> None:
        self._search_responses = search_responses or {}
        self._raise = raise_on_search
        self.search_calls: list[tuple[str, int]] = []

    async def search_articles(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        self.search_calls.append((title, per_page))
        if self._raise is not None:
            raise self._raise
        return self._search_responses.get(title, [])


class FakeDOAJClient:
    """Fake :class:`zotai.api.doaj.DOAJClient` for the 04bd tests."""

    def __init__(
        self,
        search_responses: dict[str, list[dict[str, Any]]] | None = None,
        *,
        raise_on_search: Exception | None = None,
    ) -> None:
        self._search_responses = search_responses or {}
        self._raise = raise_on_search
        self.search_calls: list[tuple[str, int]] = []

    async def search_articles(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        self.search_calls.append((title, per_page))
        if self._raise is not None:
            raise self._raise
        return self._search_responses.get(title, [])


def _no_sleep() -> Callable[[float], Awaitable[None]]:
    async def _s(_: float) -> None:
        return None

    return _s


# ─── Fixtures / helpers ───────────────────────────────────────────────────


def _settings(
    tmp_path: Path,
    *,
    enable_scielo: bool = False,
    enable_doaj: bool = False,
) -> Settings:
    """Test fixture for ``Settings``.

    The 04bs / 04bd flags default to ``False`` here so existing tests that
    pre-date ADR 018 don't try to construct real
    :class:`SciELoClient` / :class:`DOAJClient` instances. New tests that
    cover the substages set the flags explicitly.
    """
    return Settings(
        paths=PathSettings(
            state_db=tmp_path / "state.db",
            reports_folder=tmp_path / "reports",
            staging_folder=tmp_path / "staging",
            pdf_source_folders=[],
        ),
        zotero=ZoteroSettings(library_id="123", library_type="user", local_api=True),
        behavior=BehaviorSettings(
            s1_enable_scielo=enable_scielo,
            s1_enable_doaj=enable_doaj,
        ),
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


# ─── 04d: happy path — JSON extraction → Zotero parent ──────────────────


def _llm_json(
    *,
    title: str = "A discovered paper",
    year: int = 2024,
    item_type: str = "journalArticle",
    doi: str | None = "10.1000/llm-found",
    venue: str = "Journal of LLMs",
    abstract: str = "Abstract discovered by the LLM.",
    authors: list[dict[str, str]] | None = None,
) -> str:
    """Helper: valid JSON body the LLM is expected to emit for 04d."""
    body: dict[str, Any] = {
        "title": title,
        "authors": authors
        if authors is not None
        else [
            {"first": "Jane", "last": "Doe"},
            {"first": "John", "last": "Smith"},
        ],
        "year": year,
        "item_type": item_type,
        "venue": venue,
        "abstract": abstract,
    }
    if doi is not None:
        body["doi"] = doi
    return json.dumps(body)


def test_04d_extracts_and_creates_parent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "d1.pdf")
    orphan_key = "ORPHAN30"
    _seed_orphan(
        settings,
        sha="A" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(
        monkeypatch,
        {pdf.name: ["Page 1 text about something", "Page 2 continues"]},
    )

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )
    oa_client = FakeOpenAIClient(queue=[_llm_json()])

    result = run_enrich(
        substage="04d",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openai_client=oa_client,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04d == 1
    assert len(zot.created_items) == 1
    payload = zot.created_items[0]["payload"]
    assert payload["title"] == "A discovered paper"
    assert payload["DOI"] == "10.1000/llm-found"
    assert payload["itemType"] == "journalArticle"
    assert len(zot.updated_items) == 1
    assert zot.updated_items[0]["parentItem"] == zot.created_items[0]["key"]

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.stage_completed == 4
    assert item.import_route == "A"
    assert item.detected_doi == "10.1000/llm-found"


# ─── 04d: malformed JSON → retry once → succeed on second attempt ────────


def test_04d_retries_once_on_malformed_json(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "d2.pdf")
    _seed_orphan(
        settings,
        sha="B" * 64,
        source_path=pdf,
        zotero_item_key="ORPHAN31",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["Paper text", ""]})

    zot = FakeZoteroClient(orphans={"ORPHAN31": {"key": "ORPHAN31"}})
    # First response is malformed; second is valid.
    oa_client = FakeOpenAIClient(queue=["not json at all {{{", _llm_json()])

    result = run_enrich(
        substage="04d",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openai_client=oa_client,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04d == 1
    assert len(oa_client.extract_calls) == 2, "Second attempt should have fired"


# ─── 04d: both attempts invalid → no_progress ────────────────────────────


def test_04d_exhausts_retries_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "d3.pdf")
    _seed_orphan(
        settings,
        sha="C" * 64,
        source_path=pdf,
        zotero_item_key="ORPHAN32",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["text", ""]})

    zot = FakeZoteroClient()
    oa_client = FakeOpenAIClient(queue=["garbage", "still garbage"])

    result = run_enrich(
        substage="04d",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openai_client=oa_client,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert result.items_enriched_04d == 0
    assert [r.error for r in result.rows] == ["llm_json_invalid"]
    assert len(zot.created_items) == 0


# ─── 04d: budget exceeded → row records budget_exceeded status ───────────


def test_04d_budget_exceeded_marks_status(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "d4.pdf")
    _seed_orphan(
        settings,
        sha="D" * 64,
        source_path=pdf,
        zotero_item_key="ORPHAN33",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["text", ""]})

    zot = FakeZoteroClient()
    oa_client = FakeOpenAIClient(
        queue=[BudgetExceededError("spent=$2.5, budget=$2.0")]
    )

    result = run_enrich(
        substage="04d",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openai_client=oa_client,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert [r.status for r in result.rows] == ["budget_exceeded"]
    assert result.items_enriched_04d == 0
    # Item is not marked as failed — budget exhaustion is recoverable on
    # the next run with a higher cap.
    assert result.items_failed == 0


# ─── 04d: quality gate (bad item_type) → no_progress ─────────────────────


def test_04d_invalid_item_type_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "d5.pdf")
    _seed_orphan(
        settings,
        sha="E" * 64,
        source_path=pdf,
        zotero_item_key="ORPHAN34",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["text", ""]})

    zot = FakeZoteroClient()
    oa_client = FakeOpenAIClient(
        queue=[_llm_json(item_type="tweet")]  # not in the allow-list
    )

    result = run_enrich(
        substage="04d",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openai_client=oa_client,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert [r.error for r in result.rows] == ["quality_gate_failed"]


# ─── 04e: happy path — quarantine creates collection, tags, writes CSV ──


def test_04e_quarantines_and_writes_report(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "e1.pdf")
    orphan_key = "ORPHAN40"
    _seed_orphan(
        settings,
        sha="F" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    # Give 04e something to put into the text_snippet column.
    _patch_extract_text_pages(
        monkeypatch,
        {pdf.name: ["Some first page text that should appear in the snippet.", ""]},
    )

    zot = FakeZoteroClient(
        orphans={orphan_key: {"key": orphan_key, "itemType": "attachment"}}
    )

    result = run_enrich(
        substage="04e",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_quarantined == 1
    # Collection was created on-demand (no pre-existing Quarantine).
    assert len(zot.created_collections) == 1
    assert zot.created_collections[0]["data"]["name"] == "Quarantine"
    # Item was tagged + added to the collection.
    assert zot.add_tags_calls == [(orphan_key, ["needs-manual-review"])]
    assert zot.addto_collection_calls == [
        (zot.created_collections[0]["key"], orphan_key)
    ]
    # quarantine_report.csv was written with the snippet.
    assert result.quarantine_csv_path is not None
    assert result.quarantine_csv_path.exists()
    content = result.quarantine_csv_path.read_text(encoding="utf-8")
    assert "Some first page text" in content

    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.in_quarantine is True
    assert item.stage_completed == 4


# ─── 04e: reuses an already-existing Quarantine collection ───────────────


def test_04e_reuses_existing_quarantine_collection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "e2.pdf")
    _seed_orphan(
        settings,
        sha="G" * 64,
        source_path=pdf,
        zotero_item_key="ORPHAN41",
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["txt", ""]})

    zot = FakeZoteroClient(
        orphans={"ORPHAN41": {"key": "ORPHAN41"}},
        existing_collections=[
            {"key": "PREEXISTING_Q", "data": {"key": "PREEXISTING_Q", "name": "Quarantine"}}
        ],
    )

    result = run_enrich(
        substage="04e",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_quarantined == 1
    assert len(zot.created_collections) == 0, "Should not re-create Quarantine"
    assert zot.addto_collection_calls == [("PREEXISTING_Q", "ORPHAN41")]


# ─── Cascade 'all': 04a hits → never calls 04b/04c/04d ───────────────────


def test_cascade_all_stops_at_first_success(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "all1.pdf")
    orphan_key = "ORPHAN50"
    new_doi = "10.1000/cascade-hit-04a"
    _seed_orphan(
        settings,
        sha="H" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(
        monkeypatch, {pdf.name: [f"DOI {new_doi}", "", ""]}
    )

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient({new_doi: _good_openalex_work(new_doi)})
    ss = FakeSemanticScholarClient()
    llm = FakeOpenAIClient()  # no queue — would raise if called

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04a == 1
    assert result.items_enriched_04b == 0
    assert result.items_enriched_04c == 0
    assert result.items_enriched_04d == 0
    assert result.items_quarantined == 0
    assert ss.search_calls == [], "Semantic Scholar must not be called"
    assert llm.extract_calls == [], "LLM must not be called when 04a succeeded"


# ─── Cascade 'all': all free substages miss → 04d picks it up ────────────


def test_cascade_all_falls_through_to_04d(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "all2.pdf")
    orphan_key = "ORPHAN51"
    title = "A recalcitrant paper title"
    _seed_orphan(
        settings,
        sha="I" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    # No identifiers in the text → 04a misses.
    _patch_extract_text_pages(
        monkeypatch, {pdf.name: ["Body without ids", "More body", ""]}
    )
    # 04b/04c both use extract_probable_title and get no fuzzy match.
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    # Neither OpenAlex search nor Semantic Scholar return a fuzzy-match-able title.
    oa = FakeOpenAlexClient(
        search_responses={
            title: [_good_openalex_work("10.1000/x", title="Totally different")]
        }
    )
    ss = FakeSemanticScholarClient(
        search_responses={title: [_ss_paper("Unrelated")]}
    )
    llm = FakeOpenAIClient(queue=[_llm_json(title=title, doi="10.1000/llm-saved")])

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04d == 1
    assert result.items_quarantined == 0
    assert ss.search_calls == [(title, 5, "title,authors,year,venue,abstract,externalIds")]


# ─── Cascade 'all': everything fails → 04e quarantines ───────────────────


def test_cascade_all_quarantines_on_exhaustion(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    pdf = _write_pdf(tmp_path / "pdfs" / "all3.pdf")
    orphan_key = "ORPHAN52"
    title = "Another hopeless paper"
    _seed_orphan(
        settings,
        sha="J" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(
        monkeypatch, {pdf.name: ["No ids at all", "Nothing", ""]}
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient(search_responses={title: []})
    ss = FakeSemanticScholarClient(search_responses={title: []})
    # LLM emits a payload the quality gate rejects → falls through to 04e.
    llm = FakeOpenAIClient(
        queue=["garbage", "still garbage"]  # retry exhausts
    )

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_quarantined == 1
    assert result.items_enriched_04a == 0
    assert result.items_enriched_04d == 0
    assert result.quarantine_csv_path is not None
    engine = make_s1_engine(str(settings.paths.state_db))
    with Session(engine) as session:
        item = session.exec(select(Item)).one()
    assert item.in_quarantine is True


# ─── Cascade 'all': budget exceeded on first item routes rest to 04e ─────


def test_cascade_all_budget_tripped_skips_llm_for_rest(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path)
    # Two items that both need to reach 04d.
    pdf1 = _write_pdf(tmp_path / "pdfs" / "b1.pdf")
    pdf2 = _write_pdf(tmp_path / "pdfs" / "b2.pdf")
    _seed_orphan(settings, sha="K" * 64, source_path=pdf1, zotero_item_key="ORPH_B1")
    _seed_orphan(settings, sha="L" * 64, source_path=pdf2, zotero_item_key="ORPH_B2")
    _patch_extract_text_pages(
        monkeypatch, {pdf1.name: ["p1"], pdf2.name: ["p2"]}
    )
    _patch_extract_probable_title(
        monkeypatch, {pdf1.name: "Title one", pdf2.name: "Title two"}
    )

    zot = FakeZoteroClient(
        orphans={
            "ORPH_B1": {"key": "ORPH_B1"},
            "ORPH_B2": {"key": "ORPH_B2"},
        }
    )
    oa = FakeOpenAlexClient(
        search_responses={"Title one": [], "Title two": []}
    )
    ss = FakeSemanticScholarClient(
        search_responses={"Title one": [], "Title two": []}
    )
    # Budget trips on the first call; the second item must route to 04e
    # without any LLM call.
    llm = FakeOpenAIClient(
        queue=[BudgetExceededError("over budget")]
    )

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_quarantined == 2
    assert len(llm.extract_calls) == 1, "Second item must skip the LLM"


# ─── map_llm_extraction_to_zotero direct checks ─────────────────────────


def test_map_llm_requires_title_and_authors() -> None:
    # Missing title.
    assert (
        map_llm_extraction_to_zotero(
            LLMExtractedMetadata.model_validate(
                {
                    "title": "",
                    "authors": [{"first": "J", "last": "D"}],
                    "item_type": "journalArticle",
                }
            )
        )
        is None
    )
    # Missing authors.
    assert (
        map_llm_extraction_to_zotero(
            LLMExtractedMetadata.model_validate(
                {"title": "T", "authors": [], "item_type": "journalArticle"}
            )
        )
        is None
    )


def test_map_llm_rejects_unknown_item_type() -> None:
    extracted = LLMExtractedMetadata.model_validate(
        {
            "title": "Good",
            "authors": [{"first": "J", "last": "D"}],
            "item_type": "newsletter",  # off the allow-list
        }
    )
    assert map_llm_extraction_to_zotero(extracted) is None


def test_map_llm_builds_zotero_payload() -> None:
    extracted = LLMExtractedMetadata.model_validate(
        {
            "title": "Good paper",
            "authors": [{"first": "Jane", "last": "Doe"}],
            "year": 2022,
            "item_type": "preprint",
            "venue": "arXiv",
            "doi": "10.1000/x",
            "abstract": "abc",
        }
    )
    payload = map_llm_extraction_to_zotero(extracted)
    assert payload is not None
    assert payload["title"] == "Good paper"
    assert payload["itemType"] == "preprint"
    assert payload["DOI"] == "10.1000/x"
    assert payload["date"] == "2022"


# ─── Smoke: 04d without OPENAI_API_KEY raises StageAbortedError ──────────


def test_04d_without_api_key_raises_stage_aborted(tmp_path: Path) -> None:
    from zotai.s1.handler import StageAbortedError

    settings = _settings(tmp_path)  # empty openai.api_key
    try:
        run_enrich(substage="04d", settings=settings)
    except StageAbortedError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected StageAbortedError")


# ─────────────────────────────────────────────────────────────────────────
# 04bs (SciELO via Crossref Member 530, ADR 018 + ADR 019) and
# 04bd (DOAJ, ADR 018) tests.
# ─────────────────────────────────────────────────────────────────────────


def _good_crossref_record(
    doi: str = "10.1590/test-scielo-doi",
    title: str = "Informalidad laboral en Argentina",
    *,
    abstract: str | None = "<jats:p>Resumen <jats:italic>importante</jats:italic></jats:p>",
    container: str = "Desarrollo Económico",
    year: int = 2024,
    month: int | None = 7,
) -> dict[str, Any]:
    """Build a Crossref ``works`` record matching what filter:member:530 returns."""
    parts = [year]
    if month is not None:
        parts.append(month)
    rec: dict[str, Any] = {
        "DOI": doi,
        "title": [title],
        "container-title": [container],
        "type": "journal-article",
        "member": "530",
        "publisher": "FapUNIFESP (SciELO)",
        "published": {"date-parts": [parts]},
        "author": [
            {"given": "Jane", "family": "Doe", "ORCID": "https://orcid.org/0000-0000"},
            {"given": "John", "family": "Smith"},
        ],
    }
    if abstract is not None:
        rec["abstract"] = abstract
    return rec


def _good_doaj_record(
    doi: str = "10.5555/doaj-test-doi",
    title: str = "An open-access economics paper",
    *,
    journal: str = "Revista de Economía",
    year: str = "2023",
    month: str | None = "5",
    abstract: str = "Open-access abstract content.",
) -> dict[str, Any]:
    """Build a DOAJ article record (mimics ``payload['results'][i]``)."""
    bib: dict[str, Any] = {
        "title": title,
        "year": year,
        "author": [{"name": "Doe, Jane"}, {"name": "Smith, John"}],
        "journal": {"title": journal, "country": "AR", "language": ["spa"]},
        "abstract": abstract,
        "identifier": [{"type": "doi", "id": doi}],
    }
    if month is not None:
        bib["month"] = month
    return {"id": "doaj-internal-id", "bibjson": bib}


def _http_status_error(status: int, url: str) -> httpx.HTTPStatusError:
    """Build a ready-to-raise ``HTTPStatusError`` for resilience tests."""
    request = httpx.Request("GET", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"status {status}", request=request, response=response)


# ─── Mapper tests: SciELO (Crossref-shape) ──────────────────────────────


def test_map_scielo_to_zotero_full_payload() -> None:
    rec = _good_crossref_record()
    payload = map_scielo_to_zotero(rec)
    assert payload is not None
    assert payload["itemType"] == "journalArticle"
    assert payload["title"] == "Informalidad laboral en Argentina"
    assert payload["DOI"] == "10.1590/test-scielo-doi"
    assert payload["publicationTitle"] == "Desarrollo Económico"
    assert payload["date"] == "2024-07"
    assert payload["creators"][0] == {
        "creatorType": "author",
        "firstName": "Jane",
        "lastName": "Doe",
    }
    assert "Resumen importante" in payload["abstractNote"]
    assert "<" not in payload["abstractNote"], "JATS tags must be stripped"


def test_map_scielo_to_zotero_returns_none_on_missing_title() -> None:
    rec = _good_crossref_record()
    rec["title"] = []
    assert map_scielo_to_zotero(rec) is None
    rec["title"] = [""]
    assert map_scielo_to_zotero(rec) is None


def test_map_scielo_to_zotero_returns_none_on_missing_authors() -> None:
    rec = _good_crossref_record()
    rec["author"] = []
    assert map_scielo_to_zotero(rec) is None
    # Authors with neither given nor family nor name → still None.
    rec["author"] = [{"sequence": "first"}]
    assert map_scielo_to_zotero(rec) is None


def test_map_scielo_to_zotero_decodes_html_entities_and_year_only() -> None:
    rec = _good_crossref_record(month=None)
    rec["container-title"] = ["Ciência &amp; Saúde Coletiva"]
    payload = map_scielo_to_zotero(rec)
    assert payload is not None
    assert payload["publicationTitle"] == "Ciência & Saúde Coletiva"
    assert payload["date"] == "2024", "year-only date when no month available"


# ─── Mapper tests: DOAJ ──────────────────────────────────────────────────


def test_map_doaj_to_zotero_full_payload() -> None:
    rec = _good_doaj_record()
    payload = map_doaj_to_zotero(rec)
    assert payload is not None
    assert payload["itemType"] == "journalArticle"
    assert payload["title"] == "An open-access economics paper"
    assert payload["DOI"] == "10.5555/doaj-test-doi"
    assert payload["publicationTitle"] == "Revista de Economía"
    assert payload["date"] == "2023-05"
    # "Doe, Jane" → firstName="Jane", lastName="Doe".
    assert payload["creators"][0] == {
        "creatorType": "author",
        "firstName": "Jane",
        "lastName": "Doe",
    }


def test_map_doaj_to_zotero_returns_none_on_missing_title() -> None:
    rec = _good_doaj_record()
    rec["bibjson"]["title"] = ""
    assert map_doaj_to_zotero(rec) is None
    del rec["bibjson"]["title"]
    assert map_doaj_to_zotero(rec) is None


def test_map_doaj_to_zotero_returns_none_on_missing_authors() -> None:
    rec = _good_doaj_record()
    rec["bibjson"]["author"] = []
    assert map_doaj_to_zotero(rec) is None
    rec["bibjson"]["author"] = [{"affiliation": "X"}]  # no name
    assert map_doaj_to_zotero(rec) is None


def test_map_doaj_to_zotero_extracts_doi_from_identifier_list() -> None:
    rec = _good_doaj_record()
    rec["bibjson"]["identifier"] = [
        {"type": "issn", "id": "1234-5678"},
        {"type": "doi", "id": "10.9999/correct-doi"},
        {"type": "eissn", "id": "8765-4321"},
    ]
    payload = map_doaj_to_zotero(rec)
    assert payload is not None
    assert payload["DOI"] == "10.9999/correct-doi"


# ─── 04bs cascade: title match → reparent ───────────────────────────────


def test_04bs_title_match_creates_parent_and_reparents(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_scielo=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bs1.pdf")
    orphan_key = "ORPH-BS1"
    title = "Política fiscal y crecimiento"
    _seed_orphan(
        settings,
        sha="b" * 63 + "s",
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    sce = FakeSciELoClient(
        search_responses={title: [_good_crossref_record(title=title)]},
    )

    result = run_enrich(
        substage="04bs",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bs == 1
    assert sce.search_calls == [(title, 5)]
    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == "10.1590/test-scielo-doi"
    assert len(zot.updated_items) == 1, "orphan must be reparented"


def test_04bs_no_fuzzy_match_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_scielo=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bs2.pdf")
    orphan_key = "ORPH-BS2"
    title = "An obscure recalcitrant title"
    _seed_orphan(
        settings,
        sha="c" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    sce = FakeSciELoClient(
        search_responses={
            title: [_good_crossref_record(title="Totally unrelated paper")]
        },
    )

    result = run_enrich(
        substage="04bs",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bs == 0
    assert result.items_no_progress == 1
    assert zot.created_items == []


# ─── 04bd cascade: title match → reparent ───────────────────────────────


def test_04bd_title_match_creates_parent_and_reparents(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bd1.pdf")
    orphan_key = "ORPH-BD1"
    title = "Open access study on inflation"
    _seed_orphan(
        settings,
        sha="d" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    do = FakeDOAJClient(
        search_responses={title: [_good_doaj_record(title=title)]},
    )

    result = run_enrich(
        substage="04bd",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bd == 1
    assert do.search_calls == [(title, 5)]
    assert len(zot.created_items) == 1
    assert zot.created_items[0]["payload"]["DOI"] == "10.5555/doaj-test-doi"


def test_04bd_no_fuzzy_match_is_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bd2.pdf")
    orphan_key = "ORPH-BD2"
    title = "Another nothing-matches title"
    _seed_orphan(
        settings,
        sha="e" * 64,
        source_path=pdf,
        detected_doi=None,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    do = FakeDOAJClient(
        search_responses={title: [_good_doaj_record(title="Off-topic paper")]},
    )

    result = run_enrich(
        substage="04bd",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bd == 0
    assert result.items_no_progress == 1


# ─── Resilience: HTTP transients fall through, others fail ──────────────


def test_04bs_403_falls_through_as_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_scielo=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bs403.pdf")
    orphan_key = "ORPH-BS403"
    title = "Some title"
    _seed_orphan(
        settings,
        sha="f" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    sce = FakeSciELoClient(
        raise_on_search=_http_status_error(403, "https://api.crossref.org/works")
    )

    result = run_enrich(
        substage="04bs",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert result.items_failed == 0
    assert result.rows[0].error == "scielo_unavailable:403"


def test_04bs_500_returns_failed(tmp_path: Path, monkeypatch: Any) -> None:
    settings = _settings(tmp_path, enable_scielo=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bs500.pdf")
    orphan_key = "ORPH-BS500"
    title = "Unexpected server error path"
    _seed_orphan(
        settings,
        sha="g" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    sce = FakeSciELoClient(
        raise_on_search=_http_status_error(500, "https://api.crossref.org/works")
    )

    result = run_enrich(
        substage="04bs",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_failed == 1
    assert result.items_no_progress == 0
    assert result.rows[0].status == "failed"
    assert "scielo_error" in (result.rows[0].error or "")


def test_04bd_429_falls_through_as_no_progress(
    tmp_path: Path, monkeypatch: Any
) -> None:
    settings = _settings(tmp_path, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bd429.pdf")
    orphan_key = "ORPH-BD429"
    title = "Rate limited query"
    _seed_orphan(
        settings,
        sha="h" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    do = FakeDOAJClient(
        raise_on_search=_http_status_error(429, "https://doaj.org/api/v3/search/articles/x")
    )

    result = run_enrich(
        substage="04bd",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_no_progress == 1
    assert result.rows[0].error == "doaj_unavailable:429"


def test_04bd_500_returns_failed(tmp_path: Path, monkeypatch: Any) -> None:
    settings = _settings(tmp_path, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "bd500.pdf")
    orphan_key = "ORPH-BD500"
    title = "Server error path"
    _seed_orphan(
        settings,
        sha="i" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    do = FakeDOAJClient(
        raise_on_search=_http_status_error(500, "https://doaj.org/api/v3/search/articles/x")
    )

    result = run_enrich(
        substage="04bd",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_failed == 1
    assert "doaj_error" in (result.rows[0].error or "")


# ─── Feature flags ────────────────────────────────────────────────────────


def test_04bs_disabled_skips_substage_in_cascade(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When S1_ENABLE_SCIELO=false, the cascade goes 04b → 04bd (no 04bs)."""
    settings = _settings(tmp_path, enable_scielo=False, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "noflag.pdf")
    orphan_key = "ORPH-NOFLAG"
    title = "Disabled scielo cascade"
    _seed_orphan(
        settings,
        sha="j" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["body", "", ""]})
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient(search_responses={title: []})
    do = FakeDOAJClient(search_responses={title: [_good_doaj_record(title=title)]})
    ss = FakeSemanticScholarClient()
    llm = FakeOpenAIClient()

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bd == 1, "04bd must hit when 04bs is disabled"
    assert result.items_enriched_04bs == 0


def test_04bs_explicit_substage_aborts_when_disabled(tmp_path: Path) -> None:
    from zotai.s1.handler import StageAbortedError

    settings = _settings(tmp_path, enable_scielo=False)
    try:
        run_enrich(substage="04bs", settings=settings)
    except StageAbortedError as exc:
        assert "S1_ENABLE_SCIELO" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected StageAbortedError")


def test_04bd_disabled_skips_substage_in_cascade(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When S1_ENABLE_DOAJ=false, the cascade skips 04bd cleanly."""
    settings = _settings(tmp_path, enable_scielo=True, enable_doaj=False)
    pdf = _write_pdf(tmp_path / "pdfs" / "noflagd.pdf")
    orphan_key = "ORPH-NOFLAGD"
    title = "Disabled doaj cascade"
    _seed_orphan(
        settings,
        sha="k" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["body", "", ""]})
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient(search_responses={title: []})
    sce = FakeSciELoClient(
        search_responses={title: [_good_crossref_record(title=title)]}
    )
    ss = FakeSemanticScholarClient()
    llm = FakeOpenAIClient()

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bs == 1
    assert result.items_enriched_04bd == 0


def test_04bd_explicit_substage_aborts_when_disabled(tmp_path: Path) -> None:
    from zotai.s1.handler import StageAbortedError

    settings = _settings(tmp_path, enable_doaj=False)
    try:
        run_enrich(substage="04bd", settings=settings)
    except StageAbortedError as exc:
        assert "S1_ENABLE_DOAJ" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected StageAbortedError")


# ─── Combined cascade — 04bs surfaces before 04bd ────────────────────────


def test_cascade_uses_04bs_after_04b_misses(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``all``: 04a/04b miss, 04bs hits → 04bd / 04c / 04d are not invoked."""
    settings = _settings(tmp_path, enable_scielo=True, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "cas1.pdf")
    orphan_key = "ORPH-CAS1"
    title = "Cascade hit at 04bs"
    _seed_orphan(
        settings,
        sha="l" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["body without ids", "", ""]})
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient(search_responses={title: []})
    sce = FakeSciELoClient(
        search_responses={title: [_good_crossref_record(title=title)]}
    )
    do = FakeDOAJClient(search_responses={title: [_good_doaj_record(title=title)]})
    ss = FakeSemanticScholarClient(search_responses={title: [_ss_paper(title)]})
    llm = FakeOpenAIClient()

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bs == 1
    assert result.items_enriched_04bd == 0
    assert do.search_calls == [], "DOAJ must not be called once 04bs hits"
    assert ss.search_calls == [], "Semantic Scholar must not be called"
    assert llm.extract_calls == []


def test_cascade_uses_04bd_when_04b_and_04bs_miss(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``all``: 04a/04b/04bs miss, 04bd hits → 04c / 04d are not invoked."""
    settings = _settings(tmp_path, enable_scielo=True, enable_doaj=True)
    pdf = _write_pdf(tmp_path / "pdfs" / "cas2.pdf")
    orphan_key = "ORPH-CAS2"
    title = "Cascade hit at 04bd"
    _seed_orphan(
        settings,
        sha="m" * 64,
        source_path=pdf,
        zotero_item_key=orphan_key,
    )
    _patch_extract_text_pages(monkeypatch, {pdf.name: ["body without ids", "", ""]})
    _patch_extract_probable_title(monkeypatch, {pdf.name: title})

    zot = FakeZoteroClient(orphans={orphan_key: {"key": orphan_key}})
    oa = FakeOpenAlexClient(search_responses={title: []})
    sce = FakeSciELoClient(
        search_responses={title: [_good_crossref_record(title="Off-topic")]}
    )
    do = FakeDOAJClient(search_responses={title: [_good_doaj_record(title=title)]})
    ss = FakeSemanticScholarClient(search_responses={title: [_ss_paper(title)]})
    llm = FakeOpenAIClient()

    result = run_enrich(
        substage="all",
        settings=settings,
        zotero_client=zot,  # type: ignore[arg-type]
        openalex_client=oa,  # type: ignore[arg-type]
        scielo_client=sce,  # type: ignore[arg-type]
        doaj_client=do,  # type: ignore[arg-type]
        semantic_scholar_client=ss,  # type: ignore[arg-type]
        openai_client=llm,  # type: ignore[arg-type]
        sleep=_no_sleep(),
    )

    assert result.items_enriched_04bd == 1
    assert result.items_enriched_04bs == 0
    assert sce.search_calls == [(title, 5)], "SciELO is consulted before DOAJ"
    assert ss.search_calls == [], "Semantic Scholar must not be called once 04bd hits"


# ─── Spec compliance with ADR 018 + ADR 019 ──────────────────────────────


def test_default_flags_are_on() -> None:
    """ADR 018 + ADR 019 §Decision: both new substage flags default ON."""
    fields = BehaviorSettings.model_fields
    assert fields["s1_enable_scielo"].default is True
    assert fields["s1_enable_doaj"].default is True


def test_env_var_names_match_spec(tmp_path: Path, monkeypatch: Any) -> None:
    """Spec: env vars S1_ENABLE_SCIELO / S1_ENABLE_DOAJ map to the flags."""
    monkeypatch.chdir(tmp_path)  # avoid the repo's local .env
    monkeypatch.setenv("S1_ENABLE_SCIELO", "false")
    monkeypatch.setenv("S1_ENABLE_DOAJ", "false")
    b = BehaviorSettings()
    assert b.s1_enable_scielo is False
    assert b.s1_enable_doaj is False
