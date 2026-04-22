"""Stage 02 — OCR: apply Tesseract to scanned PDFs via ``ocrmypdf``.

Runs on items left by Stage 01 with ``has_text=False`` and
``stage_completed=1`` — the PDFs the academic classifier accepted but
that `pdfplumber` could not extract text from. After this stage those
items either have a text layer (``has_text=True``, copy lives at
``staging/<hash>.pdf``) or are flagged ``ocr_failed=True`` with a
``last_error`` message. Either way ``stage_completed`` advances to 2 so
Stage 03 can decide what to do with them.

Cross-cutting rules from plan_01 §3 Etapa 02:

- **Idempotent.** Items already at ``stage_completed >= 2`` are not
  re-queried. Re-running the stage on the same DB is a no-op.
- **Resume-safe.** If ``staging/<hash>.pdf`` already exists *and* has a
  text layer, the worker skips the ``ocrmypdf`` call entirely — an
  earlier partial run already did the expensive work.
- **Originals are never mutated.** All OCR happens on a copy under the
  ``STAGING_FOLDER`` volume; the user's source PDFs stay untouched.
- **Disk-aware.** Before running, the stage checks that the free space
  on the staging volume is at least ``2 * sum(size_bytes)`` of the
  eligible items. If not, it aborts with a clear message *before* any
  copy or OCR happens.
- **CPU-bound parallelism.** OCR is the only stage with meaningful
  internal parallelism (``multiprocessing.Pool`` with
  ``OCR_PARALLEL_PROCESSES`` workers, default 4). SQLite is not
  thread-safe, so workers return plain dataclasses; the main process
  applies them in a single DB transaction at the end.
- **``--force-ocr`` reprocesses.** Default mode passes ``skip_text=True``
  to ``ocrmypdf`` so pages that already carry text are left alone.
  Passing ``force_ocr=True`` re-OCRs everything — useful when a prior
  OCR pass produced junk (plan_01 §3 Etapa 02 Edge cases).
"""

from __future__ import annotations

import csv
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from multiprocessing import Pool
from pathlib import Path
from typing import Final, Literal

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import disk_space_available, ensure_dir
from zotai.utils.logging import bind, get_logger
from zotai.utils.pdf import has_text_layer

log = get_logger(__name__)

_STAGE: Final[int] = 2
_PREREQ_STAGE: Final[int] = 1
_DISK_SAFETY_MULTIPLIER: Final[int] = 2


OcrStatus = Literal[
    "ok",
    "resumed",
    "failed",
    "dry_run",
]

_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "sha256",
    "source_path",
    "staging_path",
    "status",
    "has_text_post",
    "duration_ms",
    "error",
)


@dataclass(frozen=True)
class _WorkUnit:
    """Immutable input to ``_process_one`` — picklable for ``multiprocessing.Pool``."""

    sha256: str
    source_path: str
    staging_path: str
    languages: str
    force_ocr: bool


@dataclass(frozen=True)
class _WorkResult:
    """Immutable output from ``_process_one``. The main process applies it to the DB."""

    sha256: str
    status: OcrStatus
    has_text_post: bool
    duration_ms: int
    error: str | None


@dataclass(frozen=True)
class OcrRow:
    """One row in ``ocr_report_<ts>.csv``."""

    sha256: str
    source_path: str
    staging_path: str
    status: OcrStatus
    has_text_post: bool
    duration_ms: int
    error: str | None


@dataclass(frozen=True)
class OcrResult:
    """Aggregate outcome of one ``run_ocr`` call."""

    run_id: int | None
    rows: list[OcrRow]
    csv_path: Path
    items_processed: int
    items_failed: int
    items_applied: int
    items_resumed: int


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"ocr_report_{timestamp}{suffix}.csv"


def _write_csv(csv_path: Path, rows: Iterable[OcrRow]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sha256": row.sha256,
                    "source_path": row.source_path,
                    "staging_path": row.staging_path,
                    "status": row.status,
                    "has_text_post": "true" if row.has_text_post else "false",
                    "duration_ms": row.duration_ms,
                    "error": row.error or "",
                }
            )


