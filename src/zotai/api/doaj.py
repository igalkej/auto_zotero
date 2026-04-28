"""DOAJ adapter — substage 04bd in the Stage 04 cascade (ADR 018).

Hits ``doaj.org/api/v3/search/articles/<query>`` with an Elasticsearch
query string. DOAJ's read-only article search is public (no API key)
with a 2 req/s rate limit + bursts up to 5 — comfortable for the
cascade's per-item rhythm. Per ADR 018, DOAJ rejects the Lucene fuzzy
operator ``~``; we use a quoted ``bibjson.title:"<title>"`` query which
DOAJ accepts and which still feeds ``rapidfuzz.fuzz.token_set_ratio``
on the cascade side.
"""

from __future__ import annotations

from typing import Any, Final, cast
from urllib.parse import quote

from zotai.utils.http import make_async_client, make_user_agent, with_retry
from zotai.utils.logging import get_logger

log = get_logger(__name__)

DOAJ_API_BASE: Final[str] = "https://doaj.org/api/v3"


class DOAJClient:
    """HTTP client for the DOAJ substage (04bd)."""

    def __init__(self, user_email: str | None = None) -> None:
        self._user_agent = make_user_agent(mailto=user_email)

    async def search_articles(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        """Search DOAJ for articles by free-text title.

        Returns ``payload["results"]`` (DOAJ's per-record array).
        Defensive about response shape: returns ``[]`` (and logs
        ``doaj.unexpected_shape``) when the JSON does not match
        DOAJ's documented structure.
        """

        async def _do() -> list[dict[str, Any]]:
            query = f'bibjson.title:"{title}"'
            url = f"{DOAJ_API_BASE}/search/articles/{quote(query, safe='')}"
            params: dict[str, str | int] = {"pageSize": per_page, "page": 1}
            async with make_async_client(user_agent=self._user_agent) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = cast(dict[str, Any], resp.json())
            results = payload.get("results")
            if not isinstance(results, list):
                log.warning(
                    "doaj.unexpected_shape",
                    reason="missing_results",
                    top_keys=list(payload.keys())[:8],
                )
                return []
            return cast(list[dict[str, Any]], results)

        return await with_retry(_do)


def map_doaj_to_zotero(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map a DOAJ article record to a Zotero payload.

    Returns ``None`` when the quality gate fails — missing
    ``bibjson.title`` or no parseable authors. Item type is hardcoded
    to ``journalArticle``; DOAJ indexes only journal articles by
    construction.
    """
    bib = record.get("bibjson")
    if not isinstance(bib, dict):
        return None

    raw_title = bib.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        return None
    title = raw_title.strip()

    creators: list[dict[str, str]] = []
    raw_authors = bib.get("author")
    if isinstance(raw_authors, list):
        for entry in raw_authors:
            if not isinstance(entry, dict):
                continue
            name = (entry.get("name") or "").strip()
            if not name:
                continue
            first, last = _split_doaj_name(name)
            creators.append(
                {"creatorType": "author", "firstName": first, "lastName": last}
            )
    if not creators:
        return None

    date_str = _date_from_doaj(bib.get("year"), bib.get("month"))

    venue = ""
    journal = bib.get("journal")
    if isinstance(journal, dict):
        venue_raw = journal.get("title")
        if isinstance(venue_raw, str):
            venue = venue_raw.strip()

    abstract_raw = bib.get("abstract")
    abstract = abstract_raw.strip() if isinstance(abstract_raw, str) else ""

    doi = _doi_from_doaj_record(record) or ""

    payload: dict[str, Any] = {
        "itemType": "journalArticle",
        "title": title,
        "creators": creators,
        "date": date_str,
        "abstractNote": abstract,
    }
    if doi:
        payload["DOI"] = doi
    if venue:
        payload["publicationTitle"] = venue
    return payload


def _doi_from_doaj_record(record: dict[str, Any]) -> str | None:
    """Return the first DOI from DOAJ's ``bibjson.identifier`` list, or None."""
    bib = record.get("bibjson")
    if not isinstance(bib, dict):
        return None
    identifiers = bib.get("identifier")
    if not isinstance(identifiers, list):
        return None
    for entry in identifiers:
        if not isinstance(entry, dict):
            continue
        if (entry.get("type") or "").lower() == "doi":
            raw = entry.get("id")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    return None


def _split_doaj_name(name: str) -> tuple[str, str]:
    """Split a DOAJ author ``name`` into ``(firstName, lastName)``.

    DOAJ stores authors as a single ``name`` string, typically either
    "Last, First" or "First Last". The "Last, First" branch is unique
    to DOAJ; the Western-order fallback mirrors
    :func:`zotai.api.zotero_queries.split_name` so all substages keep
    author shapes consistent.
    """
    name = name.strip()
    if not name:
        return "", ""
    if "," in name:
        last, _, first = name.partition(",")
        return first.strip(), last.strip()
    parts = name.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]


def _date_from_doaj(year_raw: Any, month_raw: Any) -> str:
    """Build a Zotero ``date`` from DOAJ's optional year + month fields."""
    year_str = ""
    if isinstance(year_raw, str) and year_raw.strip().isdigit():
        year_str = year_raw.strip()
    elif isinstance(year_raw, int):
        year_str = f"{year_raw:04d}"
    if not year_str:
        return ""

    month: int | None = None
    if isinstance(month_raw, str) and month_raw.strip().isdigit():
        m = int(month_raw.strip())
        if 1 <= m <= 12:
            month = m
    elif isinstance(month_raw, int) and 1 <= month_raw <= 12:
        month = month_raw

    if month is not None:
        return f"{year_str}-{month:02d}"
    return year_str


__all__ = [
    "DOAJ_API_BASE",
    "DOAJClient",
    "map_doaj_to_zotero",
]
