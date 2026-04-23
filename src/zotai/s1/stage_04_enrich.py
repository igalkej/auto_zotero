"""Stage 04 — enrichment cascade (plan_01 §3 Etapa 04).

This stage takes the items that Stage 03 parked as Route C orphan
attachments (no bibliographic metadata in Zotero beyond the PDF itself)
and walks them through a falling-through cascade that tries
progressively more expensive sources to recover the metadata. The
structure mirrors the spec:

- **04a** — aggressive identifier extraction on pages 1-3 of the PDF.
  If a *new* DOI is found (one not already in ``Item.detected_doi``),
  retry Route A from Stage 03: resolve via OpenAlex, map to Zotero,
  create a parent item, and reparent the existing orphan attachment
  under it. Free ($0).
- **04b** — fuzzy title match against OpenAlex. (Pending PR.)
- **04c** — fuzzy title match against Semantic Scholar. (Pending PR.)
- **04d** — LLM extraction with ``gpt-4o-mini``. Costs ~$0.0004/paper.
  (Pending PR.)
- **04e** — Quarantine. Move to the ``Quarantine`` collection and tag
  ``needs-manual-review``. (Pending PR.)

This module lands **only 04a** plus the scaffolding (types, CSV
writer, ``run_enrich`` entry point). Other substages ship in
follow-up PRs:

- 04b + 04c in the second PR
- 04d + 04e + the full cascade orchestrator + ADRs 005 / 008 in the
  third

Until the cascade orchestrator lands, the CLI (``zotai s1 enrich``)
stays stubbed — partial cascades exposed to the user invite foot-
gunning. Tests drive ``run_enrich(substage="04a")`` directly.

---

Cross-cutting rules (match Stage 03):

- **Idempotent.** Items whose ``import_route`` is already ``'A'`` or
  that have ``stage_completed >= 4`` are skipped.
- **Dedup on DOI.** If a new DOI resolves to a Zotero item the user
  already has, reuse its key rather than creating a parallel item.
  Same policy as Stage 03 (ADR 014): attach iff the existing parent
  has no PDF yet.
- **Dry-run.** No Zotero writes, no DB writes, ``_dryrun``-suffixed
  CSV. Network probes still run (OpenAlex lookups are cheap reads).
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

from rapidfuzz import fuzz
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.api.openalex import OpenAlexClient
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

# TODO(refactor): _find_existing_doi and _existing_has_pdf_attachment
# are reused across Stage 03 + 04a. A follow-up PR should extract them
# into `zotai.api.zotero_queries` (or similar). Not done here to keep
# this PR focused on Stage 04a.

log = get_logger(__name__)

_STAGE: Final[int] = 4
_PREREQ_STAGE: Final[int] = 3
_PAGES_FOR_ID_EXTRACTION: Final[int] = 3
# plan_01 §3 Stage 04b/c: ``rapidfuzz.fuzz.token_set_ratio >= 85`` gates
# every fuzzy title match. Common constant avoids drift between 04b and 04c.
_FUZZ_THRESHOLD: Final[int] = 85

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

EnrichSubstage = Literal["04a", "04b", "04c", "04d", "04e"]
EnrichStatus = Literal[
    "enriched_04a",
    "enriched_04b",
    "enriched_04c",
    "no_progress",
    "skipped_already_enriched",
    "skipped_generic_title",
    "failed",
    "dry_run",
]
_ENRICHED_STATUSES: Final[frozenset[str]] = frozenset(
    {"enriched_04a", "enriched_04b", "enriched_04c"}
)


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
    items_enriched_04c: int
    items_no_progress: int
    items_skipped: int
    items_skipped_generic_title: int


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
    settings: Settings | None = None,
    engine: Engine | None = None,
    zotero_client: ZoteroClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    semantic_scholar_client: SemanticScholarClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> EnrichResult:
    """Run one substage of the enrichment cascade.

    ``04a`` (identifier extraction + OpenAlex DOI retry), ``04b`` (title
    fuzzy match against OpenAlex), and ``04c`` (title fuzzy match against
    Semantic Scholar) are wired. ``04d`` (LLM extraction) and ``04e``
    (quarantine) raise ``NotImplementedError`` until PR 3/3 of issue #6.
    """
    if substage in ("04d", "04e"):
        raise NotImplementedError(
            f"Substage {substage} not yet implemented — '04a', '04b', '04c' only."
        )
    return asyncio.run(
        _run_enrich_async(
            substage=substage,
            dry_run=dry_run,
            settings=settings,
            engine=engine,
            zotero_client=zotero_client,
            openalex_client=openalex_client,
            semantic_scholar_client=semantic_scholar_client,
            sleep=sleep,
            now=now,
        )
    )


async def _run_enrich_async(
    *,
    substage: EnrichSubstage,
    dry_run: bool,
    settings: Settings | None,
    engine: Engine | None,
    zotero_client: ZoteroClient | None,
    openalex_client: OpenAlexClient | None,
    semantic_scholar_client: SemanticScholarClient | None,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], datetime],
) -> EnrichResult:
    _ = sleep  # reserved for future batch pacing; not needed for the free substages
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
    if semantic_scholar_client is None:
        ss_key = settings.semantic_scholar.api_key.get_secret_value() or None
        semantic_scholar_client = SemanticScholarClient(api_key=ss_key)

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[EnrichRow] = []
    staging_folder = settings.paths.staging_folder

    bind(stage=_STAGE, dry_run=dry_run, substage=substage)
    log.info("stage_started", substage=substage)

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
                elif substage == "04c":
                    row, mapped = await _enrich_04c_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        semantic_scholar_client=semantic_scholar_client,
                        dry_run=dry_run,
                    )
                else:  # pragma: no cover — 04d/04e filtered upstream.
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
                elif row.status == "dry_run":
                    pass
                elif row.status in ("no_progress", "skipped_generic_title"):
                    # No state change — item waits for the next substage.
                    pass
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
    csv_path = _csv_path(reports_folder, dry_run=dry_run, now=now())
    _write_csv(csv_path, rows)

    result = EnrichResult(
        run_id=run_id,
        rows=rows,
        csv_path=csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        items_enriched_04a=sum(1 for r in rows if r.status == "enriched_04a"),
        items_enriched_04b=sum(1 for r in rows if r.status == "enriched_04b"),
        items_enriched_04c=sum(1 for r in rows if r.status == "enriched_04c"),
        items_no_progress=sum(1 for r in rows if r.status == "no_progress"),
        items_skipped=sum(
            1 for r in rows if r.status == "skipped_already_enriched"
        ),
        items_skipped_generic_title=sum(
            1 for r in rows if r.status == "skipped_generic_title"
        ),
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        enriched_04a=result.items_enriched_04a,
        enriched_04b=result.items_enriched_04b,
        enriched_04c=result.items_enriched_04c,
        no_progress=result.items_no_progress,
        skipped_generic_title=result.items_skipped_generic_title,
        csv=str(csv_path),
    )
    return result


__all__ = [
    "EnrichResult",
    "EnrichRow",
    "EnrichStatus",
    "EnrichSubstage",
    "map_semantic_scholar_to_zotero",
    "run_enrich",
]
