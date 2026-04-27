"""SciELO substage adapter — implemented via Crossref Member 530 (ADR 019).

Per ADR 019, ``search.scielo.org``'s Solr endpoint is closed to anonymous
clients (403 Forbidden) and ArticleMeta only supports lookup by SciELO
PID. The substage's contract (fuzzy-title-search → DOI-grade metadata)
is satisfied via Crossref's REST API filtered to ``member:530``
(Crossref's identifier for SciELO). The filter narrows the search space
to SciELO-only records, which improves top-5 ranking for LATAM-Spanish
queries even though every paper returned is also in OpenAlex's
underlying corpus.

The class :class:`SciELoClient` and the file ``src/zotai/api/scielo.py``
are preserved as the substage's HTTP abstraction — the substage's
identity (``04bs``, ``enriched_04bs``, ``S1_ENABLE_SCIELO``) is
spec-compliance with ADR 018; ADR 019 documents why the implementation
hits Crossref.
"""

from __future__ import annotations

import html
import re
from typing import Any, Final, cast

from zotai.utils.http import make_async_client, make_user_agent, with_retry
from zotai.utils.logging import get_logger

log = get_logger(__name__)

CROSSREF_BASE: Final[str] = "https://api.crossref.org"
SCIELO_CROSSREF_MEMBER: Final[str] = "530"
_DEFAULT_SELECT: Final[str] = (
    "DOI,title,author,published,container-title,abstract,type"
)
_JATS_TAG_RE: Final[re.Pattern[str]] = re.compile(r"<[^>]+>")


class SciELoClient:
    """HTTP client for the SciELO substage (04bs).

    Implementation hits Crossref's REST API filtered to ``member:530``.
    Per ADR 019, the substage uses Crossref-as-mirror rather than
    ``search.scielo.org`` because the latter is gated. The filter
    narrows the candidate list so the cascade's fuzzy-title match
    surfaces SciELO papers in top-5 even for LATAM-Spanish queries.
    """

    def __init__(self, user_email: str | None = None) -> None:
        self._user_agent = make_user_agent(mailto=user_email)

    async def search_articles(
        self, title: str, *, per_page: int = 5
    ) -> list[dict[str, Any]]:
        """Search Crossref (member:530) for SciELO papers by free-text title.

        Returns ``payload["message"]["items"]``, possibly empty.
        Defensive about response shape: returns ``[]`` (and logs
        ``scielo.unexpected_shape``) when the JSON does not match
        Crossref's documented structure.
        """

        async def _do() -> list[dict[str, Any]]:
            params: dict[str, str | int] = {
                "query.title": title,
                "filter": f"member:{SCIELO_CROSSREF_MEMBER}",
                "rows": per_page,
                "select": _DEFAULT_SELECT,
            }
            async with make_async_client(user_agent=self._user_agent) as client:
                resp = await client.get(f"{CROSSREF_BASE}/works", params=params)
                resp.raise_for_status()
                payload = cast(dict[str, Any], resp.json())
            message = payload.get("message")
            if not isinstance(message, dict):
                log.warning(
                    "scielo.unexpected_shape",
                    reason="missing_message",
                    top_keys=list(payload.keys())[:8],
                )
                return []
            items = message.get("items")
            if not isinstance(items, list):
                log.warning(
                    "scielo.unexpected_shape",
                    reason="missing_items",
                    message_keys=list(message.keys())[:8],
                )
                return []
            return cast(list[dict[str, Any]], items)

        return await with_retry(_do)


def map_scielo_to_zotero(record: dict[str, Any]) -> dict[str, Any] | None:
    """Map a Crossref ``works`` record (member:530) to a Zotero payload.

    Returns ``None`` when the quality gate fails — missing title or no
    parseable authors. Item type is hardcoded to ``journalArticle``;
    Crossref's ``type`` for SciELO records is uniformly
    ``'journal-article'``. Schema mirrors
    :func:`zotai.s1.stage_04_enrich.map_semantic_scholar_to_zotero` so
    every cascade substage feeds the same Zotero ``create_items``
    endpoint.
    """
    title_list = record.get("title")
    if not isinstance(title_list, list) or not title_list:
        return None
    raw_title = title_list[0]
    if not isinstance(raw_title, str) or not raw_title.strip():
        return None
    title = html.unescape(raw_title.strip())

    creators: list[dict[str, str]] = []
    raw_authors = record.get("author")
    if isinstance(raw_authors, list):
        for entry in raw_authors:
            if not isinstance(entry, dict):
                continue
            given = (entry.get("given") or "").strip()
            family = (entry.get("family") or "").strip()
            if given or family:
                creators.append(
                    {
                        "creatorType": "author",
                        "firstName": given,
                        "lastName": family,
                    }
                )
                continue
            # Crossref also exposes ``name`` on corporate authors.
            name = (entry.get("name") or "").strip()
            if name:
                creators.append(
                    {"creatorType": "author", "firstName": "", "lastName": name}
                )
    if not creators:
        return None

    date_str = _date_from_crossref_published(record.get("published"))

    venue = ""
    container_list = record.get("container-title")
    if isinstance(container_list, list) and container_list:
        first = container_list[0]
        if isinstance(first, str) and first.strip():
            venue = html.unescape(first.strip())

    abstract = _abstract_from_crossref(record.get("abstract"))

    doi_raw = record.get("DOI")
    doi = doi_raw.strip() if isinstance(doi_raw, str) else ""

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


def _doi_from_scielo_record(record: dict[str, Any]) -> str | None:
    """Return the normalised DOI from a Crossref record, or None."""
    raw = record.get("DOI")
    if isinstance(raw, str):
        cleaned = raw.strip()
        if cleaned:
            return cleaned
    return None


def _date_from_crossref_published(published: Any) -> str:
    """Build a Zotero ``date`` string from Crossref's ``published.date-parts``.

    Crossref returns ``{"date-parts": [[year, month?, day?]]}``. Returns
    "YYYY", "YYYY-MM", or "YYYY-MM-DD" when the inner triple is well
    formed; "" otherwise.
    """
    if not isinstance(published, dict):
        return ""
    parts = published.get("date-parts")
    if not isinstance(parts, list) or not parts:
        return ""
    inner = parts[0]
    if not isinstance(inner, list) or not inner:
        return ""
    year = inner[0] if isinstance(inner[0], int) else None
    if year is None:
        return ""
    out = f"{year:04d}"
    if len(inner) >= 2 and isinstance(inner[1], int):
        out += f"-{inner[1]:02d}"
        if len(inner) >= 3 and isinstance(inner[2], int):
            out += f"-{inner[2]:02d}"
    return out


def _abstract_from_crossref(raw: Any) -> str:
    """Strip JATS XML tags from a Crossref abstract; return "" if missing.

    Crossref returns abstracts wrapped in JATS markup (``<jats:p>``,
    ``<jats:italic>``, ``<jats:title>``, etc.). We strip every angle-bracket
    tag and collapse the resulting whitespace.
    """
    if not isinstance(raw, str):
        return ""
    stripped = _JATS_TAG_RE.sub(" ", raw)
    return " ".join(stripped.split())


__all__ = [
    "CROSSREF_BASE",
    "SCIELO_CROSSREF_MEMBER",
    "SciELoClient",
    "map_scielo_to_zotero",
]
