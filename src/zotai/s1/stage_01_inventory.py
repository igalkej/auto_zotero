"""Stage 01 — inventory: walk PDF folders, hash, classify, persist Items.

First persistent step of Subsystem 1 (see ``docs/plan_01_subsystem1.md`` §3).
Every valid PDF below the configured source folders is assigned a stable
SHA-256 identity, passed through the academic / non-academic classifier
(§3.1), and — *only if accepted* — recorded in ``state.db`` with
``stage_completed=1``. Rejected PDFs are listed in
``reports/excluded_report_<ts>.csv`` and never consume OCR or API budget
in downstream stages.

Idempotence is guaranteed by the primary key: ``Item.id = sha256(bytes)``.
Re-runs on the same inputs produce zero inserts. Duplicates (same hash,
new path) are reported in the CSV but never mutate the winner's
``source_path``.

This module exposes two entry points:

- :func:`run_inventory` — synchronous; spins up its own event loop to
  reach the classifier's LLM gate. Used by the CLI and existing tests.
- :func:`_run_inventory_async` — the underlying coroutine; used when the
  caller is already inside an event loop (new async tests, future
  orchestrators).
"""

from __future__ import annotations

import asyncio
import csv
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session

from zotai.api.openai_client import BudgetExceededError, OpenAIClient
from zotai.config import Settings
from zotai.s1.classifier import classify
from zotai.s1.handler import StageAbortedError, stage_item_handler
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir, file_sha256, validate_pdf_magic
from zotai.utils.logging import bind, get_logger
from zotai.utils.pdf import count_pages, detect_doi, extract_text_pages

log = get_logger(__name__)

_STAGE: Final[int] = 1
_MAX_PAGES: Final[int] = 3
_HAS_TEXT_THRESHOLD: Final[int] = 100

_INVENTORY_COLUMNS: Final[tuple[str, ...]] = (
    "source_path",
    "sha256",
    "size_bytes",
    "has_text",
    "detected_doi",
    "classification",
    "needs_review",
    "rejection_reason",
    "status",
    "duplicate_of",
    "last_error",
)

_EXCLUDED_COLUMNS: Final[tuple[str, ...]] = (
    "source_path",
    "sha256",
    "size_bytes",
    "page_count",
    "rejection_reason",
    "classifier_branch",
    "llm_reason",
)

InventoryStatus = Literal[
    "new",
    "duplicate",
    "invalid_magic",
    "error",
    "unchanged",
    "retried",
    "excluded",
]


@dataclass(frozen=True)
class InventoryRow:
    """One row in ``inventory_report_<ts>.csv``."""

    source_path: str
    sha256: str | None
    size_bytes: int
    has_text: bool
    detected_doi: str | None
    classification: str | None
    needs_review: bool
    rejection_reason: str | None
    status: InventoryStatus
    duplicate_of: str | None
    last_error: str | None


@dataclass(frozen=True)
class ExcludedRow:
    """One row in ``excluded_report_<ts>.csv`` — PDFs rejected by the classifier."""

    source_path: str
    sha256: str
    size_bytes: int
    page_count: int
    rejection_reason: str
    classifier_branch: str
    llm_reason: str | None


@dataclass(frozen=True)
class InventoryResult:
    """Aggregate outcome of one ``run_inventory`` call."""

    run_id: int | None
    rows: list[InventoryRow]
    excluded_rows: list[ExcludedRow]
    csv_path: Path
    excluded_csv_path: Path
    items_processed: int
    items_failed: int
    duplicates: int
    invalid: int
    excluded: int
    llm_cost_usd: float


@dataclass(frozen=True)
class _ExtractionOutcome:
    pages_text: list[str]
    page_count: int


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _iter_pdf_paths(folders: Iterable[Path]) -> Iterator[Path]:
    """Yield .pdf files under every folder, deduped and deterministically ordered."""
    seen: set[Path] = set()
    for folder in folders:
        for path in sorted(folder.rglob("*.pdf")):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            yield path


def _csv_path(
    reports_dir: Path, *, prefix: str, dry_run: bool, now: datetime
) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"{prefix}_{timestamp}{suffix}.csv"


