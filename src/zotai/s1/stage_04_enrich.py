"""Stage 04 — enrichment cascade (plan_01 §3 Etapa 04).

This stage takes the items that Stage 03 parked as Route C orphan
attachments (no bibliographic metadata in Zotero beyond the PDF itself)
and walks them through a falling-through cascade that tries
progressively more expensive sources to recover the metadata:

- **04a** — aggressive identifier extraction on pages 1-3 of the PDF.
  If a *new* DOI is found (one not already in ``Item.detected_doi``),
  retry Route A from Stage 03 via OpenAlex. Free ($0).
- **04b** — fuzzy title match against OpenAlex
  (``rapidfuzz.fuzz.token_set_ratio >= 85``). Free ($0).
- **04c** — fuzzy title match against Semantic Scholar. Free ($0).
- **04d** — LLM (``gpt-4o-mini``) JSON extraction from the first two
  pages, validated against :class:`LLMExtractedMetadata`. Bounded by
  ``MAX_COST_USD_STAGE_04`` (ADR 005). Once the budget trips, remaining
  items route directly to 04e without further LLM calls.
- **04e** — Quarantine: tag ``needs-manual-review``, add to the
  ``Quarantine`` collection, append to ``quarantine_report.csv`` (ADR
  008).

``run_enrich(substage="all")`` drives the per-item cascade end-to-end;
individual substages are callable directly for debugging or partial
runs. On a successful hit (04a-04d) the item advances to
``stage_completed=4, import_route='A'``; on quarantine (04e) the item
advances to ``stage_completed=4, in_quarantine=True``.

---

Cross-cutting rules (match Stage 03):

- **Idempotent.** Items whose ``import_route`` is already ``'A'`` or
  that have ``stage_completed >= 4`` are skipped by ``_select_eligible``.
- **Dedup on DOI.** If a new DOI resolves to a Zotero item the user
  already has, reuse its key rather than creating a parallel item.
  Same policy as Stage 03 (ADR 014): attach iff the existing parent
  has no PDF yet.
- **Dry-run.** No Zotero writes, no DB writes, ``_dryrun``-suffixed
  CSV. Network probes still run (OpenAlex/SemanticScholar lookups are
  cheap reads).
- **Fail-loud.** Per-item failures get logged and recorded in the CSV
  with ``status='failed'`` and an error string, but do not abort the
  stage. Aborting is handled by the standard handler.
"""

from __future__ import annotations

import asyncio
import csv
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError
from rapidfuzz import fuzz
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.api.doaj import DOAJClient, _doi_from_doaj_record, map_doaj_to_zotero
from zotai.api.openai_client import BudgetExceededError, OpenAIClient
from zotai.api.openalex import OpenAlexClient
from zotai.api.scielo import (
    SciELoClient,
    _doi_from_scielo_record,
    map_scielo_to_zotero,
)
from zotai.api.semantic_scholar import SemanticScholarClient
from zotai.api.zotero import ZoteroClient
from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_03_import import (
    _existing_has_pdf_attachment,
    _find_existing_doi,
    _split_name,
    map_openalex_to_zotero,
)
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir
from zotai.utils.logging import bind, get_logger
from zotai.utils.pdf import extract_probable_title, extract_text_pages

# TODO(refactor): _find_existing_doi, _existing_has_pdf_attachment, and
# _split_name are now reused across Stage 03 and every 04 substage via
# _create_parent_and_reparent. A follow-up chore PR should promote them
# to `zotai.api.zotero_queries` (or similar) and drop the noqa imports
# below. Deferred — trivial, not worth mixing with Stage 04 feature
# work; pick up when Stage 05 lands (it will want the same helpers).

log = get_logger(__name__)

_STAGE: Final[int] = 4
_PREREQ_STAGE: Final[int] = 3
_PAGES_FOR_ID_EXTRACTION: Final[int] = 3
# plan_01 §3 Stage 04b/c: ``rapidfuzz.fuzz.token_set_ratio >= 85`` gates
# every fuzzy title match. Common constant avoids drift between 04b and 04c.
_FUZZ_THRESHOLD: Final[int] = 85
# Stage 04d sends the first two pages to the LLM — enough for the title
# + authors + abstract to be visible on a typical first-page layout.
_PAGES_FOR_LLM_EXTRACTION: Final[int] = 2
# 04d JSON mode — retry once if the first response fails validation
# (malformed JSON, missing fields, off-schema item_type). After the
# retry we fall through to 04e (quarantine).
_LLM_MAX_RETRIES: Final[int] = 1
# Zotero item types the LLM is allowed to emit (plan_01 §3 Stage 04d).
# Keeps 04d's output on the small set that `map_llm_extraction_to_zotero`
# maps directly without a lookup table.
_LLM_ALLOWED_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "journalArticle",
        "book",
        "bookSection",
        "thesis",
        "report",
        "preprint",
        "conferencePaper",
    }
)
# Stage 04e — Quarantine.
_QUARANTINE_COLLECTION_NAME: Final[str] = "Quarantine"
_QUARANTINE_TAG: Final[str] = "needs-manual-review"
_QUARANTINE_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "sha256",
    "source_path",
    "text_snippet",
    "reason",
)
_QUARANTINE_SNIPPET_CHARS: Final[int] = 200

_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "sha256",
    "source_path",
    "zotero_item_key_before",
    "zotero_item_key_after",
    "substage_resolved",
    "new_doi",
    "status",
    "error",
)

EnrichSubstage = Literal[
    "04a", "04b", "04bs", "04bd", "04c", "04d", "04e", "all"
]
EnrichStatus = Literal[
    "enriched_04a",
    "enriched_04b",
    "enriched_04bs",
    "enriched_04bd",
    "enriched_04c",
    "enriched_04d",
    "quarantined_04e",
    "no_progress",
    "skipped_already_enriched",
    "skipped_generic_title",
    "budget_exceeded",
    "failed",
    "dry_run",
]
_ENRICHED_STATUSES: Final[frozenset[str]] = frozenset(
    {
        "enriched_04a",
        "enriched_04b",
        "enriched_04bs",
        "enriched_04bd",
        "enriched_04c",
        "enriched_04d",
    }
)
# 04bs / 04bd resilience policy (ADR 018 + ADR 019): transient or
# upstream-side HTTP errors fall through to the next substage as
# ``no_progress`` rather than failing the item outright.
_LATAM_TRANSIENT_STATUSES: Final[frozenset[int]] = frozenset({403, 429, 502, 503})


