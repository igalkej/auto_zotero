"""Tests for :mod:`zotai.api.doaj`.

The DOAJ adapter is the 04bd substage's HTTP client + a Zotero payload
mapper + two small parsing helpers. Coverage matches:

- ``DOAJClient.search_articles``: query construction (URL-quoted Lucene
  query against ``bibjson.title``), ``pageSize`` / ``page`` params,
  defensive parsing.
- ``map_doaj_to_zotero``: every quality-gate path (missing bibjson,
  blank title, no authors → ``None``); abstract / venue / DOI / date
  population; comma-form vs Western-order author parsing.
- ``_doi_from_doaj_record``: identifier list parsing — DOI vs other
  types, missing list, malformed entries.
- ``_split_doaj_name``: comma form, Western order, single token,
  empty / whitespace-only input, leading/trailing whitespace.
- ``_date_from_doaj``: year as str / int, with / without month, invalid
  month, missing year.

HTTP is mocked with respx; no live network.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import unquote

import httpx
import respx

from zotai.api.doaj import (
    DOAJ_API_BASE,
    DOAJClient,
    _date_from_doaj,
    _doi_from_doaj_record,
    _split_doaj_name,
    map_doaj_to_zotero,
)

# ── DOAJClient.search_articles ────────────────────────────────────────────


async def test_search_articles_quotes_title_in_lucene_query() -> None:
    async with respx.mock() as router:
        # The query is URL-encoded into the path — match by base prefix.
        route = router.get(
            url__startswith=f"{DOAJ_API_BASE}/search/articles/"
        ).mock(return_value=httpx.Response(200, json={"results": []}))
        client = DOAJClient()
        await client.search_articles("Inflación")

    request = route.calls.last.request
    decoded = unquote(str(request.url.path))
    assert 'bibjson.title:"Inflación"' in decoded


async def test_search_articles_passes_paging_params() -> None:
    async with respx.mock() as router:
        route = router.get(
            url__startswith=f"{DOAJ_API_BASE}/search/articles/"
        ).mock(return_value=httpx.Response(200, json={"results": []}))
        client = DOAJClient()
        await client.search_articles("foo", per_page=3)

    params = route.calls.last.request.url.params
    assert params["pageSize"] == "3"
    assert params["page"] == "1"


async def test_search_articles_returns_results_array() -> None:
    sample = [{"id": "1", "bibjson": {"title": "X"}}]
    async with respx.mock() as router:
        router.get(url__startswith=f"{DOAJ_API_BASE}/search/articles/").mock(
            return_value=httpx.Response(200, json={"results": sample})
        )
        client = DOAJClient()
        out = await client.search_articles("x")

    assert out == sample


async def test_search_articles_returns_empty_when_results_missing() -> None:
    async with respx.mock() as router:
        router.get(url__startswith=f"{DOAJ_API_BASE}/search/articles/").mock(
            return_value=httpx.Response(200, json={"timestamp": "..."})
        )
        client = DOAJClient()
        out = await client.search_articles("x")

    assert out == []


# ── map_doaj_to_zotero ─────────────────────────────────────────────────────


def _full_record() -> dict[str, Any]:
    return {
        "bibjson": {
            "title": "Inflación en LATAM",
            "author": [
                {"name": "Doe, Jane"},
                {"name": "Smith Jr."},
            ],
            "year": "2024",
            "month": "06",
            "journal": {"title": "Revista DOAJ"},
            "abstract": "An abstract.",
            "identifier": [
                {"type": "doi", "id": "10.1590/abc"},
                {"type": "issn", "id": "1234-5678"},
            ],
        }
    }


def test_map_doaj_to_zotero_happy_path() -> None:
    payload = map_doaj_to_zotero(_full_record())

    assert payload is not None
    assert payload["itemType"] == "journalArticle"
    assert payload["title"] == "Inflación en LATAM"
    assert payload["DOI"] == "10.1590/abc"
    assert payload["date"] == "2024-06"
    assert payload["publicationTitle"] == "Revista DOAJ"
    assert payload["abstractNote"] == "An abstract."
    assert payload["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"},
        {"creatorType": "author", "firstName": "Smith", "lastName": "Jr."},
    ]


def test_map_doaj_to_zotero_drops_when_bibjson_missing() -> None:
    assert map_doaj_to_zotero({"id": "1"}) is None


def test_map_doaj_to_zotero_drops_when_title_blank() -> None:
    record = _full_record()
    record["bibjson"]["title"] = "  "
    assert map_doaj_to_zotero(record) is None


def test_map_doaj_to_zotero_drops_when_no_authors() -> None:
    record = _full_record()
    record["bibjson"]["author"] = []
    assert map_doaj_to_zotero(record) is None


def test_map_doaj_to_zotero_skips_non_dict_authors() -> None:
    record = _full_record()
    record["bibjson"]["author"] = ["not a dict", {"name": "Doe, Jane"}]
    payload = map_doaj_to_zotero(record)
    assert payload is not None
    assert payload["creators"] == [
        {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"}
    ]


def test_map_doaj_to_zotero_omits_doi_field_when_no_identifier() -> None:
    record = _full_record()
    record["bibjson"]["identifier"] = [{"type": "issn", "id": "1234"}]
    payload = map_doaj_to_zotero(record)
    assert payload is not None
    assert "DOI" not in payload


def test_map_doaj_to_zotero_handles_missing_optional_fields() -> None:
    record = {
        "bibjson": {
            "title": "Minimal",
            "author": [{"name": "Solo Author"}],
        }
    }
    payload = map_doaj_to_zotero(record)
    assert payload is not None
    assert payload["title"] == "Minimal"
    assert payload["abstractNote"] == ""
    assert payload["date"] == ""
    assert "DOI" not in payload
    assert "publicationTitle" not in payload or payload["publicationTitle"] == ""


# ── _doi_from_doaj_record ─────────────────────────────────────────────────


def test_doi_from_doaj_record_picks_doi_type_only() -> None:
    record = {
        "bibjson": {
            "identifier": [
                {"type": "issn", "id": "1234-5678"},
                {"type": "doi", "id": "10.x/abc"},
            ]
        }
    }
    assert _doi_from_doaj_record(record) == "10.x/abc"


def test_doi_from_doaj_record_returns_none_when_no_doi_type() -> None:
    record = {"bibjson": {"identifier": [{"type": "issn", "id": "1234"}]}}
    assert _doi_from_doaj_record(record) is None


def test_doi_from_doaj_record_returns_none_when_bibjson_missing() -> None:
    assert _doi_from_doaj_record({}) is None


def test_doi_from_doaj_record_returns_none_when_identifier_missing() -> None:
    assert _doi_from_doaj_record({"bibjson": {}}) is None


def test_doi_from_doaj_record_skips_non_dict_entries() -> None:
    record = {
        "bibjson": {
            "identifier": ["str-not-dict", {"type": "doi", "id": "10.1/x"}],
        }
    }
    assert _doi_from_doaj_record(record) == "10.1/x"


# ── _split_doaj_name ──────────────────────────────────────────────────────


def test_split_doaj_name_comma_form() -> None:
    assert _split_doaj_name("Doe, Jane A.") == ("Jane A.", "Doe")


def test_split_doaj_name_western_order() -> None:
    assert _split_doaj_name("Jane A. Doe") == ("Jane A.", "Doe")


def test_split_doaj_name_single_token() -> None:
    assert _split_doaj_name("Plato") == ("", "Plato")


def test_split_doaj_name_empty_string_returns_empty_pair() -> None:
    # The caller in ``map_doaj_to_zotero`` filters blanks before
    # invoking, but the helper still must not raise on an empty string —
    # its contract should match
    # ``zotai.api.zotero_queries.split_name``.
    assert _split_doaj_name("") == ("", "")


def test_split_doaj_name_whitespace_only_returns_empty_pair() -> None:
    assert _split_doaj_name("   ") == ("", "")


def test_split_doaj_name_strips_whitespace() -> None:
    assert _split_doaj_name("  Doe, Jane  ") == ("Jane", "Doe")


# ── _date_from_doaj ───────────────────────────────────────────────────────


def test_date_from_doaj_year_string_only() -> None:
    assert _date_from_doaj("2024", None) == "2024"


def test_date_from_doaj_year_int_zero_pads() -> None:
    assert _date_from_doaj(987, None) == "0987"


def test_date_from_doaj_year_with_month_string() -> None:
    assert _date_from_doaj("2024", "6") == "2024-06"


def test_date_from_doaj_year_with_month_int() -> None:
    assert _date_from_doaj(2024, 12) == "2024-12"


def test_date_from_doaj_invalid_month_drops_month() -> None:
    assert _date_from_doaj("2024", "13") == "2024"
    assert _date_from_doaj("2024", 0) == "2024"
    assert _date_from_doaj("2024", "abc") == "2024"


def test_date_from_doaj_blank_year_returns_empty_string() -> None:
    assert _date_from_doaj("", "6") == ""
    assert _date_from_doaj(None, "6") == ""
    assert _date_from_doaj("not-a-year", "6") == ""
