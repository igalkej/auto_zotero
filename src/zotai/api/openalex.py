"""OpenAlex adapter.

No API key required; including a `mailto` in the User-Agent unlocks the
"polite pool" (100 req/s vs 10 req/s). We pass that in from
`BehaviorSettings.user_email`.
"""

from __future__ import annotations

from typing import Any, cast

from zotai.utils.http import make_async_client, make_user_agent, with_retry
from zotai.utils.logging import get_logger

log = get_logger(__name__)

OPENALEX_BASE = "https://api.openalex.org"


class OpenAlexClient:
    def __init__(self, user_email: str | None = None) -> None:
        self._user_agent = make_user_agent(mailto=user_email)

    async def search_works(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        """Search `/works` by title. Returns the `results` array, possibly empty."""

        async def _do() -> list[dict[str, Any]]:
            async with make_async_client(user_agent=self._user_agent) as client:
                resp = await client.get(
                    f"{OPENALEX_BASE}/works",
                    params={"search": title, "per-page": per_page},
                )
                resp.raise_for_status()
                payload = cast(dict[str, Any], resp.json())
                results = payload.get("results", [])
                return cast(list[dict[str, Any]], results)

        return await with_retry(_do)

    async def work_by_doi(self, doi: str) -> dict[str, Any] | None:
        """Return the full OpenAlex record for a DOI, or None if not found."""

        async def _do() -> dict[str, Any] | None:
            async with make_async_client(user_agent=self._user_agent) as client:
                resp = await client.get(f"{OPENALEX_BASE}/works/doi:{doi}")
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                return cast(dict[str, Any], resp.json())

        return await with_retry(_do)


__all__ = ["OPENALEX_BASE", "OpenAlexClient"]
