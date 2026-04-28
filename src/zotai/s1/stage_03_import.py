"""Stage 03 — import PDFs into Zotero (Route A via OpenAlex, Route C orphan).

Consumes items left by Stage 02 with ``stage_completed=2 AND has_text=True
AND classification='academic'`` and produces Zotero items. Two routes
(plan_01 §3 Etapa 03, post-ADR 010):

- **Route A** — ``detected_doi`` is non-null *and* OpenAlex
  (:meth:`OpenAlexClient.work_by_doi`) returns a record with both a
  title and at least one author. Metadata is mapped to Zotero's item
  schema and written via ``pyzotero.create_items``. The PDF is
  attached as a child of the created item.
- **Route C** — everything else. The PDF is uploaded as a top-level
  orphan attachment (``pyzotero.attachment_simple(parent_key=None)``).
  Stage 04's enrichment cascade recovers metadata later.

In both routes the attachment call uses ``attachment_simple`` without a
``linkMode`` override, so Zotero operates in its default **stored**
mode: Zotero reads the bytes from the path we give it and copies them
into ``~/Zotero/storage/<attach_key>/``. The user's original PDF at
``Item.source_path`` is never mutated — we already hash-checked it in
Stage 01 and copied it to ``staging/<hash>.pdf`` in Stage 02 if OCR was
needed.

Which file do we attach?

- If ``staging/<hash>.pdf`` exists (Stage 02 produced an OCR'd copy),
  attach that — it carries the text layer.
- Otherwise attach ``Item.source_path`` — Stage 01 already verified
  native text was present.

Cross-cutting rules:

- **Connectivity probe.** Before the first batch, ``items(limit=1)`` is
  called to verify that Zotero (local API by default, web fallback
  otherwise) is reachable. If not, abort with ``StageAbortedError`` so
  the user sees a clear message before any partial work.
- **Idempotent.** Items with a non-null ``zotero_item_key`` are
  skipped. Re-running the stage on the same DB is a no-op for them.
- **Dedup on DOI.** Before creating a Route A item, we search Zotero
  for an existing item with the same DOI. If found, we link to the
  existing key instead of creating a duplicate.
- **Batching.** Items are processed in batches of ``batch_size``
  (default 50) with ``batch_pause_seconds`` (default 30) of sleep
  between batches — loose rate-limit against Zotero local API and a
  breathing room for the Zotero desktop sync.
- **Dry-run.** No Zotero writes, no DB writes, ``_dryrun``-suffixed
  CSV. The connectivity probe still runs (cheap, verifies the setup).

This module is async to reach :func:`OpenAlexClient.work_by_doi`; the
sync :func:`run_import` wrapper exists for the CLI and for the existing
tests that drive stages synchronously.
"""

from __future__ import annotations

import asyncio
import csv
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.api.openalex import OpenAlexClient
from zotai.api.zotero import ZoteroClient
from zotai.api.zotero_queries import (
    existing_has_pdf_attachment,
    find_existing_doi,
    split_name,
)
from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir
from zotai.utils.logging import bind, get_logger

log = get_logger(__name__)

_STAGE: Final[int] = 3
_PREREQ_STAGE: Final[int] = 2

_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "sha256",
    "source_path",
    "attached_path",
    "detected_doi",
    "import_route",
    "zotero_item_key",
    "status",
    "error",
)

ImportStatus = Literal[
    "imported",
    "deduped",
    "deduped_pdf_added",
    "skipped_already_imported",
    "failed",
    "dry_run",
]

_OPENALEX_TO_ZOTERO_TYPE: Final[dict[str, str]] = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book": "book",
    "book-chapter": "bookSection",
    "dissertation": "thesis",
    "posted-content": "preprint",
    "report": "report",
}


@dataclass(frozen=True)
class ImportRow:
    """One row in ``import_report_<ts>.csv``."""

    sha256: str
    source_path: str
    attached_path: str
    detected_doi: str | None
    import_route: str | None  # 'A' | 'C' | None (when skipped / failed)
    zotero_item_key: str | None
    status: ImportStatus
    error: str | None


@dataclass(frozen=True)
class ImportResult:
    """Aggregate outcome of one ``run_import`` call."""

    run_id: int | None
    rows: list[ImportRow]
    csv_path: Path
    items_processed: int
    items_failed: int
    items_route_a: int
    items_route_c: int
    items_deduped: int
    items_skipped: int


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"import_report_{timestamp}{suffix}.csv"


