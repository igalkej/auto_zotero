"""Tests for :mod:`zotai.api.openalex`.

The adapter is a thin wrapper around two endpoints: ``/works`` (free-text
search) and ``/works/doi:<doi>`` (DOI lookup). We cover happy paths,
empty responses, the documented 404 → ``None`` contract on the DOI
lookup, the User-Agent ``mailto`` polite-pool wiring, and the request
parameters Stage 03 / 04b rely on (``search``, ``per-page``).

HTTP is mocked with respx; no live network.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from zotai.api.openalex import OPENALEX_BASE, OpenAlexClient


async def test_search_works_happy_path() -> None:
    sample = {"results": [{"id": "W1", "title": "A paper"}]}
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(f"{OPENALEX_BASE}/works").mock(
            return_value=httpx.Response(200, json=sample)
        )
        client = OpenAlexClient(user_email="user@example.org")
        results = await client.search_works("A paper", per_page=5)

    assert results == [{"id": "W1", "title": "A paper"}]
    request = route.calls.last.request
    assert request.url.params["search"] == "A paper"
    assert request.url.params["per-page"] == "5"
    # mailto goes into the User-Agent (polite pool).
    assert "mailto:user@example.org" in request.headers["user-agent"]


async def test_search_works_returns_empty_list_when_no_results() -> None:
    async with respx.mock() as router:
        router.get(f"{OPENALEX_BASE}/works").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        client = OpenAlexClient()
        results = await client.search_works("nothing matches")

    assert results == []


async def test_search_works_tolerates_missing_results_key() -> None:
    # OpenAlex always returns ``results`` but the adapter falls back to
    # an empty list defensively. Verify that contract.
    async with respx.mock() as router:
        router.get(f"{OPENALEX_BASE}/works").mock(
            return_value=httpx.Response(200, json={"meta": {}})
        )
        client = OpenAlexClient()
        results = await client.search_works("anything")

    assert results == []


async def test_work_by_doi_happy_path() -> None:
    sample = {"id": "W42", "doi": "https://doi.org/10.1000/x", "title": "Paper"}
    async with respx.mock() as router:
        route = router.get(
            f"{OPENALEX_BASE}/works/doi:10.1000/x"
        ).mock(return_value=httpx.Response(200, json=sample))
        client = OpenAlexClient()
        result = await client.work_by_doi("10.1000/x")

    assert result == sample
    assert route.calls.call_count == 1


async def test_work_by_doi_returns_none_on_404() -> None:
    async with respx.mock() as router:
        router.get(f"{OPENALEX_BASE}/works/doi:10.0/notfound").mock(
            return_value=httpx.Response(404, json={"error": "Not Found"})
        )
        client = OpenAlexClient()
        result = await client.work_by_doi("10.0/notfound")

    assert result is None


async def test_work_by_doi_raises_on_5xx() -> None:
    async with respx.mock() as router:
        router.get(f"{OPENALEX_BASE}/works/doi:10.0/boom").mock(
            return_value=httpx.Response(500)
        )
        client = OpenAlexClient()
        with pytest.raises(httpx.HTTPStatusError):
            await client.work_by_doi("10.0/boom")


async def test_user_agent_omits_mailto_when_no_email() -> None:
    async with respx.mock() as router:
        route = router.get(f"{OPENALEX_BASE}/works").mock(
            return_value=httpx.Response(200, json={"results": []})
        )
        client = OpenAlexClient()
        await client.search_works("x")

    ua = route.calls.last.request.headers["user-agent"]
    assert "mailto:" not in ua
