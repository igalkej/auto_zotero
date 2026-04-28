"""Shared Zotero lookup + payload helpers reused across S1 stages.

Stage 03 (import via Route A) and every Stage 04 enrichment substage
that creates a parent item need the same three primitives:

- :func:`find_existing_doi` — locate a Zotero item by DOI before
  creating a duplicate (ADR 014 dedup).
- :func:`existing_has_pdf_attachment` — decide whether to attach our
  PDF to an existing parent or skip the attach (ADR 014).
- :func:`split_name` — split a display name into ``(first, last)`` for
  Zotero's ``creators`` payload, used by every metadata mapper that
  consumes a free-form display name (OpenAlex, Semantic Scholar,
  SciELO, DOAJ, the LLM extractor).

Kept here rather than in ``stage_03_import`` so that Stage 04 substages
do not have to reach into Stage 03's private namespace.
"""

from __future__ import annotations

from zotai.api.zotero import ZoteroClient


def find_existing_doi(zotero_client: ZoteroClient, doi: str) -> str | None:
    """Return the item_key of an existing Zotero item with this DOI, or None."""
    results = zotero_client.items(q=doi, qmode="everything", limit=25)
    lowered = doi.lower()
    for result in results:
        data = result.get("data") or {}
        existing_doi = (data.get("DOI") or "").lower()
        if existing_doi == lowered:
            key = result.get("key") or data.get("key")
            if isinstance(key, str) and key:
                return key
    return None


def existing_has_pdf_attachment(
    zotero_client: ZoteroClient, item_key: str
) -> bool:
    """Return True iff the item already has at least one PDF attachment.

    Used on the dedup path (ADR 014): when an importer finds that the
    DOI is already in the user's Zotero library, we skip attaching our
    PDF if an existing PDF child is there — the user already had the
    paper and its own copy. If the parent has *no* PDF (metadata-only
    import from a prior session), we still attach.
    """
    for child in zotero_client.children(item_key):
        data = child.get("data") or {}
        if data.get("itemType") != "attachment":
            continue
        content_type = (data.get("contentType") or "").lower()
        if content_type.startswith("application/pdf"):
            return True
    return False


def split_name(display_name: str) -> tuple[str, str]:
    """Split ``"Jane A. Doe"`` into ``("Jane A.", "Doe")``.

    Heuristic: the last whitespace-separated token is the surname. Works
    well for Western name order; misclassifies some Spanish compound
    surnames (``"Gabriel García Márquez"`` → ``("Gabriel García", "Márquez")``)
    but the upstream sources only store display names, so we cannot do
    better without more data. Stage 04 LLM extraction has a chance to
    correct this when a document falls through.
    """
    cleaned = display_name.strip()
    if not cleaned:
        return "", ""
    parts = cleaned.split()
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]
