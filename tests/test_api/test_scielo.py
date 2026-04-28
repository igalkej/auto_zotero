"""Tests for :mod:`zotai.api.scielo`.

Per ADR 019, the substage hits Crossref filtered to ``member:530``
rather than ``search.scielo.org`` directly. The tests cover both halves
of the module:

- ``SciELoClient.search_articles`` — request shape (filter, query.title,
  rows, select), happy path, defensive parsing when the JSON does not
  match Crossref's documented shape.
- ``map_scielo_to_zotero`` — full mapping + every quality-gate path
  (missing title / no authors return ``None``), corporate-author
  handling, JATS abstract stripping.
- ``_doi_from_scielo_record`` and ``_date_from_crossref_published`` —
  the small helpers that surrounded the mapper.

HTTP is mocked with respx; no live network.
"""

from __future__ import annotations

from typing import Any

import httpx
import respx

from zotai.api.scielo import (
    CROSSREF_BASE,
    SCIELO_CROSSREF_MEMBER,
    SciELoClient,
    _date_from_crossref_published,
    _doi_from_scielo_record,
    map_scielo_to_zotero,
)

# ── SciELoClient.search_articles ──────────────────────────────────────────


def _crossref_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "ok", "message": {"items": items}}


async def test_search_articles_sends_member_filter_and_select() -> None:
    async with respx.mock() as router:
        route = router.get(f"{CROSSREF_BASE}/works").mock(
            return_value=httpx.Response(200, json=_crossref_payload([])),
        )
        client = SciELoClient(user_email="user@example.org")
        await client.search_articles("inflación", per_page=3)

    params = route.calls.last.request.url.params
    assert params["query.title"] == "inflación"
    assert params["filter"] == f"member:{SCIELO_CROSSREF_MEMBER}"
    assert params["rows"] == "3"
    assert "DOI" in params["select"]
    assert "title" in params["select"]


async def test_search_articles_returns_items_array() -> None:
    items = [{"DOI": "10.1590/abc", "title": ["Paper"]}]
    async with respx.mock() as router:
        router.get(f"{CROSSREF_BASE}/works").mock(
            return_value=httpx.Response(200, json=_crossref_payload(items)),
        )
        client = SciELoClient()
        out = await client.search_articles("paper")

    assert out == items


async def test_search_articles_returns_empty_when_message_missing() -> None:
    async with respx.mock() as router:
        router.get(f"{CROSSREF_BASE}/works").mock(
            return_value=httpx.Response(200, json={"status": "ok"}),
        )
        client = SciELoClient()
        out = await client.search_articles("paper")

    assert out == []


async def test_search_articles_returns_empty_when_items_missing() -> None:
    async with respx.mock() as router:
        router.get(f"{CROSSREF_BASE}/works").mock(
            return_value=httpx.Response(
                200, json={"status": "ok", "message": {"total-results": 0}}
            ),
        )
        client = SciELoClient()
        out = await client.search_articles("paper")

    assert out == []


# ── map_scielo_to_zotero ───────────────────────────────────────────────────


def _full_record() -> dict[str, Any]:
    return {
        "DOI": "10.1590/abc",
        "title": ["Inflación y crecimiento en LATAM"],
        "author": [
            {"given": "Jane", "family": "Doe"},
            {"given": "", "family": "Smith"},
        ],
        "published": {"date-parts": [[2024, 6, 15]]},
        "container-title": ["Revista CEPAL"],
        "abstract": "<jats:p>This paper discusses <jats:italic>X</jats:italic>.</jats:p>",
        "type": "journal-article",
    }


def test_map_scielo_to_zotero_happy_path() -> None:
    payload = map_scielo_to_zotero(_full_record())

    assert payload is not None
    assert payload["itemType"] == "journalArticle"
    assert payload["title"] == "Inflación y crecimiento en LATAM"
    assert payload["DOI"] == "10.1590/abc"
    assert payload["date"] == "2024-06-15"
    assert payload["publicationTitle"] == "Revista CEPAL"
    # JATS tags stripped, whitespace collapsed.
    assert payload["abstractNote"] == "This paper discusses X ."
    assert payload["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"},
        {"creatorType": "author", "firstName": "", "lastName": "Smith"},
    ]


def test_map_scielo_to_zotero_drops_record_without_title() -> None:
    record = _full_record()
    record["title"] = []
    assert map_scielo_to_zotero(record) is None


def test_map_scielo_to_zotero_drops_record_with_blank_title() -> None:
    record = _full_record()
    record["title"] = ["   "]
    assert map_scielo_to_zotero(record) is None


def test_map_scielo_to_zotero_drops_record_without_authors() -> None:
    record = _full_record()
    record["author"] = []
    assert map_scielo_to_zotero(record) is None


def test_map_scielo_to_zotero_falls_back_to_corporate_name() -> None:
    record = _full_record()
    record["author"] = [{"name": "Comisión Económica para América Latina"}]
    payload = map_scielo_to_zotero(record)
    assert payload is not None
    assert payload["creators"] == [
        {
            "creatorType": "author",
            "firstName": "",
            "lastName": "Comisión Económica para América Latina",
        }
    ]


def test_map_scielo_to_zotero_unescapes_html_entities_in_title_and_venue() -> None:
    record = _full_record()
    record["title"] = ["Café &amp; Sociedad"]
    record["container-title"] = ["Revista &lt;X&gt;"]
    payload = map_scielo_to_zotero(record)
    assert payload is not None
    assert payload["title"] == "Café & Sociedad"
    assert payload["publicationTitle"] == "Revista <X>"


def test_map_scielo_to_zotero_omits_doi_field_when_missing() -> None:
    record = _full_record()
    record["DOI"] = ""
    payload = map_scielo_to_zotero(record)
    assert payload is not None
    assert "DOI" not in payload


# ── _doi_from_scielo_record ────────────────────────────────────────────────


def test_doi_from_scielo_record_returns_stripped_doi() -> None:
    assert _doi_from_scielo_record({"DOI": "  10.1590/abc  "}) == "10.1590/abc"


def test_doi_from_scielo_record_returns_none_when_missing() -> None:
    assert _doi_from_scielo_record({}) is None


def test_doi_from_scielo_record_returns_none_when_blank() -> None:
    assert _doi_from_scielo_record({"DOI": "   "}) is None


# ── _date_from_crossref_published ──────────────────────────────────────────


def test_date_from_crossref_published_year_only() -> None:
    assert _date_from_crossref_published({"date-parts": [[2024]]}) == "2024"


def test_date_from_crossref_published_year_month() -> None:
    assert _date_from_crossref_published({"date-parts": [[2024, 6]]}) == "2024-06"


def test_date_from_crossref_published_year_month_day() -> None:
    assert (
        _date_from_crossref_published({"date-parts": [[2024, 6, 5]]}) == "2024-06-05"
    )


def test_date_from_crossref_published_returns_empty_for_missing_input() -> None:
    assert _date_from_crossref_published(None) == ""
    assert _date_from_crossref_published({}) == ""
    assert _date_from_crossref_published({"date-parts": []}) == ""
    assert _date_from_crossref_published({"date-parts": [[]]}) == ""


def test_date_from_crossref_published_handles_non_int_year() -> None:
    assert _date_from_crossref_published({"date-parts": [["2024"]]}) == ""