def _write_inventory_csv(csv_path: Path, rows: Iterable[InventoryRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_INVENTORY_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source_path": row.source_path,
                    "sha256": row.sha256 or "",
                    "size_bytes": row.size_bytes,
                    "has_text": "true" if row.has_text else "false",
                    "detected_doi": row.detected_doi or "",
                    "classification": row.classification or "",
                    "needs_review": "true" if row.needs_review else "false",
                    "rejection_reason": row.rejection_reason or "",
                    "status": row.status,
                    "duplicate_of": row.duplicate_of or "",
                    "last_error": row.last_error or "",
                }
            )


def _write_excluded_csv(csv_path: Path, rows: Iterable[ExcludedRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_EXCLUDED_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source_path": row.source_path,
                    "sha256": row.sha256,
                    "size_bytes": row.size_bytes,
                    "page_count": row.page_count,
                    "rejection_reason": row.rejection_reason,
                    "classifier_branch": row.classifier_branch,
                    "llm_reason": row.llm_reason or "",
                }
            )


@stage_item_handler(stage=_STAGE)
def _extract(
    item: Item,
    *,
    run: Run,
    path: Path,
    now: Callable[[], datetime],
) -> _ExtractionOutcome:
    """Extract first pages + page count, hydrate ``item`` metadata.

    The stage handler owns error flow: on exception it sets
    ``item.last_error`` and returns ``None`` so the caller can still
    decide what to do with the partially-populated item (the Stage 01
    spec keeps unreadable PDFs as ``academic + needs_review=True`` —
    conservative bias).
    """
    pages = extract_text_pages(path, max_pages=_MAX_PAGES)
    page_count = count_pages(path)
    first_page = pages[0] if pages else ""
    item.has_text = len(first_page) >= _HAS_TEXT_THRESHOLD
    item.detected_doi = detect_doi("\n".join(pages))
    item.updated_at = now()
    return _ExtractionOutcome(pages_text=pages, page_count=page_count)