def _write_csv(csv_path: Path, rows: Iterable[ImportRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sha256": row.sha256,
                    "source_path": row.source_path,
                    "attached_path": row.attached_path,
                    "detected_doi": row.detected_doi or "",
                    "import_route": row.import_route or "",
                    "zotero_item_key": row.zotero_item_key or "",
                    "status": row.status,
                    "error": row.error or "",
                }
            )


# ── OpenAlex → Zotero mapping ─────────────────────────────────────────────


def _reconstruct_abstract(inverted: dict[str, list[int]] | None) -> str:
    """Reconstruct an OpenAlex ``abstract_inverted_index`` into plain text."""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        for idx in idxs:
            positions.append((idx, word))
    positions.sort(key=lambda p: p[0])
    return " ".join(word for _, word in positions)


def _strip_doi_url(raw: str | None) -> str | None:
    """OpenAlex returns DOIs as URLs; Zotero wants the bare identifier."""
    if not raw:
        return None
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


def map_openalex_to_zotero(work: dict[str, Any]) -> dict[str, Any] | None:
    """Map an OpenAlex ``Work`` to a Zotero item payload.

    Returns ``None`` if the quality gate fails — the work lacks a
    non-empty title, or has zero authors. In those cases the caller must
    fall through to Route C. See ADR 010.
    """
    title = (work.get("title") or "").strip()
    if not title:
        return None

    authorships = work.get("authorships") or []
    creators: list[dict[str, str]] = []
    for entry in authorships:
        author = entry.get("author") or {}
        display_name = (author.get("display_name") or "").strip()
        if not display_name:
            continue
        first, last = split_name(display_name)
        creators.append(
            {"creatorType": "author", "firstName": first, "lastName": last}
        )
    if not creators:
        return None

    openalex_type = (work.get("type") or "").strip().lower()
    item_type = _OPENALEX_TO_ZOTERO_TYPE.get(openalex_type, "journalArticle")

    doi = _strip_doi_url(work.get("doi"))

    year = work.get("publication_year")
    date_str = str(year) if isinstance(year, int) else ""

    venue = ""
    primary = work.get("primary_location") or {}
    source = primary.get("source") or {}
    venue_name = source.get("display_name")
    if isinstance(venue_name, str):
        venue = venue_name.strip()

    abstract = _reconstruct_abstract(work.get("abstract_inverted_index"))

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
        # Zotero field differs across item types; `publicationTitle` is
        # correct for journalArticle / preprint / conferencePaper; book
        # types use `bookTitle` instead. Start with publicationTitle and
        # let Zotero ignore unknown fields.
        payload["publicationTitle"] = venue
    return payload


# ── Zotero helpers ────────────────────────────────────────────────────────


def _check_connectivity(zotero_client: ZoteroClient) -> None:
    """Raise ``StageAbortedError`` if we cannot reach Zotero."""
    try:
        zotero_client.items(limit=1)
    except Exception as exc:  # pyzotero raises a variety of types
        raise StageAbortedError(
            "Cannot reach Zotero: local API requires Zotero Desktop to be "
            f"open; web API requires ZOTERO_API_KEY / ZOTERO_LIBRARY_ID. "
            f"Underlying error: {type(exc).__name__}: {exc}"
        ) from exc


def _pick_attach_path(item: Item, staging_folder: Path) -> Path:
    """Return the PDF path to attach: staging copy if Stage 02 ran, else original."""
    staging_path = staging_folder / f"{item.id}.pdf"
    if staging_path.exists():
        return staging_path
    return Path(item.source_path)


def _extract_key(response: dict[str, Any]) -> str | None:
    """Extract the first item_key from a pyzotero create/attach response."""
    success = response.get("success") or {}
    if not isinstance(success, dict) or not success:
        return None
    first = next(iter(success.values()))
    return first if isinstance(first, str) else None


# ── Per-item processing ───────────────────────────────────────────────────


