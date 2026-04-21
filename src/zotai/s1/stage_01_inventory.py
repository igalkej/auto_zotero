"""Stage 01 — inventory: walk PDF folders, hash, detect DOI, persist Items.

First persistent step of Subsystem 1 (see ``docs/plan_01_subsystem1.md`` §3).
Every valid PDF below the configured source folders is assigned a stable
SHA-256 identity and recorded in ``state.db`` with ``stage_completed=1``.
Later stages read from there.

Idempotence is guaranteed by the primary key: ``Item.id = sha256(bytes)``.
Re-runs on the same inputs produce zero inserts. Duplicates (same hash, new
path) are reported in the CSV but never mutate the winner's ``source_path``.
"""

from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session

from zotai.config import Settings
from zotai.s1.handler import StageAbortedError, stage_item_handler
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir, file_sha256, validate_pdf_magic
from zotai.utils.logging import bind, get_logger
from zotai.utils.pdf import detect_doi, extract_text_pages

log = get_logger(__name__)

_STAGE: Final[int] = 1
_MAX_PAGES: Final[int] = 3
_HAS_TEXT_THRESHOLD: Final[int] = 100
_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "source_path",
    "sha256",
    "size_bytes",
    "has_text",
    "detected_doi",
    "status",
    "duplicate_of",
    "last_error",
)

InventoryStatus = Literal[
    "new", "duplicate", "invalid_magic", "error", "unchanged", "retried"
]


@dataclass(frozen=True)
class InventoryRow:
    """One entry in the inventory CSV (and the return value's row list)."""

    source_path: str
    sha256: str | None
    size_bytes: int
    has_text: bool
    detected_doi: str | None
    status: InventoryStatus
    duplicate_of: str | None
    last_error: str | None


@dataclass(frozen=True)
class InventoryResult:
    """Aggregate outcome of one `run_inventory` call."""

    run_id: int | None
    rows: list[InventoryRow]
    csv_path: Path
    items_processed: int
    items_failed: int
    duplicates: int
    invalid: int


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _iter_pdf_paths(folders: Iterable[Path]) -> Iterator[Path]:
    """Yield .pdf files under every folder, deduped and deterministically ordered."""
    seen: set[Path] = set()
    for folder in folders:
        for path in sorted(folder.rglob("*.pdf")):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            yield path


def _csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"inventory_report_{timestamp}{suffix}.csv"


def _write_csv(csv_path: Path, rows: Iterable[InventoryRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "source_path": row.source_path,
                    "sha256": row.sha256 or "",
                    "size_bytes": row.size_bytes,
                    "has_text": "true" if row.has_text else "false",
                    "detected_doi": row.detected_doi or "",
                    "status": row.status,
                    "duplicate_of": row.duplicate_of or "",
                    "last_error": row.last_error or "",
                }
            )


@stage_item_handler(stage=_STAGE)
def _process_new(
    item: Item,
    *,
    run: Run,
    path: Path,
    now: Callable[[], datetime],
) -> None:
    """Extract text from the first pages, derive ``has_text`` + DOI, stamp ``updated_at``.

    Called only for PDFs we have not seen in the DB. The decorator owns the
    exception flow: on failure it sets ``item.last_error`` and swallows.
    """
    pages = extract_text_pages(path, max_pages=_MAX_PAGES)
    first_page = pages[0] if pages else ""
    item.has_text = len(first_page) >= _HAS_TEXT_THRESHOLD
    item.detected_doi = detect_doi("\n".join(pages))
    item.updated_at = now()