def _process_one(unit: _WorkUnit) -> _WorkResult:
    """Worker: copy source → staging, run ``ocrmypdf.ocr``, verify, return result.

    Kept at module top level so it can be pickled by ``multiprocessing``
    when ``parallel > 1``. Does *not* touch the database. The main
    process applies the returned ``_WorkResult`` in a single transaction.

    ``ocrmypdf`` is imported lazily: the import hooks into ``pdfminer.six``
    (used by ``pdfplumber``) and changes how Type-1 fonts without a
    ``ToUnicode`` map decode back to text. Our hand-crafted fixture PDFs
    rely on the default decoder, so deferring the import keeps Stage 01
    tests (which run before Stage 02 tests) safe.
    """
    import ocrmypdf

    start = time.monotonic()
    staging = Path(unit.staging_path)
    source = Path(unit.source_path)

    # Ensure the staging copy exists. If it's already there from a prior
    # (interrupted) run *and* already carries a text layer, skip the
    # expensive OCR call.
    try:
        if not staging.exists():
            staging.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, staging)
        elif not unit.force_ocr and has_text_layer(staging):
            duration = int((time.monotonic() - start) * 1000)
            return _WorkResult(
                sha256=unit.sha256,
                status="resumed",
                has_text_post=True,
                duration_ms=duration,
                error=None,
            )
    except OSError as exc:
        duration = int((time.monotonic() - start) * 1000)
        return _WorkResult(
            sha256=unit.sha256,
            status="failed",
            has_text_post=False,
            duration_ms=duration,
            error=f"{type(exc).__name__}: {exc}",
        )

    languages = [part.strip() for part in unit.languages.split("+") if part.strip()]
    try:
        if unit.force_ocr:
            ocrmypdf.ocr(
                str(staging),
                str(staging),
                language=languages,
                force_ocr=True,
                progress_bar=False,
            )
        else:
            ocrmypdf.ocr(
                str(staging),
                str(staging),
                language=languages,
                skip_text=True,
                progress_bar=False,
            )
    except Exception as exc:  # OCR failures are per-item, not fatal
        duration = int((time.monotonic() - start) * 1000)
        return _WorkResult(
            sha256=unit.sha256,
            status="failed",
            has_text_post=False,
            duration_ms=duration,
            error=f"{type(exc).__name__}: {exc}",
        )

    post_has_text = has_text_layer(staging)
    duration = int((time.monotonic() - start) * 1000)
    if post_has_text:
        return _WorkResult(
            sha256=unit.sha256,
            status="ok",
            has_text_post=True,
            duration_ms=duration,
            error=None,
        )
    return _WorkResult(
        sha256=unit.sha256,
        status="failed",
        has_text_post=False,
        duration_ms=duration,
        error="no_text_after_ocr",
    )


def _select_eligible(session: Session) -> list[Item]:
    """Items that Stage 01 left without text, still waiting for Stage 02."""
    stmt = (
        select(Item)
        .where(Item.has_text == False)  # noqa: E712 — SQL boolean compare
        .where(Item.stage_completed == _PREREQ_STAGE)
    )
    return list(session.exec(stmt))