async def _import_one(
    item: Item,
    *,
    staging_folder: Path,
    zotero_client: ZoteroClient,
    openalex_client: OpenAlexClient,
    dry_run: bool,
) -> ImportRow:
    attached_path = _pick_attach_path(item, staging_folder)
    if not attached_path.exists():
        return ImportRow(
            sha256=item.id,
            source_path=item.source_path,
            attached_path=str(attached_path),
            detected_doi=item.detected_doi,
            import_route=None,
            zotero_item_key=None,
            status="failed",
            error=f"attach_path_missing:{attached_path}",
        )

    # ── Route A: DOI present, OpenAlex resolves usable metadata ─────────
    if item.detected_doi:
        try:
            work = await openalex_client.work_by_doi(item.detected_doi)
        except Exception as exc:  # network, retry-exhausted, parse error
            log.warning(
                "stage_03.openalex_error",
                doi=item.detected_doi,
                error=str(exc),
            )
            work = None

        payload = map_openalex_to_zotero(work) if work else None
        if payload is not None:
            doi_value = payload.get("DOI") or item.detected_doi
            assert isinstance(doi_value, str)
            existing_key = (
                None
                if dry_run
                else find_existing_doi(zotero_client, doi_value)
            )
            if existing_key:
                # ADR 014: if the existing Zotero item already carries a
                # PDF attachment, the user already had this paper with
                # its own copy. Do not add a second PDF child; just link
                # our state row to the existing key.
                if dry_run:
                    dedup_status: ImportStatus = "dry_run"
                else:
                    already_has_pdf = existing_has_pdf_attachment(
                        zotero_client, existing_key
                    )
                    if already_has_pdf:
                        log.info(
                            "stage_03.dedup.attach_skipped",
                            existing_key=existing_key,
                            reason="pdf_present",
                        )
                        dedup_status = "deduped"
                    else:
                        try:
                            zotero_client.attachment_simple(
                                [str(attached_path)],
                                parent_key=existing_key,
                            )
                        except Exception as exc:
                            return ImportRow(
                                sha256=item.id,
                                source_path=item.source_path,
                                attached_path=str(attached_path),
                                detected_doi=item.detected_doi,
                                import_route="A",
                                zotero_item_key=existing_key,
                                status="failed",
                                error=(
                                    f"attachment_simple:{type(exc).__name__}:{exc}"
                                ),
                            )
                        log.info(
                            "stage_03.dedup.attach_added",
                            existing_key=existing_key,
                        )
                        dedup_status = "deduped_pdf_added"
                return ImportRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    attached_path=str(attached_path),
                    detected_doi=item.detected_doi,
                    import_route="A",
                    zotero_item_key=existing_key,
                    status=dedup_status,
                    error=None,
                )

            try:
                create_response = (
                    {"success": {"0": "DRYRUN"}}
                    if dry_run
                    else zotero_client.create_items([payload])
                )
            except Exception as exc:
                return ImportRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    attached_path=str(attached_path),
                    detected_doi=item.detected_doi,
                    import_route=None,
                    zotero_item_key=None,
                    status="failed",
                    error=f"create_items:{type(exc).__name__}:{exc}",
                )
            parent_key = _extract_key(create_response)
            if parent_key is None:
                return ImportRow(
                    sha256=item.id,
                    source_path=item.source_path,
                    attached_path=str(attached_path),
                    detected_doi=item.detected_doi,
                    import_route=None,
                    zotero_item_key=None,
                    status="failed",
                    error="create_items_no_success_key",
                )

            if not dry_run:
                try:
                    zotero_client.attachment_simple(
                        [str(attached_path)], parent_key=parent_key
                    )
                except Exception as exc:
                    # Parent was created but attachment failed — still
                    # record the parent so Stage 06 can surface the item
                    # and the user (or a re-run) can retry the attach.
                    return ImportRow(
                        sha256=item.id,
                        source_path=item.source_path,
                        attached_path=str(attached_path),
                        detected_doi=item.detected_doi,
                        import_route="A",
                        zotero_item_key=parent_key,
                        status="failed",
                        error=f"attachment_simple:{type(exc).__name__}:{exc}",
                    )

            return ImportRow(
                sha256=item.id,
                source_path=item.source_path,
                attached_path=str(attached_path),
                detected_doi=item.detected_doi,
                import_route="A",
                zotero_item_key=parent_key,
                status="dry_run" if dry_run else "imported",
                error=None,
            )

    # ── Route C: orphan attachment ───────────────────────────────────────
    try:
        response = (
            {"success": {"0": "DRYRUN"}}
            if dry_run
            else zotero_client.attachment_simple(
                [str(attached_path)], parent_key=None
            )
        )
    except Exception as exc:
        return ImportRow(
            sha256=item.id,
            source_path=item.source_path,
            attached_path=str(attached_path),
            detected_doi=item.detected_doi,
            import_route=None,
            zotero_item_key=None,
            status="failed",
            error=f"attachment_simple:{type(exc).__name__}:{exc}",
        )
    attachment_key = _extract_key(response)
    if attachment_key is None:
        return ImportRow(
            sha256=item.id,
            source_path=item.source_path,
            attached_path=str(attached_path),
            detected_doi=item.detected_doi,
            import_route=None,
            zotero_item_key=None,
            status="failed",
            error="attachment_no_success_key",
        )
    return ImportRow(
        sha256=item.id,
        source_path=item.source_path,
        attached_path=str(attached_path),
        detected_doi=item.detected_doi,
        import_route="C",
        zotero_item_key=attachment_key,
        status="dry_run" if dry_run else "imported",
        error=None,
    )