# ─── Identifier regexes (shared patterns reused from Stage 01 classifier) ──

_DOI_RE: Final[re.Pattern[str]] = re.compile(
    r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE
)
_ARXIV_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:arXiv:|arxiv\.org/abs/)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)
_ISBN_RE: Final[re.Pattern[str]] = re.compile(
    r"\bISBN(?:-1[03])?[:\s]*([\d\-X ]{10,17})",
    re.IGNORECASE,
)
_HANDLE_RE: Final[re.Pattern[str]] = re.compile(
    r"\bhdl\.handle\.net/([\w./-]+)",
    re.IGNORECASE,
)
_REPEC_RE: Final[re.Pattern[str]] = re.compile(
    r"\bRePEc:[A-Za-z]{3,}:[A-Za-z0-9._-]+(?::[A-Za-z0-9._-]+){1,4}\b",
)


# ─── Result types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnrichRow:
    """One row in ``enrich_report_<ts>.csv``."""

    sha256: str
    source_path: str
    zotero_item_key_before: str | None
    zotero_item_key_after: str | None
    substage_resolved: EnrichSubstage | None
    new_doi: str | None
    status: EnrichStatus
    error: str | None


@dataclass(frozen=True)
class EnrichResult:
    """Aggregate outcome of one ``run_enrich`` call."""

    run_id: int | None
    rows: list[EnrichRow]
    csv_path: Path
    items_processed: int
    items_failed: int
    items_enriched_04a: int
    items_enriched_04b: int
    items_enriched_04bs: int
    items_enriched_04bd: int
    items_enriched_04c: int
    items_enriched_04d: int
    items_quarantined: int
    items_no_progress: int
    items_skipped: int
    items_skipped_generic_title: int
    # Path of the quarantine_report.csv (plan_01 §3.04e). ``None`` when
    # no item was quarantined in this run.
    quarantine_csv_path: Path | None = None


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"enrich_report_{timestamp}{suffix}.csv"


def _write_csv(csv_path: Path, rows: Iterable[EnrichRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sha256": row.sha256,
                    "source_path": row.source_path,
                    "zotero_item_key_before": row.zotero_item_key_before or "",
                    "zotero_item_key_after": row.zotero_item_key_after or "",
                    "substage_resolved": row.substage_resolved or "",
                    "new_doi": row.new_doi or "",
                    "status": row.status,
                    "error": row.error or "",
                }
            )


# ─── Identifier extraction (04a — regex on pages 1-3) ────────────────────


def _pdf_for_text(item: Item, staging_folder: Path) -> Path:
    """Path to the PDF to re-extract text from — staging copy if present."""
    staging_path = staging_folder / f"{item.id}.pdf"
    if staging_path.exists():
        return staging_path
    return Path(item.source_path)


def _strip_doi_url(raw: str) -> str:
    """OpenAlex returns DOIs as URLs or with trailing punctuation; normalise."""
    doi = raw.strip().rstrip(".,;:)")
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix) :]
    return doi


def _find_first_new_doi(text: str, known_doi: str | None) -> str | None:
    """Return the first DOI found in ``text`` that differs from ``known_doi``.

    Comparison is case-insensitive after stripping trailing punctuation —
    OpenAlex sometimes returns DOIs with slightly different casing or
    trailing characters than what the regex captures on the PDF side.
    """
    known_norm = (known_doi or "").strip().lower().rstrip(".,;:)")
    for match in _DOI_RE.finditer(text):
        candidate = _strip_doi_url(match.group(0)).lower()
        if candidate and candidate != known_norm:
            return candidate
    return None


def _find_extra_identifiers(text: str) -> dict[str, str]:
    """Return the first arXiv/ISBN/Handle/REPEC id found, for CSV reporting only.

    04a in this PR only retries Route A on a new DOI. arXiv / ISBN /
    Handle / REPEC are captured for the report but not re-fetched —
    Route A's resolver (``OpenAlexClient.work_by_doi``) speaks DOI.
    A later iteration can add dedicated resolvers (e.g. arXiv via
    ``10.48550/arXiv.XXXX.XXXXX`` synthetic DOIs, REPEC via the EconPapers
    API) without changing this module's public surface.
    """
    extras: dict[str, str] = {}
    if (m := _ARXIV_RE.search(text)) is not None:
        extras["arxiv"] = m.group(1)
    if (m := _ISBN_RE.search(text)) is not None:
        extras["isbn"] = m.group(1).strip()
    if (m := _HANDLE_RE.search(text)) is not None:
        extras["handle"] = m.group(1)
    if (m := _REPEC_RE.search(text)) is not None:
        extras["repec"] = m.group(0)
    return extras


# ─── Semantic Scholar → Zotero mapping (04c) ──────────────────────────────


def map_semantic_scholar_to_zotero(
    paper: dict[str, Any],
) -> dict[str, Any] | None:
    """Map a Semantic Scholar paper record to a Zotero item payload.

    Returns ``None`` when the quality gate fails (missing title, missing
    authors). Schema mirrors ``stage_03_import.map_openalex_to_zotero`` so
    both mappers feed the same Zotero ``create_items`` endpoint. Semantic
    Scholar does not expose a structured item type, so we default to
    ``journalArticle`` — good enough for the fallback path; 04d's LLM can
    correct it when the cascade reaches that far.
    """
    title = (paper.get("title") or "").strip()
    if not title:
        return None

    raw_authors = paper.get("authors") or []
    creators: list[dict[str, str]] = []
    for entry in raw_authors:
        if not isinstance(entry, dict):
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        first, last = _split_name(name)
        creators.append(
            {"creatorType": "author", "firstName": first, "lastName": last}
        )
    if not creators:
        return None

    year = paper.get("year")
    date_str = str(year) if isinstance(year, int) else ""

    venue_raw = paper.get("venue")
    venue = venue_raw.strip() if isinstance(venue_raw, str) else ""

    abstract_raw = paper.get("abstract")
    abstract = abstract_raw.strip() if isinstance(abstract_raw, str) else ""

    doi = _doi_from_ss_paper(paper) or ""

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


def _doi_from_ss_paper(paper: dict[str, Any]) -> str | None:
    """Extract a normalised DOI string from a Semantic Scholar paper, or None."""
    ext = paper.get("externalIds") or {}
    if not isinstance(ext, dict):
        return None
    raw = ext.get("DOI")
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned or None


def _doi_from_openalex_work(work: dict[str, Any]) -> str | None:
    """Extract a normalised DOI from an OpenAlex work, or None."""
    raw = work.get("doi")
    if not isinstance(raw, str) or not raw.strip():
        return None
    cleaned = _strip_doi_url(raw)
    return cleaned or None