def run_ocr(
    *,
    force_ocr: bool = False,
    parallel: int | None = None,
    dry_run: bool = False,
    settings: Settings | None = None,
    engine: Engine | None = None,
    worker: Callable[[_WorkUnit], _WorkResult] = _process_one,
    now: Callable[[], datetime] = _utc_now,
) -> OcrResult:
    """Run Stage 02 over every eligible item in ``state.db``.

    Args:
        force_ocr: When True, ``ocrmypdf`` is called with
            ``force_ocr=True`` (re-OCR pages that already carry text).
            Default mode passes ``skip_text=True`` so pages with text
            survive untouched.
        parallel: Number of workers. ``None`` reads
            ``settings.ocr.parallel_processes``. ``parallel <= 1``
            skips ``multiprocessing.Pool`` and runs the workers
            sequentially in the calling process — useful under tests
            where pytest monkeypatches don't cross process boundaries.
        dry_run: Reports what would be done. No file copies, no OCR
            calls, no DB writes. Writes a ``_dryrun``-suffixed CSV.
        settings: Override for ``Settings()``.
        engine: Override for the default engine bound to
            ``settings.paths.state_db``.
        worker: Worker callable. Tests inject a fake; production uses
            :func:`_process_one`.
        now: Clock — overridable for tests.

    Raises:
        StageAbortedError: Free disk on the staging volume is under
            ``2 * sum(size_bytes)`` of the eligible items. Raised
            *before* any copy or OCR happens.
    """
    settings = settings or Settings()
    effective_parallel = (
        parallel if parallel is not None else settings.ocr.parallel_processes
    )
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[OcrRow] = []

    bind(stage=_STAGE, dry_run=dry_run)
    log.info("stage_started", parallel=effective_parallel, force_ocr=force_ocr)

    with Session(engine) as session:
        if not dry_run:
            session.add(run)
            session.flush()

        try:
            items = _select_eligible(session)
            log.info("stage_02.eligible_items", count=len(items))

            if items:
                staging_dir = ensure_dir(settings.paths.staging_folder)
                required_bytes = (
                    sum(i.size_bytes for i in items) * _DISK_SAFETY_MULTIPLIER
                )
                available_bytes = disk_space_available(staging_dir)
                if available_bytes < required_bytes:
                    raise StageAbortedError(
                        "Insufficient disk space on staging volume: "
                        f"need {required_bytes} bytes (2x corpus size), "
                        f"have {available_bytes}."
                    )

                units = [
                    _WorkUnit(
                        sha256=item.id,
                        source_path=item.source_path,
                        staging_path=str(staging_dir / f"{item.id}.pdf"),
                        languages=settings.ocr.languages,
                        force_ocr=force_ocr,
                    )
                    for item in items
                ]

                if dry_run:
                    results = [
                        _WorkResult(
                            sha256=u.sha256,
                            status="dry_run",
                            has_text_post=False,
                            duration_ms=0,
                            error=None,
                        )
                        for u in units
                    ]
                elif effective_parallel <= 1:
                    results = [worker(u) for u in units]
                else:
                    with Pool(effective_parallel) as pool:
                        results = list(pool.map(worker, units))

                by_id = {item.id: item for item in items}
                for unit, work_result in zip(units, results, strict=True):
                    item = by_id[work_result.sha256]
                    rows.append(
                        OcrRow(
                            sha256=work_result.sha256,
                            source_path=item.source_path,
                            staging_path=unit.staging_path,
                            status=work_result.status,
                            has_text_post=work_result.has_text_post,
                            duration_ms=work_result.duration_ms,
                            error=work_result.error,
                        )
                    )
                    if dry_run or work_result.status == "dry_run":
                        continue

                    item.updated_at = now()
                    if work_result.status in ("ok", "resumed"):
                        item.has_text = True
                        item.ocr_failed = False
                        item.last_error = None
                        item.stage_completed = max(item.stage_completed, _STAGE)
                        run.items_processed += 1
                    elif work_result.status == "failed":
                        # Spec: advance stage_completed to 2 anyway so
                        # Stage 03 sees the item, but leave ocr_failed
                        # set so downstream stages can route it.
                        item.has_text = False
                        item.ocr_failed = True
                        item.last_error = work_result.error
                        item.stage_completed = max(item.stage_completed, _STAGE)
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

        # Snapshot Run state while the session is still open — same
        # DetachedInstanceError workaround as Stage 01.
        run_id = run.id
        items_processed = run.items_processed
        items_failed = run.items_failed

    reports_folder = ensure_dir(settings.paths.reports_folder)
    csv_path = _csv_path(reports_folder, dry_run=dry_run, now=now())
    _write_csv(csv_path, rows)

    result = OcrResult(
        run_id=run_id,
        rows=rows,
        csv_path=csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        items_applied=sum(1 for r in rows if r.status == "ok"),
        items_resumed=sum(1 for r in rows if r.status == "resumed"),
    )
    log.info(
        "stage_finished",
        processed=result.items_processed,
        failed=result.items_failed,
        applied=result.items_applied,
        resumed=result.items_resumed,
        csv=str(csv_path),
    )
    return result


__all__ = [
    "OcrResult",
    "OcrRow",
    "OcrStatus",
    "run_ocr",
]