def _select_eligible(session: Session) -> list[Item]:
    """Items ready for Stage 03: academic + post-Stage-02 + text + not yet imported."""
    stmt = (
        select(Item)
        .where(Item.stage_completed == _PREREQ_STAGE)
        .where(Item.has_text == True)  # noqa: E712 — SQL boolean compare
        .where(Item.classification == "academic")
        .where(Item.zotero_item_key.is_(None))  # type: ignore[union-attr]
    )
    return list(session.exec(stmt))


def _batch(items: list[Item], size: int) -> Iterable[list[Item]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# ── Public entry points ───────────────────────────────────────────────────


def run_import(
    *,
    batch_size: int = 50,
    batch_pause_seconds: float = 30.0,
    dry_run: bool = False,
    settings: Settings | None = None,
    engine: Engine | None = None,
    zotero_client: ZoteroClient | None = None,
    openalex_client: OpenAlexClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: Callable[[], datetime] = _utc_now,
) -> ImportResult:
    """Synchronous entry point; wraps :func:`_run_import_async`."""
    return asyncio.run(
        _run_import_async(
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
            dry_run=dry_run,
            settings=settings,
            engine=engine,
            zotero_client=zotero_client,
            openalex_client=openalex_client,
            sleep=sleep,
            now=now,
        )
    )


async def _run_import_async(
    *,
    batch_size: int,
    batch_pause_seconds: float,
    dry_run: bool,
    settings: Settings | None,
    engine: Engine | None,
    zotero_client: ZoteroClient | None,
    openalex_client: OpenAlexClient | None,
    sleep: Callable[[float], Awaitable[None]],
    now: Callable[[], datetime],
) -> ImportResult:
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

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[ImportRow] = []
    staging_folder = settings.paths.staging_folder

    bind(stage=_STAGE, dry_run=dry_run)
    log.info(
        "stage_started",
        batch_size=batch_size,
        batch_pause_seconds=batch_pause_seconds,
    )

    with Session(engine) as session:
        if not dry_run:
            session.add(run)
            session.flush()

        try:
            _check_connectivity(zotero_client)

            items = _select_eligible(session)
            log.info("stage_03.eligible_items", count=len(items))

            for batch_idx, batch in enumerate(_batch(items, batch_size)):
                if batch_idx > 0 and batch_pause_seconds > 0 and not dry_run:
                    log.info(
                        "stage_03.batch_pause", seconds=batch_pause_seconds
                    )
                    await sleep(batch_pause_seconds)

                for item in batch:
                    row = await _import_one(
                        item,
                        staging_folder=staging_folder,
                        zotero_client=zotero_client,
                        openalex_client=openalex_client,
                        dry_run=dry_run,
                    )
                    rows.append(row)

                    if row.status in (
                        "imported",
                        "deduped",
                        "deduped_pdf_added",
                    ):
                        item.updated_at = now()
                        if not dry_run:
                            item.zotero_item_key = row.zotero_item_key
                            item.import_route = row.import_route
                            item.stage_completed = max(
                                item.stage_completed, _STAGE
                            )
                            item.last_error = None
                        run.items_processed += 1
                    elif row.status == "dry_run":
                        # No state change, do not count as processed.
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

    _success_statuses = ("imported", "deduped", "deduped_pdf_added")
    result = ImportResult(
        run_id=run_id,
        rows=rows,
        csv_path=csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        items_route_a=sum(
            1
            for r in rows
            if r.import_route == "A" and r.status in _success_statuses
        ),
        items_route_c=sum(
            1
            for r in rows
            if r.import_route == "C" and r.status in _success_statuses
        ),
        items_deduped=sum(
            1 for r in rows if r.status in ("deduped", "deduped_pdf_added")
        ),
        items_skipped=sum(
            1 for r in rows if r.status == "skipped_already_imported"
        ),
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        route_a=result.items_route_a,
        route_c=result.items_route_c,
        deduped=result.items_deduped,
        csv=str(csv_path),
    )
    return result


__all__ = [
    "ImportResult",
    "ImportRow",
    "ImportStatus",
    "map_openalex_to_zotero",
    "run_import",
]