def _pick_best_fuzzy_match(
    query_title: str,
    candidates: list[dict[str, Any]],
    *,
    title_key: str = "title",
) -> tuple[dict[str, Any], float] | None:
    """Return the top candidate scoring ``>= _FUZZ_THRESHOLD``, with its score.

    Uses ``rapidfuzz.fuzz.token_set_ratio`` per plan_01 §3 Stage 04b.
    Candidates with missing or empty titles are skipped. Returns ``None``
    when no candidate clears the threshold — the caller then falls
    through to the next substage.
    """
    best: tuple[dict[str, Any], float] | None = None
    for cand in candidates:
        candidate_title = cand.get(title_key)
        if not isinstance(candidate_title, str) or not candidate_title.strip():
            continue
        score = float(fuzz.token_set_ratio(query_title, candidate_title))
        if score < _FUZZ_THRESHOLD:
            continue
        if best is None or score > best[1]:
            best = (cand, score)
    return best


# ─── Create parent + reparent orphan (shared by 04a / 04b / 04c) ──────────


async def _create_parent_and_reparent(
    item: Item,
    payload: dict[str, Any],
    *,
    doi: str | None,
    zotero_client: ZoteroClient,
    dry_run: bool,
) -> tuple[str | None, str | None]:
    """Create (or dedup on ``doi``) a parent, then reparent the orphan under it.

    Returns ``(new_parent_key, error)``. ADR 014 dedup policy: when an
    existing Zotero item already has the DOI and already carries a PDF,
    we link the Item row to that key and skip reparenting (no duplicate
    PDFs). When the existing item has no PDF, we reparent our orphan
    under it. When no existing item matches (or no DOI available at all),
    we create a fresh parent and reparent.

    Shared helper so 04a, 04b, and 04c follow identical Zotero semantics
    after each finds its metadata through its own source.
    """
    if dry_run:
        return "DRYRUN_PARENT", None

    existing_parent: str | None = None
    if doi:
        existing_parent = _find_existing_doi(zotero_client, doi)

    if existing_parent is not None:
        if _existing_has_pdf_attachment(zotero_client, existing_parent):
            log.info(
                "stage_04.dedup.existing_item_with_pdf",
                doi=doi,
                existing_key=existing_parent,
            )
            return existing_parent, None
        new_parent_key = existing_parent
    else:
        create_response = zotero_client.create_items([payload])
        success = create_response.get("success") or {}
        if not isinstance(success, dict) or not success:
            return None, "create_items_no_success_key"
        first = next(iter(success.values()))
        if not isinstance(first, str) or not first:
            return None, "create_items_bad_key"
        new_parent_key = first

    orphan_key = item.zotero_item_key
    if orphan_key is None:
        # Shouldn't happen — Stage 04 precondition is stage_completed=3.
        return None, "orphan_key_missing"
    try:
        orphan = zotero_client.item(orphan_key)
    except Exception as exc:
        return None, f"fetch_orphan:{type(exc).__name__}:{exc}"

    orphan_data = orphan.get("data") or {}
    if not orphan_data:
        return None, "orphan_data_missing"
    updated = dict(orphan_data)
    updated["parentItem"] = new_parent_key
    try:
        zotero_client.update_item(updated)
    except Exception as exc:
        return None, f"update_item:{type(exc).__name__}:{exc}"

    return new_parent_key, None


# ─── Route A retry — uses map_openalex_to_zotero from stage_03 ────────────


async def _retry_route_a(
    item: Item,
    new_doi: str,
    *,
    zotero_client: ZoteroClient,
    openalex_client: OpenAlexClient,
    dry_run: bool,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """Resolve ``new_doi`` via OpenAlex, then create-parent + reparent.

    Returns ``(new_parent_key, mapped_payload, error)`` — callers persist
    ``mapped_payload`` in ``Item.metadata_json`` on success. ``error``
    is a human-readable reason on failure (quality gate, OpenAlex 404,
    Zotero create/update failures).
    """
    try:
        work = await openalex_client.work_by_doi(new_doi)
    except Exception as exc:
        log.warning("stage_04a.openalex_error", doi=new_doi, error=str(exc))
        return None, None, f"openalex_error:{type(exc).__name__}:{exc}"

    if work is None:
        return None, None, "openalex_404"

    payload = map_openalex_to_zotero(work)
    if payload is None:
        return None, None, "quality_gate_failed"

    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=new_doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None:
        return None, None, error
    return parent_key, payload, None


# ─── Per-item substage 04a ────────────────────────────────────────────────


async def _enrich_04a_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    openalex_client: OpenAlexClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04a for a single item. Returns the row and the mapped metadata (or None)."""
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        pages = extract_text_pages(pdf_path, max_pages=_PAGES_FOR_ID_EXTRACTION)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )
    combined_text = "\n".join(pages)

    new_doi = _find_first_new_doi(combined_text, item.detected_doi)
    if new_doi is None:
        # No new DOI; 04a cannot make progress. Fall through to 04b in
        # a later iteration (not this PR).
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=None,
            ),
            None,
        )

    new_parent_key, mapped, error = await _retry_route_a(
        item,
        new_doi,
        zotero_client=zotero_client,
        openalex_client=openalex_client,
        dry_run=dry_run,
    )
    if error is not None or new_parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=new_doi,
                status="no_progress" if error in {"openalex_404", "quality_gate_failed"} else "failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04a"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=new_parent_key,
            substage_resolved="04a",
            new_doi=new_doi,
            status=status,
            error=None,
        ),
        mapped,
    )


# ─── Per-item substages 04b and 04c (title fuzzy match) ──────────────────


async def _enrich_04b_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    openalex_client: OpenAlexClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04b for a single item: title → OpenAlex search → fuzzy match → reparent."""
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        title = extract_probable_title(pdf_path)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    if title is None:
        # Generic heading or pathological layout; falls to 04c later.
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="skipped_generic_title",
                error=None,
            ),
            None,
        )

    try:
        candidates = await openalex_client.search_works(title)
    except Exception as exc:
        log.warning("stage_04b.openalex_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"openalex_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    picked = _pick_best_fuzzy_match(title, candidates, title_key="title")
    if picked is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=None,
            ),
            None,
        )
    best, _score = picked

    payload = map_openalex_to_zotero(best)
    if payload is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="quality_gate_failed",
            ),
            None,
        )

    doi = _doi_from_openalex_work(best)
    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None or parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=doi,
                status="failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04b"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=parent_key,
            substage_resolved="04b",
            new_doi=doi,
            status=status,
            error=None,
        ),
        payload,
    )


