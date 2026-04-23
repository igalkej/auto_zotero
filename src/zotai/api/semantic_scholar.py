"""Semantic Scholar Graph API adapter.

Rate limit is 100 req / 5 min without a key, 1 req / s with one (plan_01 §3,
Stage 04c). The optional key is passed via `.env`
(`SEMANTIC_SCHOLAR_API_KEY`).
"""

from __future__ import annotations

from typing import Any, Final, cast

from zotai.utils.http import make_async_client, make_user_agent, with_retry
from zotai.utils.logging import get_logger

log = get_logger(__name__)

SEMANTIC_SCHOLAR_BASE = "https://api.semanticscholar.org/graph/v1"

# Fields required by Stage 04c's mapper (authors, year, venue, abstract,
# externalIds.DOI). Semantic Scholar returns only ``paperId`` + ``title``
# by default, so callers that want to build a Zotero payload must opt in.
_DEFAULT_SEARCH_FIELDS: Final[str] = (
    "title,authors,year,venue,abstract,externalIds"
)


class SemanticScholarClient:
    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or None
        self._user_agent = make_user_agent()

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent}
        if self._api_key:
            headers["x-api-key"] = self._api_key
        return headers

    async def search_paper(
        self,
        query: str,
        *,
        limit: int = 5,
        fields: str = _DEFAULT_SEARCH_FIELDS,
    ) -> list[dict[str, Any]]:
        """Search `/paper/search` by query. Returns `data` array, possibly empty.

        By default requests the fields Stage 04c needs. Pass a different
        comma-separated ``fields`` string to narrow the response (or pass
        the empty string to fall back to the API default of
        ``paperId + title``).
        """

        async def _do() -> list[dict[str, Any]]:
            params: dict[str, str | int] = {"query": query, "limit": limit}
            if fields:
                params["fields"] = fields
            async with make_async_client(user_agent=self._user_agent) as client:
                resp = await client.get(
                    f"{SEMANTIC_SCHOLAR_BASE}/paper/search",
                    params=params,
                    headers=self._headers(),
                )
                resp.raise_for_status()
                payload = cast(dict[str, Any], resp.json())
                data = payload.get("data", [])
                return cast(list[dict[str, Any]], data)

        return await with_retry(_do)


__all__ = ["SEMANTIC_SCHOLAR_BASE", "SemanticScholarClient"]
