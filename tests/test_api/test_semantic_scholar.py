"""Tests for :mod:`zotai.api.semantic_scholar`.

Coverage:

- ``search_paper`` happy path with the default field set Stage 04c
  consumes (``title,authors,year,venue,abstract,externalIds``).
- Custom ``fields`` overrides; empty ``fields`` falls back to the
  Semantic Scholar API default (``paperId + title``).
- API key wiring: ``x-api-key`` header is present iff a key was passed
  to the constructor.
- Defensive parse: missing ``data`` key returns an empty list rather
  than blowing up.

HTTP is mocked with respx; no live network.
"""

from __future__ import annotations

import httpx
import respx

from zotai.api.semantic_scholar import (
    SEMANTIC_SCHOLAR_BASE,
    SemanticScholarClient,
)


async def test_search_paper_happy_path_with_default_fields() -> None:
    sample = {
        "data": [
            {
                "paperId": "abc",
                "title": "Inflación en LATAM",
                "authors": [{"name": "Doe, J."}],
                "year": 2024,
            }
        ]
    }
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json=sample)
        )
        client = SemanticScholarClient()
        results = await client.search_paper("inflación LATAM", limit=5)

    assert results == sample["data"]
    request = route.calls.last.request
    assert request.url.params["query"] == "inflación LATAM"
    assert request.url.params["limit"] == "5"
    # Default fields include the columns Stage 04c's mapper needs.
    fields = request.url.params["fields"]
    for required in ("title", "authors", "year", "venue", "abstract", "externalIds"):
        assert required in fields


async def test_search_paper_custom_fields_passthrough() -> None:
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        client = SemanticScholarClient()
        await client.search_paper("x", fields="title,year")

    assert route.calls.last.request.url.params["fields"] == "title,year"


async def test_search_paper_empty_fields_omits_param() -> None:
    # Passing ``fields=""`` should fall back to the Semantic Scholar
    # API default (paperId + title) — i.e. the param is not sent.
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        client = SemanticScholarClient()
        await client.search_paper("x", fields="")

    assert "fields" not in route.calls.last.request.url.params


async def test_search_paper_returns_empty_list_when_data_missing() -> None:
    async with respx.mock() as router:
        router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"meta": {}})
        )
        client = SemanticScholarClient()
        results = await client.search_paper("nothing")

    assert results == []


async def test_api_key_sets_x_api_key_header() -> None:
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        client = SemanticScholarClient(api_key="key-abc")
        await client.search_paper("x")

    headers = route.calls.last.request.headers
    assert headers["x-api-key"] == "key-abc"


async def test_no_api_key_omits_x_api_key_header() -> None:
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        client = SemanticScholarClient()
        await client.search_paper("x")

    assert "x-api-key" not in route.calls.last.request.headers


async def test_empty_string_api_key_treated_as_absent() -> None:
    # Constructor coalesces falsy api_key to None. A `.env` value left
    # blank should not produce a literal ``x-api-key: `` header.
    async with respx.mock() as router:
        route = router.get(f"{SEMANTIC_SCHOLAR_BASE}/paper/search").mock(
            return_value=httpx.Response(200, json={"data": []})
        )
        client = SemanticScholarClient(api_key="")
        await client.search_paper("x")

    assert "x-api-key" not in route.calls.last.request.headers