# ─── 04bs (SciELO via Crossref Member 530, ADR 018 + ADR 019) ────────────


def _picked_via_crossref_title(
    title: str, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any], float] | None:
    """Apply the cascade's fuzzy threshold against Crossref's list-shaped title.

    Crossref returns ``record["title"]`` as a list of strings (typically
    one element). :func:`_pick_best_fuzzy_match` skips non-string titles,
    so we project to a flat ``{"title": str, "_record": dict}`` shape
    first.
    """
    flat: list[dict[str, Any]] = []
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        title_list = cand.get("title")
        if not isinstance(title_list, list) or not title_list:
            continue
        flat_title = title_list[0]
        if not isinstance(flat_title, str) or not flat_title.strip():
            continue
        flat.append({"title": flat_title, "_record": cand})
    picked = _pick_best_fuzzy_match(title, flat, title_key="title")
    if picked is None:
        return None
    best_flat, score = picked
    return best_flat["_record"], score


async def _enrich_04bs_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    scielo_client: SciELoClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04bs for a single item: title → SciELO (Crossref member:530) → fuzzy → reparent.

    On transient HTTP failure (403/429/502/503 — see ADR 018 §Resilience
    policy) the substage returns ``no_progress`` so the cascade flows to
    04bd; only genuine bugs land as ``failed``.
    """
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        title = extract_probable_title(pdf_path)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    if title is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="skipped_generic_title",
                error=None,
            ),
            None,
        )

    try:
        candidates = await scielo_client.search_articles(title)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in _LATAM_TRANSIENT_STATUSES:
            log.warning(
                "stage_04bs.scielo_unavailable", title=title, status=status_code
            )
            return (
                EnrichRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    zotero_item_key_before=item.zotero_item_key,
                    zotero_item_key_after=None,
                    substage_resolved=None,
                    new_doi=None,
                    status="no_progress",
                    error=f"scielo_unavailable:{status_code}",
                ),
                None,
            )
        log.warning("stage_04bs.scielo_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"scielo_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )
    except Exception as exc:
        log.warning("stage_04bs.scielo_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"scielo_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    picked = _picked_via_crossref_title(title, candidates)
    if picked is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=None,
            ),
            None,
        )
    best, _score = picked

    payload = map_scielo_to_zotero(best)
    if payload is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="quality_gate_failed",
            ),
            None,
        )

    doi = _doi_from_scielo_record(best)
    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None or parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=doi,
                status="failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04bs"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=parent_key,
            substage_resolved="04bs",
            new_doi=doi,
            status=status,
            error=None,
        ),
        payload,
    )


# ─── 04bd (DOAJ, ADR 018) ─────────────────────────────────────────────────


def _picked_via_bibjson_title(
    title: str, candidates: list[dict[str, Any]]
) -> tuple[dict[str, Any], float] | None:
    """Apply the cascade's fuzzy threshold against DOAJ's nested title field.

    DOAJ records carry the title at ``record["bibjson"]["title"]`` rather
    than ``record["title"]`` — :func:`_pick_best_fuzzy_match` doesn't
    follow nested keys, so we project to a flat shape first.
    """
    flat: list[dict[str, Any]] = []
    for cand in candidates:
        bib = cand.get("bibjson") if isinstance(cand, dict) else None
        if not isinstance(bib, dict):
            continue
        flat_title = bib.get("title")
        if not isinstance(flat_title, str) or not flat_title.strip():
            continue
        flat.append({"title": flat_title, "_record": cand})
    picked = _pick_best_fuzzy_match(title, flat, title_key="title")
    if picked is None:
        return None
    best_flat, score = picked
    return best_flat["_record"], score


async def _enrich_04bd_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    doaj_client: DOAJClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04bd for a single item: title → DOAJ search → fuzzy match → reparent.

    Same resilience policy as 04bs: HTTP 403/429/502/503 →
    ``no_progress`` (cascade falls through to 04c); other exceptions
    → ``failed``.
    """
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        title = extract_probable_title(pdf_path)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    if title is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="skipped_generic_title",
                error=None,
            ),
            None,
        )

    try:
        candidates = await doaj_client.search_articles(title)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        if status_code in _LATAM_TRANSIENT_STATUSES:
            log.warning(
                "stage_04bd.doaj_unavailable", title=title, status=status_code
            )
            return (
                EnrichRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    zotero_item_key_before=item.zotero_item_key,
                    zotero_item_key_after=None,
                    substage_resolved=None,
                    new_doi=None,
                    status="no_progress",
                    error=f"doaj_unavailable:{status_code}",
                ),
                None,
            )
        log.warning("stage_04bd.doaj_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"doaj_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )
    except Exception as exc:
        log.warning("stage_04bd.doaj_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"doaj_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    picked = _picked_via_bibjson_title(title, candidates)
    if picked is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=None,
            ),
            None,
        )
    best, _score = picked

    payload = map_doaj_to_zotero(best)
    if payload is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="quality_gate_failed",
            ),
            None,
        )

    doi = _doi_from_doaj_record(best)
    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None or parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=doi,
                status="failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04bd"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=parent_key,
            substage_resolved="04bd",
            new_doi=doi,
            status=status,
            error=None,
        ),
        payload,
    )