def run_inventory(
    folders: list[Path],
    dry_run: bool,
    *,
    retry_errors: bool = False,
    skip_llm_gate: bool = False,
    max_cost: float | None = None,
    settings: Settings | None = None,
    engine: Engine | None = None,
    openai_client: OpenAIClient | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> InventoryResult:
    """Synchronous entry point. Wraps :func:`_run_inventory_async`.

    Args:
        folders: Absolute paths to scan recursively.
        dry_run: When True, no DB writes happen and the CSV filenames gain
            a ``_dryrun`` suffix.
        retry_errors: When True, previously-seen items that still carry a
            ``last_error`` are re-extracted (same hash, same bytes, so
            this only helps with transient I/O / pdfplumber failures).
        skip_llm_gate: When True, ambiguous items (the ones that reach
            Branch 3 of the classifier) are kept as academic with
            ``needs_review=True`` without calling OpenAI.
        max_cost: Override ``settings.budgets.max_cost_usd_stage_01`` for
            this call only. Used by the CLI's ``--max-cost`` flag.
        settings: Optional override. Defaults to ``Settings()``.
        engine: Optional SQLAlchemy engine. Defaults to a fresh engine
            bound to ``settings.paths.state_db``.
        openai_client: Optional pre-built client. When None and an API
            key is present, the function builds one with a budget equal
            to ``max_cost`` (when given) or
            ``settings.budgets.max_cost_usd_stage_01``.
        now: Clock callable — overridable for tests.

    Returns:
        An :class:`InventoryResult` summarising counts and CSV paths.

    Raises:
        StageAbortedError: Failure ratio exceeded the handler's threshold,
            *or* the LLM gate tripped the budget ceiling mid-run.
    """
    return asyncio.run(
        _run_inventory_async(
            folders,
            dry_run,
            retry_errors=retry_errors,
            skip_llm_gate=skip_llm_gate,
            max_cost=max_cost,
            settings=settings,
            engine=engine,
            openai_client=openai_client,
            now=now,
        )
    )


async def _run_inventory_async(
    folders: list[Path],
    dry_run: bool,
    *,
    retry_errors: bool,
    skip_llm_gate: bool,
    max_cost: float | None,
    settings: Settings | None,
    engine: Engine | None,
    openai_client: OpenAIClient | None,
    now: Callable[[], datetime],
) -> InventoryResult:
    settings = settings or Settings()
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    if openai_client is None and not skip_llm_gate:
        api_key = settings.openai.api_key.get_secret_value()
        if api_key:
            budget = (
                max_cost
                if max_cost is not None
                else settings.budgets.max_cost_usd_stage_01
            )
            openai_client = OpenAIClient(api_key=api_key, budget_usd=budget)
        else:
            log.info("stage_01.no_openai_key", action="skip_llm_gate")
            skip_llm_gate = True

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[InventoryRow] = []
    excluded_rows: list[ExcludedRow] = []
    seen_in_run: dict[str, str] = {}
    llm_cost_usd = 0.0

    bind(stage=_STAGE, dry_run=dry_run)
    log.info("stage_started", folders=[str(f) for f in folders])

    with Session(engine) as session:
        if not dry_run:
            session.add(run)
            session.flush()

        try:
            for path in _iter_pdf_paths(folders):
                row, excluded_row, cost = await _process_path(
                    path=path,
                    session=session,
                    run=run,
                    seen_in_run=seen_in_run,
                    dry_run=dry_run,
                    retry_errors=retry_errors,
                    skip_llm_gate=skip_llm_gate,
                    openai_client=openai_client,
                    now=now,
                )
                rows.append(row)
                if excluded_row is not None:
                    excluded_rows.append(excluded_row)
                llm_cost_usd += cost
            run.status = "succeeded"
        except StageAbortedError:
            run.status = "aborted"
            raise
        except Exception:
            run.status = "failed"
            raise
        finally:
            run.finished_at = now()
            run.cost_usd = llm_cost_usd
            if not dry_run:
                session.commit()
        # Snapshot the Run state while the session is still open — after
        # exiting the with-block the instance is detached and attribute
        # access triggers a DetachedInstanceError under default
        # expire_on_commit semantics.
        run_id = run.id
        items_processed = run.items_processed
        items_failed = run.items_failed

    reports_folder = ensure_dir(settings.paths.reports_folder)
    csv_path = _csv_path(
        reports_folder, prefix="inventory_report", dry_run=dry_run, now=now()
    )
    excluded_csv_path = _csv_path(
        reports_folder, prefix="excluded_report", dry_run=dry_run, now=now()
    )
    _write_inventory_csv(csv_path, rows)
    _write_excluded_csv(excluded_csv_path, excluded_rows)

    result = InventoryResult(
        run_id=run_id,
        rows=rows,
        excluded_rows=excluded_rows,
        csv_path=csv_path,
        excluded_csv_path=excluded_csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        duplicates=sum(1 for r in rows if r.status == "duplicate"),
        invalid=sum(1 for r in rows if r.status == "invalid_magic"),
        excluded=len(excluded_rows),
        llm_cost_usd=llm_cost_usd,
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        duplicates=result.duplicates,
        invalid=result.invalid,
        excluded=result.excluded,
        cost_usd=result.llm_cost_usd,
        csv=str(csv_path),
        excluded_csv=str(excluded_csv_path),
    )
    return result


async def _process_path(
    *,
    path: Path,
    session: Session,
    run: Run,
    seen_in_run: dict[str, str],
    dry_run: bool,
    retry_errors: bool,
    skip_llm_gate: bool,
    openai_client: OpenAIClient | None,
    now: Callable[[], datetime],
) -> tuple[InventoryRow, ExcludedRow | None, float]:
    size = path.stat().st_size

    if not validate_pdf_magic(path):
        return (
            InventoryRow(
                source_path=str(path),
                sha256=None,
                size_bytes=size,
                has_text=False,
                detected_doi=None,
                classification=None,
                needs_review=False,
                rejection_reason=None,
                status="invalid_magic",
                duplicate_of=None,
                last_error=None,
            ),
            None,
            0.0,
        )

    sha = file_sha256(path)
    existing = session.get(Item, sha)

    if existing is not None:
        if existing.source_path != str(path):
            return (
                InventoryRow(
                    source_path=str(path),
                    sha256=sha,
                    size_bytes=size,
                    has_text=existing.has_text,
                    detected_doi=existing.detected_doi,
                    classification=existing.classification,
                    needs_review=existing.needs_review,
                    rejection_reason=None,
                    status="duplicate",
                    duplicate_of=existing.source_path,
                    last_error=None,
                ),
                None,
                0.0,
            )

        if retry_errors and existing.last_error is not None:
            _extract(existing, run=run, path=path, now=now)
            retry_status: InventoryStatus = (
                "error" if existing.last_error else "retried"
            )
            return (
                InventoryRow(
                    source_path=str(path),
                    sha256=sha,
                    size_bytes=size,
                    has_text=existing.has_text,
                    detected_doi=existing.detected_doi,
                    classification=existing.classification,
                    needs_review=existing.needs_review,
                    rejection_reason=None,
                    status=retry_status,
                    duplicate_of=None,
                    last_error=existing.last_error,
                ),
                None,
                0.0,
            )

        return (
            InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=existing.has_text,
                detected_doi=existing.detected_doi,
                classification=existing.classification,
                needs_review=existing.needs_review,
                rejection_reason=None,
                status="unchanged",
                duplicate_of=None,
                last_error=existing.last_error,
            ),
            None,
            0.0,
        )

    if sha in seen_in_run:
        return (
            InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=False,
                detected_doi=None,
                classification=None,
                needs_review=False,
                rejection_reason=None,
                status="duplicate",
                duplicate_of=seen_in_run[sha],
                last_error=None,
            ),
            None,
            0.0,
        )

    # New PDF: extract, classify, persist (or exclude).
    item = Item(id=sha, source_path=str(path), size_bytes=size)
    extraction = _extract(item, run=run, path=path, now=now)

    if extraction is None:
        # Extraction raised — handler set item.last_error. Conservative
        # bias (plan_01 §3.1 Edge cases): keep as academic + needs_review.
        item.classification = "academic"
        item.needs_review = True
        if not dry_run:
            session.add(item)
        seen_in_run[sha] = str(path)
        return (
            InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=item.has_text,
                detected_doi=item.detected_doi,
                classification=item.classification,
                needs_review=item.needs_review,
                rejection_reason=None,
                status="error",
                duplicate_of=None,
                last_error=item.last_error,
            ),
            None,
            0.0,
        )

    try:
        result, usage = await classify(
            pages_text=extraction.pages_text,
            page_count=extraction.page_count,
            has_text=item.has_text,
            skip_llm_gate=skip_llm_gate,
            openai_client=openai_client,
        )
    except BudgetExceededError as exc:
        log.error("stage_01.budget_exceeded", error=str(exc))
        raise StageAbortedError(str(exc)) from exc

    cost = usage.cost_usd if usage is not None else 0.0

    if result.decision == "reject":
        excluded_row = ExcludedRow(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            page_count=extraction.page_count,
            rejection_reason=result.rejection_reason or "unknown",
            classifier_branch=result.branch,
            llm_reason=result.llm_reason,
        )
        return (
            InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=item.has_text,
                detected_doi=item.detected_doi,
                classification=None,
                needs_review=False,
                rejection_reason=result.rejection_reason,
                status="excluded",
                duplicate_of=None,
                last_error=None,
            ),
            excluded_row,
            cost,
        )

    item.classification = "academic"
    item.needs_review = result.needs_review
    if not dry_run:
        session.add(item)
    seen_in_run[sha] = str(path)

    return (
        InventoryRow(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            has_text=item.has_text,
            detected_doi=item.detected_doi,
            classification=item.classification,
            needs_review=item.needs_review,
            rejection_reason=None,
            status="new",
            duplicate_of=None,
            last_error=item.last_error,
        ),
        None,
        cost,
    )


__all__ = [
    "ExcludedRow",
    "InventoryResult",
    "InventoryRow",
    "InventoryStatus",
    "run_inventory",
]