def run_inventory(
    folders: list[Path],
    dry_run: bool,
    *,
    retry_errors: bool = False,
    settings: Settings | None = None,
    engine: Engine | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> InventoryResult:
    """Scan ``folders`` for PDFs and persist ``Item`` rows to ``state.db``.

    Args:
        folders: Absolute paths to folders to recursively scan.
        dry_run: When True, no DB writes happen and the CSV filename gains a
            ``_dryrun`` suffix.
        retry_errors: When True, previously-seen items that still carry a
            ``last_error`` are re-processed (text extraction + DOI detection)
            instead of being reported as ``unchanged``. Hash-based identity
            means the file content is the same, so this only helps when the
            prior failure was transient (I/O glitch, pdfplumber hiccup).
        settings: Optional injected ``Settings``; defaults to ``Settings()``.
        engine: Optional injected SQLAlchemy engine; defaults to a fresh
            engine bound to ``settings.paths.state_db``.
        now: Clock callable — overridable for tests.

    Returns:
        An ``InventoryResult`` summarising counts and pointing at the CSV.

    Raises:
        StageAbortedError: Failure ratio exceeded the handler's threshold.
    """
    settings = settings or Settings()
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[InventoryRow] = []
    seen_in_run: dict[str, str] = {}

    bind(stage=_STAGE, dry_run=dry_run)
    log.info("stage_started", folders=[str(f) for f in folders])

    with Session(engine) as session:
        if not dry_run:
            session.add(run)
            session.flush()

        try:
            for path in _iter_pdf_paths(folders):
                row = _process_path(
                    path=path,
                    session=session,
                    run=run,
                    seen_in_run=seen_in_run,
                    dry_run=dry_run,
                    retry_errors=retry_errors,
                    now=now,
                )
                rows.append(row)
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

    reports_folder = ensure_dir(settings.paths.reports_folder)
    csv_path = _csv_path(reports_folder, dry_run=dry_run, now=now())
    _write_csv(csv_path, rows)

    result = InventoryResult(
        run_id=run.id,
        rows=rows,
        csv_path=csv_path,
        items_processed=run.items_processed,
        items_failed=run.items_failed,
        duplicates=sum(1 for r in rows if r.status == "duplicate"),
        invalid=sum(1 for r in rows if r.status == "invalid_magic"),
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        duplicates=result.duplicates,
        invalid=result.invalid,
        csv=str(csv_path),
    )
    return result


def _process_path(
    *,
    path: Path,
    session: Session,
    run: Run,
    seen_in_run: dict[str, str],
    dry_run: bool,
    retry_errors: bool,
    now: Callable[[], datetime],
) -> InventoryRow:
    size = path.stat().st_size

    if not validate_pdf_magic(path):
        return InventoryRow(
            source_path=str(path),
            sha256=None,
            size_bytes=size,
            has_text=False,
            detected_doi=None,
            status="invalid_magic",
            duplicate_of=None,
            last_error=None,
        )

    sha = file_sha256(path)
    existing = session.get(Item, sha)

    if existing is not None:
        if existing.source_path != str(path):
            return InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=existing.has_text,
                detected_doi=existing.detected_doi,
                status="duplicate",
                duplicate_of=existing.source_path,
                last_error=None,
            )

        if retry_errors and existing.last_error is not None:
            _process_new(existing, run=run, path=path, now=now)
            retry_status: InventoryStatus = (
                "error" if existing.last_error else "retried"
            )
            return InventoryRow(
                source_path=str(path),
                sha256=sha,
                size_bytes=size,
                has_text=existing.has_text,
                detected_doi=existing.detected_doi,
                status=retry_status,
                duplicate_of=None,
                last_error=existing.last_error,
            )

        return InventoryRow(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            has_text=existing.has_text,
            detected_doi=existing.detected_doi,
            status="unchanged",
            duplicate_of=None,
            last_error=existing.last_error,
        )

    if sha in seen_in_run:
        return InventoryRow(
            source_path=str(path),
            sha256=sha,
            size_bytes=size,
            has_text=False,
            detected_doi=None,
            status="duplicate",
            duplicate_of=seen_in_run[sha],
            last_error=None,
        )

    item = Item(id=sha, source_path=str(path), size_bytes=size)
    _process_new(item, run=run, path=path, now=now)

    if not dry_run:
        session.add(item)
    seen_in_run[sha] = str(path)

    status: InventoryStatus = "error" if item.last_error else "new"
    return InventoryRow(
        source_path=str(path),
        sha256=sha,
        size_bytes=size,
        has_text=item.has_text,
        detected_doi=item.detected_doi,
        status=status,
        duplicate_of=None,
        last_error=item.last_error,
    )


__all__ = [
    "InventoryResult",
    "InventoryRow",
    "InventoryStatus",
    "run_inventory",
]