async def _enrich_04c_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    semantic_scholar_client: SemanticScholarClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04c for a single item: title → Semantic Scholar search → fuzzy match → reparent."""
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        title = extract_probable_title(pdf_path)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    if title is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="skipped_generic_title",
                error=None,
            ),
            None,
        )

    try:
        candidates = await semantic_scholar_client.search_paper(title)
    except Exception as exc:
        log.warning("stage_04c.semantic_scholar_error", title=title, error=str(exc))
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"semantic_scholar_error:{type(exc).__name__}:{exc}",
            ),
            None,
        )

    picked = _pick_best_fuzzy_match(title, candidates, title_key="title")
    if picked is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=None,
            ),
            None,
        )
    best, _score = picked

    payload = map_semantic_scholar_to_zotero(best)
    if payload is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="quality_gate_failed",
            ),
            None,
        )

    doi = _doi_from_ss_paper(best)
    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None or parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=doi,
                status="failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04c"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=parent_key,
            substage_resolved="04c",
            new_doi=doi,
            status=status,
            error=None,
        ),
        payload,
    )


# ─── LLM extraction (04d) ────────────────────────────────────────────────


class _LLMAuthor(BaseModel):
    """One author name as the LLM emits it."""

    first: str = ""
    last: str = ""


class LLMExtractedMetadata(BaseModel):
    """Pydantic schema for the JSON the LLM returns in substage 04d.

    Fields default to empty / None so a partial response that
    Pydantic accepts still lets us run the quality gate on top of it.
    The gate lives in :func:`map_llm_extraction_to_zotero`, not here.
    """

    title: str | None = None
    authors: list[_LLMAuthor] = Field(default_factory=list)
    year: int | None = None
    item_type: str | None = None
    venue: str | None = None
    doi: str | None = None
    abstract: str | None = None


def map_llm_extraction_to_zotero(
    extracted: LLMExtractedMetadata,
) -> dict[str, Any] | None:
    """Map an LLM extraction to a Zotero item payload.

    Returns ``None`` when the quality gate fails — missing title, missing
    authors, or an ``item_type`` not in :data:`_LLM_ALLOWED_ITEM_TYPES`.
    Callers that receive ``None`` fall through to 04e (quarantine).
    """
    title = (extracted.title or "").strip()
    if not title:
        return None

    creators: list[dict[str, str]] = []
    for author in extracted.authors:
        first = author.first.strip()
        last = author.last.strip()
        if not first and not last:
            continue
        creators.append(
            {"creatorType": "author", "firstName": first, "lastName": last}
        )
    if not creators:
        return None

    item_type = (extracted.item_type or "").strip() or "journalArticle"
    if item_type not in _LLM_ALLOWED_ITEM_TYPES:
        return None

    date_str = str(extracted.year) if isinstance(extracted.year, int) else ""
    venue = (extracted.venue or "").strip()
    abstract = (extracted.abstract or "").strip()
    doi = (extracted.doi or "").strip()

    payload: dict[str, Any] = {
        "itemType": item_type,
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


def _parse_llm_response(usage: Any) -> LLMExtractedMetadata | None:
    """Parse the LLM's ``UsageRecord.response`` into :class:`LLMExtractedMetadata`.

    Returns ``None`` on malformed JSON or schema mismatch — callers retry
    once, then fall through to 04e.
    """
    response = getattr(usage, "response", None)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return None
    message = getattr(choices[0], "message", None)
    content = getattr(message, "content", None) if message is not None else None
    if not isinstance(content, str) or not content:
        return None
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        return LLMExtractedMetadata.model_validate(raw)
    except ValidationError:
        return None


async def _enrich_04d_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    openai_client: OpenAIClient,
    dry_run: bool,
) -> tuple[EnrichRow, dict[str, Any] | None]:
    """04d for a single item: first 2 pages → LLM JSON → Zotero.

    Budget enforcement lives inside ``openai_client``; a ``BudgetExceeded
    Error`` bubbles up so the orchestrator can short-circuit the rest of
    the cascade. Per-call JSON parsing retries once; after that the item
    falls through to 04e as ``no_progress``.
    """
    pdf_path = _pdf_for_text(item, staging_folder)
    if not pdf_path.exists():
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_missing:{pdf_path}",
            ),
            None,
        )

    try:
        pages = extract_text_pages(pdf_path, max_pages=_PAGES_FOR_LLM_EXTRACTION)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"pdf_extract:{type(exc).__name__}:{exc}",
            ),
            None,
        )
    text = "\n".join(pages).strip()
    if not text:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="empty_pdf_text",
            ),
            None,
        )

    extracted: LLMExtractedMetadata | None = None
    last_error: str | None = None
    for _attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            usage = await openai_client.extract_metadata(text=text)
        except BudgetExceededError:
            # Propagate: the orchestrator routes remaining items to 04e.
            raise
        except Exception as exc:
            last_error = f"openai_error:{type(exc).__name__}:{exc}"
            continue
        extracted = _parse_llm_response(usage)
        if extracted is not None:
            break
        last_error = "llm_json_invalid"

    if extracted is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error=last_error or "llm_no_extraction",
            ),
            None,
        )

    payload = map_llm_extraction_to_zotero(extracted)
    if payload is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="no_progress",
                error="quality_gate_failed",
            ),
            None,
        )

    doi = (extracted.doi or "").strip() or None
    parent_key, error = await _create_parent_and_reparent(
        item,
        payload,
        doi=doi,
        zotero_client=zotero_client,
        dry_run=dry_run,
    )
    if error is not None or parent_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=item.zotero_item_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=doi,
                status="failed",
                error=error,
            ),
            None,
        )

    status: EnrichStatus = "dry_run" if dry_run else "enriched_04d"
    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=item.zotero_item_key,
            zotero_item_key_after=parent_key,
            substage_resolved="04d",
            new_doi=doi,
            status=status,
            error=None,
        ),
        payload,
    )


# ─── Quarantine (04e) ────────────────────────────────────────────────────


def _ensure_quarantine_collection(
    zotero_client: ZoteroClient, name: str = _QUARANTINE_COLLECTION_NAME
) -> str | None:
    """Return the Zotero key of the quarantine collection, creating it if needed.

    Idempotent: looks up by ``name`` first, creates only if absent. Returns
    ``None`` when the client is in dry-run mode so 04e can still record the
    intent without hitting the API.
    """
    if zotero_client.dry_run:
        return None
    for col in zotero_client.collections():
        data = col.get("data") or {}
        if data.get("name") == name:
            key = col.get("key") or data.get("key")
            if isinstance(key, str) and key:
                return key
    created = zotero_client.create_collections([{"name": name}])
    success = created.get("success") or {}
    if isinstance(success, dict):
        for key in success.values():
            if isinstance(key, str) and key:
                return key
    return None


async def _enrich_04e_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    quarantine_collection_key: str | None,
    last_error: str | None,
    dry_run: bool,
) -> tuple[EnrichRow, str]:
    """Move the orphan into the Quarantine collection and tag it.

    Returns the row and the short text snippet used for the
    ``quarantine_report.csv`` (plan_01 §3.04e). Never raises on Zotero
    write failures — surfaces them in the row instead so the rest of the
    batch keeps moving.
    """
    orphan_key = item.zotero_item_key
    pdf_path = _pdf_for_text(item, staging_folder)
    snippet = ""
    if pdf_path.exists():
        try:
            pages = extract_text_pages(pdf_path, max_pages=1)
            if pages:
                snippet = pages[0].strip().replace("\n", " ")[
                    :_QUARANTINE_SNIPPET_CHARS
                ]
        except Exception as exc:
            log.warning(
                "stage_04e.snippet_extract_failed",
                sha256=item.id,
                error=str(exc),
            )

    if orphan_key is None:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=None,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error="orphan_key_missing",
            ),
            snippet,
        )

    if dry_run:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=orphan_key,
                zotero_item_key_after=orphan_key,
                substage_resolved="04e",
                new_doi=None,
                status="dry_run",
                error=last_error,
            ),
            snippet,
        )

    # Fetch current orphan so add_tags / addto_collection get the latest version.
    try:
        orphan = zotero_client.item(orphan_key)
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=orphan_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"fetch_orphan:{type(exc).__name__}:{exc}",
            ),
            snippet,
        )

    try:
        zotero_client.add_tags(orphan, [_QUARANTINE_TAG])
    except Exception as exc:
        return (
            EnrichRow(
                sha256=item.id,
                source_path=item.source_path,
                zotero_item_key_before=orphan_key,
                zotero_item_key_after=None,
                substage_resolved=None,
                new_doi=None,
                status="failed",
                error=f"add_tags:{type(exc).__name__}:{exc}",
            ),
            snippet,
        )

    if quarantine_collection_key is not None:
        try:
            zotero_client.addto_collection(quarantine_collection_key, orphan)
        except Exception as exc:
            return (
                EnrichRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    zotero_item_key_before=orphan_key,
                    zotero_item_key_after=None,
                    substage_resolved=None,
                    new_doi=None,
                    status="failed",
                    error=f"addto_collection:{type(exc).__name__}:{exc}",
                ),
                snippet,
            )

    return (
        EnrichRow(
            sha256=item.id,
            source_path=item.source_path,
            zotero_item_key_before=orphan_key,
            zotero_item_key_after=orphan_key,
            substage_resolved="04e",
            new_doi=None,
            status="quarantined_04e",
            error=last_error,
        ),
        snippet,
    )


def _quarantine_csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"quarantine_report_{timestamp}{suffix}.csv"


def _write_quarantine_csv(
    path: Path, rows: Iterable[tuple[EnrichRow, str]]
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_QUARANTINE_CSV_COLUMNS))
        writer.writeheader()
        for row, snippet in rows:
            writer.writerow(
                {
                    "sha256": row.sha256,
                    "source_path": row.source_path,
                    "text_snippet": snippet,
                    "reason": row.error or "cascade_exhausted",
                }
            )


# ─── Eligible-items query ─────────────────────────────────────────────────


def _select_eligible(session: Session) -> list[Item]:
    """Items ready for Stage 04: stage_completed=3 + Route C (orphan)."""
    stmt = (
        select(Item)
        .where(Item.stage_completed == _PREREQ_STAGE)
        .where(Item.import_route == "C")
        .where(Item.in_quarantine == False)  # noqa: E712
    )
    return list(session.exec(stmt))


# ─── Public entry points ──────────────────────────────────────────────────


def run_enrich(
    *,
    substage: EnrichSubstage = "04a",
    dry_run: bool = False,
    max_cost: float | None = None,
    settings: Settings | None = None,
    engine: Engine | None = None,
    zotero_client: ZoteroClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    scielo_client: SciELoClient | None = None,
    doaj_client: DOAJClient | None = None,
    semantic_scholar_client: SemanticScholarClient | None = None,
    openai_client: OpenAIClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> EnrichResult:
    """Run one substage (or the full cascade) of Stage 04.

    ``04a`` — identifier extraction + OpenAlex DOI retry.
    ``04b`` — title fuzzy match against OpenAlex.
    ``04bs`` — title fuzzy match against SciELO via Crossref Member 530
    (ADR 018 + ADR 019). Default ON; opt-out via ``S1_ENABLE_SCIELO=false``.
    ``04bd`` — title fuzzy match against DOAJ (ADR 018). Default ON;
    opt-out via ``S1_ENABLE_DOAJ=false``.
    ``04c`` — title fuzzy match against Semantic Scholar.
    ``04d`` — LLM (``gpt-4o-mini``) extraction with budget enforcement
    (``MAX_COST_USD_STAGE_04``, overridable via ``max_cost``).
    ``04e`` — Quarantine: tag ``needs-manual-review`` + move to the
    Quarantine collection + append to ``quarantine_report.csv``.
    ``all`` — per-item cascade 04a → 04b → 04bs → 04bd → 04c → 04d → 04e.

    Once 04d's budget is exhausted during an ``all`` or ``04d`` run, the
    remaining items route directly to 04e without retrying the LLM.
    """
    return asyncio.run(
        _run_enrich_async(
            substage=substage,
            dry_run=dry_run,
            max_cost=max_cost,
            settings=settings,
            engine=engine,
            zotero_client=zotero_client,
            openalex_client=openalex_client,
            scielo_client=scielo_client,
            doaj_client=doaj_client,
            semantic_scholar_client=semantic_scholar_client,
            openai_client=openai_client,
            sleep=sleep,
            now=now,
        )
    )


async def _run_enrich_async(
    *,
    substage: EnrichSubstage,
    dry_run: bool,
    max_cost: float | None,
    settings: Settings | None,
    engine: Engine | None,
    zotero_client: ZoteroClient | None,
    openalex_client: OpenAlexClient | None,
    scielo_client: SciELoClient | None,
    doaj_client: DOAJClient | None,
    semantic_scholar_client: SemanticScholarClient | None,
    openai_client: OpenAIClient | None,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], datetime],
) -> EnrichResult:
    _ = sleep  # reserved for future batch pacing
    settings = settings or Settings()
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    if zotero_client is None:
        zotero_client = ZoteroClient(
            library_id=settings.zotero.library_id,
            library_type=settings.zotero.library_type,
            api_key=settings.zotero.api_key.get_secret_value(),
            local=settings.zotero.local_api,
            local_api_host=settings.zotero.local_api_host or None,
            dry_run=dry_run,
        )
    if openalex_client is None:
        email = settings.behavior.user_email or None
        openalex_client = OpenAlexClient(user_email=email)
    # 04bs / 04bd are opt-out-able (ADR 018 + ADR 019). Build only when
    # the corresponding flag is True; the cascade short-circuits a
    # ``None`` client cleanly. Explicit substage selection of a disabled
    # source is a config error and raises StageAbortedError below.
    if scielo_client is None and settings.behavior.s1_enable_scielo:
        scielo_email = settings.behavior.user_email or None
        scielo_client = SciELoClient(user_email=scielo_email)
    if doaj_client is None and settings.behavior.s1_enable_doaj:
        doaj_email = settings.behavior.user_email or None
        doaj_client = DOAJClient(user_email=doaj_email)
    if semantic_scholar_client is None:
        ss_key = settings.semantic_scholar.api_key.get_secret_value() or None
        semantic_scholar_client = SemanticScholarClient(api_key=ss_key)

    if substage == "04bs" and scielo_client is None:
        raise StageAbortedError(
            "Substage 04bs requires S1_ENABLE_SCIELO=true. Set it in .env "
            "or pass scielo_client explicitly."
        )
    if substage == "04bd" and doaj_client is None:
        raise StageAbortedError(
            "Substage 04bd requires S1_ENABLE_DOAJ=true. Set it in .env "
            "or pass doaj_client explicitly."
        )

    # 04d / "all" need the OpenAI client + a budget. Build lazily so the
    # free substages don't require ``OPENAI_API_KEY``.
    needs_llm = substage in ("04d", "all")
    if needs_llm and openai_client is None:
        openai_api_key = settings.openai.api_key.get_secret_value()
        if not openai_api_key:
            raise StageAbortedError(
                "Substage 04d / 'all' requires OPENAI_API_KEY. Set it in .env "
                "or pass openai_client explicitly."
            )
        budget = (
            max_cost if max_cost is not None else settings.budgets.max_cost_usd_stage_04
        )
        openai_client = OpenAIClient(api_key=openai_api_key, budget_usd=budget)

    # 04e / "all" may quarantine items; look up the collection once.
    needs_quarantine = substage in ("04e", "all")
    quarantine_collection_key: str | None = None
    if needs_quarantine:
        quarantine_collection_key = _ensure_quarantine_collection(zotero_client)

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[EnrichRow] = []
    quarantine_rows: list[tuple[EnrichRow, str]] = []
    staging_folder = settings.paths.staging_folder

    bind(stage=_STAGE, dry_run=dry_run, substage=substage)
    log.info("stage_started", substage=substage)

    # Tracks whether 04d's budget has been exhausted during the current run;
    # once tripped, remaining items route directly to 04e without retrying
    # the LLM (cheaper than catching BudgetExceededError on every item).
    budget_exhausted = False

    async def _run_per_item_cascade(
        item: Item,
    ) -> tuple[EnrichRow, dict[str, Any] | None, str | None]:
        """Walk an item through 04a → 04b → 04c → 04d → 04e.

        Returns ``(row, mapped_payload, last_error)``. ``mapped_payload``
        is set only on a successful enrichment; ``last_error`` carries the
        most recent non-progress reason for the quarantine_report.csv.
        """
        nonlocal budget_exhausted
        last_error: str | None = None

        # 04a
        row_a, mapped_a = await _enrich_04a_one(
            item,
            staging_folder=staging_folder,
            zotero_client=zotero_client,
            openalex_client=openalex_client,
            dry_run=dry_run,
        )
        if row_a.status in _ENRICHED_STATUSES or row_a.status == "dry_run":
            return row_a, mapped_a, None
        if row_a.status == "failed":
            return row_a, None, row_a.error
        last_error = row_a.error

        # 04b
        row_b, mapped_b = await _enrich_04b_one(
            item,
            staging_folder=staging_folder,
            zotero_client=zotero_client,
            openalex_client=openalex_client,
            dry_run=dry_run,
        )
        if row_b.status in _ENRICHED_STATUSES or row_b.status == "dry_run":
            return row_b, mapped_b, None
        if row_b.status == "failed":
            return row_b, None, row_b.error
        last_error = row_b.error or last_error

        # 04bs (SciELO via Crossref Member 530 — ADR 018 + ADR 019)
        if scielo_client is not None:
            row_bs, mapped_bs = await _enrich_04bs_one(
                item,
                staging_folder=staging_folder,
                zotero_client=zotero_client,
                scielo_client=scielo_client,
                dry_run=dry_run,
            )
            if row_bs.status in _ENRICHED_STATUSES or row_bs.status == "dry_run":
                return row_bs, mapped_bs, None
            if row_bs.status == "failed":
                return row_bs, None, row_bs.error
            last_error = row_bs.error or last_error

        # 04bd (DOAJ — ADR 018)
        if doaj_client is not None:
            row_bd, mapped_bd = await _enrich_04bd_one(
                item,
                staging_folder=staging_folder,
                zotero_client=zotero_client,
                doaj_client=doaj_client,
                dry_run=dry_run,
            )
            if row_bd.status in _ENRICHED_STATUSES or row_bd.status == "dry_run":
                return row_bd, mapped_bd, None
            if row_bd.status == "failed":
                return row_bd, None, row_bd.error
            last_error = row_bd.error or last_error

        # 04c
        row_c, mapped_c = await _enrich_04c_one(
            item,
            staging_folder=staging_folder,
            zotero_client=zotero_client,
            semantic_scholar_client=semantic_scholar_client,
            dry_run=dry_run,
        )
        if row_c.status in _ENRICHED_STATUSES or row_c.status == "dry_run":
            return row_c, mapped_c, None
        if row_c.status == "failed":
            return row_c, None, row_c.error
        last_error = row_c.error or last_error

        # 04d (skip if budget already tripped earlier in the run)
        if not budget_exhausted and openai_client is not None:
            try:
                row_d, mapped_d = await _enrich_04d_one(
                    item,
                    staging_folder=staging_folder,
                    zotero_client=zotero_client,
                    openai_client=openai_client,
                    dry_run=dry_run,
                )
            except BudgetExceededError as exc:
                log.warning("stage_04d.budget_exceeded", error=str(exc))
                budget_exhausted = True
                last_error = f"budget_exceeded:{exc}"
            else:
                if row_d.status in _ENRICHED_STATUSES or row_d.status == "dry_run":
                    return row_d, mapped_d, None
                if row_d.status == "failed":
                    return row_d, None, row_d.error
                last_error = row_d.error or last_error

        # 04e (quarantine)
        row_e, snippet = await _enrich_04e_one(
            item,
            staging_folder=staging_folder,
            zotero_client=zotero_client,
            quarantine_collection_key=quarantine_collection_key,
            last_error=last_error,
            dry_run=dry_run,
        )
        if row_e.status == "quarantined_04e":
            quarantine_rows.append((row_e, snippet))
        return row_e, None, last_error

    with Session(engine) as session:
        if not dry_run:
            session.add(run)
            session.flush()

        try:
            items = _select_eligible(session)
            log.info("stage_04.eligible_items", count=len(items))

            for item in items:
                if substage == "04a":
                    row, mapped = await _enrich_04a_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        openalex_client=openalex_client,
                        dry_run=dry_run,
                    )
                elif substage == "04b":
                    row, mapped = await _enrich_04b_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        openalex_client=openalex_client,
                        dry_run=dry_run,
                    )
                elif substage == "04bs":
                    assert scielo_client is not None  # gated by the StageAbortedError check
                    row, mapped = await _enrich_04bs_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        scielo_client=scielo_client,
                        dry_run=dry_run,
                    )
                elif substage == "04bd":
                    assert doaj_client is not None  # gated by the StageAbortedError check
                    row, mapped = await _enrich_04bd_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        doaj_client=doaj_client,
                        dry_run=dry_run,
                    )
                elif substage == "04c":
                    row, mapped = await _enrich_04c_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        semantic_scholar_client=semantic_scholar_client,
                        dry_run=dry_run,
                    )
                elif substage == "04d":
                    assert openai_client is not None  # needs_llm branch built it
                    try:
                        row, mapped = await _enrich_04d_one(
                            item,
                            staging_folder=staging_folder,
                            zotero_client=zotero_client,
                            openai_client=openai_client,
                            dry_run=dry_run,
                        )
                    except BudgetExceededError as exc:
                        log.warning("stage_04d.budget_exceeded", error=str(exc))
                        budget_exhausted = True
                        row = EnrichRow(
                            sha256=item.id,
                            source_path=item.source_path,
                            zotero_item_key_before=item.zotero_item_key,
                            zotero_item_key_after=None,
                            substage_resolved=None,
                            new_doi=None,
                            status="budget_exceeded",
                            error=str(exc),
                        )
                        mapped = None
                elif substage == "04e":
                    row, snippet = await _enrich_04e_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        quarantine_collection_key=quarantine_collection_key,
                        last_error=item.last_error,
                        dry_run=dry_run,
                    )
                    mapped = None
                    if row.status == "quarantined_04e":
                        quarantine_rows.append((row, snippet))
                elif substage == "all":
                    row, mapped, _last = await _run_per_item_cascade(item)
                else:  # pragma: no cover — typer blocks the unknown values upstream
                    raise NotImplementedError(
                        f"Unreachable substage {substage} inside dispatcher."
                    )
                rows.append(row)

                if row.status in _ENRICHED_STATUSES:
                    if not dry_run:
                        item.zotero_item_key = row.zotero_item_key_after
                        item.import_route = "A"
                        if row.new_doi:
                            item.detected_doi = row.new_doi
                        if mapped is not None:
                            item.metadata_json = json.dumps(mapped)
                        item.stage_completed = max(item.stage_completed, _STAGE)
                        item.last_error = None
                        item.updated_at = now()
                    run.items_processed += 1
                elif row.status == "quarantined_04e":
                    if not dry_run:
                        item.in_quarantine = True
                        item.stage_completed = max(item.stage_completed, _STAGE)
                        item.last_error = row.error
                        item.updated_at = now()
                    run.items_processed += 1
                elif row.status == "dry_run":
                    pass
                elif row.status in (
                    "no_progress",
                    "skipped_generic_title",
                    "budget_exceeded",
                ):
                    if not dry_run and row.error:
                        item.last_error = row.error
                        item.updated_at = now()
                elif row.status == "failed":
                    if not dry_run:
                        item.last_error = row.error
                        item.updated_at = now()
                    run.items_failed += 1

            run.status = "succeeded"
        except StageAbortedError:
            run.status = "aborted"
            raise
        except Exception:
            run.status = "failed"
            raise
        finally:
            run.finished_at = now()
            if not dry_run:
                session.commit()

        run_id = run.id
        items_processed = run.items_processed
        items_failed = run.items_failed

    reports_folder = ensure_dir(settings.paths.reports_folder)
    csv_now = now()
    csv_path = _csv_path(reports_folder, dry_run=dry_run, now=csv_now)
    _write_csv(csv_path, rows)

    quarantine_csv_path: Path | None = None
    if quarantine_rows:
        quarantine_csv_path = _quarantine_csv_path(
            reports_folder, dry_run=dry_run, now=csv_now
        )
        _write_quarantine_csv(quarantine_csv_path, quarantine_rows)

    result = EnrichResult(
        run_id=run_id,
        rows=rows,
        csv_path=csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        items_enriched_04a=sum(1 for r in rows if r.status == "enriched_04a"),
        items_enriched_04b=sum(1 for r in rows if r.status == "enriched_04b"),
        items_enriched_04bs=sum(1 for r in rows if r.status == "enriched_04bs"),
        items_enriched_04bd=sum(1 for r in rows if r.status == "enriched_04bd"),
        items_enriched_04c=sum(1 for r in rows if r.status == "enriched_04c"),
        items_enriched_04d=sum(1 for r in rows if r.status == "enriched_04d"),
        items_quarantined=sum(1 for r in rows if r.status == "quarantined_04e"),
        items_no_progress=sum(1 for r in rows if r.status == "no_progress"),
        items_skipped=sum(
            1 for r in rows if r.status == "skipped_already_enriched"
        ),
        items_skipped_generic_title=sum(
            1 for r in rows if r.status == "skipped_generic_title"
        ),
        quarantine_csv_path=quarantine_csv_path,
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        enriched_04a=result.items_enriched_04a,
        enriched_04b=result.items_enriched_04b,
        enriched_04bs=result.items_enriched_04bs,
        enriched_04bd=result.items_enriched_04bd,
        enriched_04c=result.items_enriched_04c,
        enriched_04d=result.items_enriched_04d,
        quarantined=result.items_quarantined,
        no_progress=result.items_no_progress,
        skipped_generic_title=result.items_skipped_generic_title,
        csv=str(csv_path),
        quarantine_csv=str(quarantine_csv_path) if quarantine_csv_path else None,
    )
    return result


__all__ = [
    "EnrichResult",
    "EnrichRow",
    "EnrichStatus",
    "EnrichSubstage",
    "LLMExtractedMetadata",
    "map_llm_extraction_to_zotero",
    "map_semantic_scholar_to_zotero",
    "run_enrich",
]
# Note: ``map_scielo_to_zotero`` and ``map_doaj_to_zotero`` live with their
# clients in ``zotai.api.scielo`` and ``zotai.api.doaj`` respectively (per
# ADR 018 §"Implementation artefacts" §1 and ADR 019 §Decision §1).
