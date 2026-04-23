"""Status snapshot of the S1 pipeline (plan_01 §6).

Reads ``state.db`` and renders a single plain-text block summarising:

- Items per ``stage_completed`` bucket (0..6).
- Quarantine / needs-review counts + how many carry a ``last_error``.
- Total cost so far + breakdown by stage from ``Run``.
- Timestamp of the most recent ``Run`` row (any stage, any status).
- Whether ``OPENAI_API_KEY`` / ``ZOTERO_API_KEY`` are configured (no key
  values leak — just a presence flag).

Zero-surprise on an empty DB: returns a well-formed snapshot with
every counter at zero and ``last_run=None``. The command is always
safe to invoke.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.config import Settings
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.logging import get_logger

log = get_logger(__name__)

# Stages the pipeline knows about — kept as a constant so the status
# report is stable when new stages land (each new stage adds a new row).
_STAGES: Final[tuple[tuple[int, str], ...]] = (
    (0, "not started"),
    (1, "inventory"),
    (2, "ocr"),
    (3, "import"),
    (4, "enrich"),
    (5, "tag"),
    (6, "validate"),
)


@dataclass(frozen=True)
class StageCount:
    stage: int
    label: str
    count: int


@dataclass(frozen=True)
class StageCost:
    stage: int
    label: str
    cost_usd: float
    runs: int


@dataclass(frozen=True)
class CredentialsSnapshot:
    openai_configured: bool
    zotero_configured: bool


@dataclass(frozen=True)
class StatusSnapshot:
    """Structured view of the S1 state. Rendered via :func:`format_status`."""

    generated_at: datetime
    total_items: int
    items_by_stage: list[StageCount]
    items_in_quarantine: int
    items_needs_review: int
    items_with_last_error: int
    items_with_zotero_key: int
    items_tagged: int
    cost_total_usd: float
    cost_by_stage: list[StageCost]
    last_run_at: datetime | None
    last_run_stage: int | None
    last_run_status: str | None
    credentials: CredentialsSnapshot
    state_db_path: str
    state_db_exists: bool


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def compute_status(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    now: Iterable[datetime] | None = None,
) -> StatusSnapshot:
    """Build a :class:`StatusSnapshot` from the current ``state.db``.

    ``now`` is a kw-only iterable for tests — yields the reported
    ``generated_at``; defaults to the wall clock.
    """
    settings = settings or Settings()
    state_db_path = str(settings.paths.state_db)
    state_db_exists = settings.paths.state_db.exists()
    if engine is None:
        engine = make_s1_engine(state_db_path)
        init_s1(engine)

    generated_at = _utc_now() if now is None else next(iter(now))

    with Session(engine) as session:
        items = list(session.exec(select(Item)))
        runs = list(session.exec(select(Run)))

    items_by_stage = _items_by_stage(items)
    total_items = len(items)
    items_in_quarantine = sum(1 for it in items if it.in_quarantine)
    items_needs_review = sum(1 for it in items if it.needs_review)
    items_with_last_error = sum(1 for it in items if it.last_error)
    items_with_zotero_key = sum(1 for it in items if it.zotero_item_key)
    items_tagged = sum(1 for it in items if it.tags_json)

    cost_total = sum(r.cost_usd for r in runs)
    cost_by_stage = _costs_by_stage(runs)

    last_run = max(runs, key=lambda r: r.started_at, default=None)
    credentials = CredentialsSnapshot(
        openai_configured=bool(settings.openai.api_key.get_secret_value()),
        zotero_configured=bool(settings.zotero.api_key.get_secret_value()),
    )

    return StatusSnapshot(
        generated_at=generated_at,
        total_items=total_items,
        items_by_stage=items_by_stage,
        items_in_quarantine=items_in_quarantine,
        items_needs_review=items_needs_review,
        items_with_last_error=items_with_last_error,
        items_with_zotero_key=items_with_zotero_key,
        items_tagged=items_tagged,
        cost_total_usd=cost_total,
        cost_by_stage=cost_by_stage,
        last_run_at=last_run.started_at if last_run is not None else None,
        last_run_stage=last_run.stage if last_run is not None else None,
        last_run_status=last_run.status if last_run is not None else None,
        credentials=credentials,
        state_db_path=state_db_path,
        state_db_exists=state_db_exists,
    )


def _items_by_stage(items: list[Item]) -> list[StageCount]:
    counter: dict[int, int] = {stage: 0 for stage, _ in _STAGES}
    for it in items:
        counter[it.stage_completed] = counter.get(it.stage_completed, 0) + 1
    return [
        StageCount(stage=stage, label=label, count=counter.get(stage, 0))
        for stage, label in _STAGES
    ]


def _costs_by_stage(runs: list[Run]) -> list[StageCost]:
    per_stage: dict[int, list[Run]] = {}
    for r in runs:
        per_stage.setdefault(r.stage, []).append(r)
    rows: list[StageCost] = []
    labels = dict(_STAGES)
    for stage, stage_runs in sorted(per_stage.items()):
        rows.append(
            StageCost(
                stage=stage,
                label=labels.get(stage, f"stage_{stage:02d}"),
                cost_usd=sum(r.cost_usd for r in stage_runs),
                runs=len(stage_runs),
            )
        )
    return rows


def format_status(snapshot: StatusSnapshot) -> str:
    """Render a :class:`StatusSnapshot` as plain text for ``zotai s1 status``."""
    lines: list[str] = []
    ts = snapshot.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append(f"zotai s1 status — {ts}")
    lines.append("")
    lines.append(
        f"state.db: {snapshot.state_db_path} "
        f"({'exists' if snapshot.state_db_exists else 'not created yet'})"
    )
    lines.append(
        f"credentials: openai={'yes' if snapshot.credentials.openai_configured else 'no'}"
        f"  zotero={'yes' if snapshot.credentials.zotero_configured else 'no'}"
    )
    lines.append(f"total items: {snapshot.total_items}")
    lines.append(
        f"  in quarantine: {snapshot.items_in_quarantine}"
        f"    needs review: {snapshot.items_needs_review}"
    )
    lines.append(
        f"  with zotero key: {snapshot.items_with_zotero_key}"
        f"    tagged: {snapshot.items_tagged}"
    )
    lines.append(f"  with last_error: {snapshot.items_with_last_error}")
    lines.append("")
    lines.append("items by stage_completed:")
    for row in snapshot.items_by_stage:
        lines.append(f"  {row.stage}  {row.label:<14}  {row.count:>6}")
    lines.append("")
    lines.append("cost by stage:")
    if snapshot.cost_by_stage:
        for cost in snapshot.cost_by_stage:
            lines.append(
                f"  {cost.stage}  {cost.label:<14}  "
                f"${cost.cost_usd:>8.4f}  ({cost.runs} run(s))"
            )
    else:
        lines.append("  (no runs recorded)")
    lines.append(f"total cost: ${snapshot.cost_total_usd:.4f}")
    lines.append("")
    if snapshot.last_run_at is not None:
        ts_last = snapshot.last_run_at.astimezone(UTC).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
        lines.append(
            f"last run: stage {snapshot.last_run_stage:02d} — "
            f"{snapshot.last_run_status} at {ts_last}"
        )
    else:
        lines.append("last run: (none)")
    return "\n".join(lines)


__all__ = [
    "CredentialsSnapshot",
    "StageCost",
    "StageCount",
    "StatusSnapshot",
    "compute_status",
    "format_status",
]
