"""Stage 05 — LLM tagging against the TEMA / METODO taxonomy (plan_01 §3 Etapa 05).

Input: every item with ``stage_completed >= 4 AND in_quarantine=False AND
zotero_item_key IS NOT NULL``. Output: ``Item.tags_json`` persisted and
(in ``--apply`` mode) the tags attached to the Zotero item.

Per-item flow:

1. Build a metadata dict from ``Item.metadata_json`` (the Zotero payload
   written by Stage 03 Route-A or Stage 04 enrichment).
2. Ask ``OpenAIClient.tag_paper`` to return a JSON object of the shape
   ``{"tema": [...], "metodo": [...]}``. Retry once on malformed JSON;
   after the retry, the item is recorded with ``status="llm_failed"``
   and no tags are written.
3. Strict-validate each returned id against the active taxonomy. Unknown
   ids are dropped (not fatal) and surfaced in the CSV so the researcher
   can spot LLM hallucinations or taxonomy gaps.
4. In ``--apply``, call ``ZoteroClient.add_tags`` and persist
   ``Item.tags_json``; advance ``stage_completed`` to 5. In ``--preview``
   (or global ``--dry-run``), the CSV is written but Zotero and the DB
   stay untouched.

Budget: ``MAX_COST_USD_STAGE_05`` (default $1.00 — typical spend on 1000
items at ``~$0.0004/paper`` is ~$0.40). Overridable per-run with
``--max-cost``. ``BudgetExceededError`` aborts the stage cleanly; items
already processed keep their tags.

Taxonomy file sanity gate: ``config/taxonomy.yaml`` carries a
``status: template | customized`` marker. The stage refuses to run
against ``status: template`` unless the caller passes
``--allow-template-taxonomy`` — plan_01 §3.05 forbids accidentally
populating a library with placeholder tags.
"""

from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.api.openai_client import BudgetExceededError, OpenAIClient
from zotai.api.zotero import ZoteroClient
from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.state import Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir
from zotai.utils.logging import bind, get_logger

log = get_logger(__name__)

_STAGE: Final[int] = 5
_PREREQ_STAGE: Final[int] = 4
# Retry the LLM call once if the first response fails JSON / schema
# validation; after that the item is recorded as ``llm_failed`` and
# left without tags.
_LLM_MAX_RETRIES: Final[int] = 1

_CSV_COLUMNS: Final[tuple[str, ...]] = (
    "sha256",
    "zotero_item_key",
    "title",
    "tema_applied",
    "metodo_applied",
    "tema_rejected",
    "metodo_rejected",
    "cost_usd",
    "status",
    "error",
)

TagStatus = Literal[
    "tagged",
    "preview",
    "no_metadata",
    "no_valid_tags",
    "llm_failed",
    "dry_run",
    "skipped_already_tagged",
]


# ─── Taxonomy loading ────────────────────────────────────────────────────


class TaxonomyEntry(BaseModel):
    """One row under ``tema:`` or ``metodo:`` in ``config/taxonomy.yaml``."""

    id: str
    description: str = ""
    synonyms: list[str] = Field(default_factory=list)


class Taxonomy(BaseModel):
    """Parsed ``config/taxonomy.yaml``.

    ``status`` is the sanity gate described in plan_01 §3.05 and in the
    header comment of the yaml file. ``tema`` / ``metodo`` are plain
    lists so the downstream ``OpenAIClient.tag_paper`` helper can read
    ``entry.id`` directly.
    """

    version: int = 1
    status: Literal["template", "customized"] = "template"
    tema: list[TaxonomyEntry] = Field(default_factory=list)
    metodo: list[TaxonomyEntry] = Field(default_factory=list)

    @property
    def tema_ids(self) -> set[str]:
        return {entry.id for entry in self.tema}

    @property
    def metodo_ids(self) -> set[str]:
        return {entry.id for entry in self.metodo}

    def as_payload_dict(self) -> dict[str, list[dict[str, Any]]]:
        """Shape accepted by ``OpenAIClient.tag_paper``."""
        return {
            "tema": [entry.model_dump() for entry in self.tema],
            "metodo": [entry.model_dump() for entry in self.metodo],
        }


def load_taxonomy(path: Path) -> Taxonomy:
    """Parse + validate the taxonomy YAML at ``path``.

    Raises ``StageAbortedError`` on unreadable / invalid files so the
    caller can surface a clear message instead of a bare validation
    traceback.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StageAbortedError(
            f"Taxonomy file not found: {path}. Create it from the "
            "template at config/taxonomy.yaml and set status=customized "
            "before running `zotai s1 tag`."
        ) from exc
    except yaml.YAMLError as exc:
        raise StageAbortedError(f"Taxonomy YAML parse error in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise StageAbortedError(
            f"Taxonomy file {path} must be a mapping at the top level; "
            f"got {type(raw).__name__}."
        )
    try:
        return Taxonomy.model_validate(raw)
    except ValidationError as exc:
        raise StageAbortedError(f"Taxonomy schema error in {path}: {exc}") from exc


# ─── Result types ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TagRow:
    """One row in ``reports/tag_report_<ts>.csv``."""

    sha256: str
    zotero_item_key: str | None
    title: str
    tema_applied: list[str]
    metodo_applied: list[str]
    tema_rejected: list[str]
    metodo_rejected: list[str]
    cost_usd: float
    status: TagStatus
    error: str | None


@dataclass(frozen=True)
class TagResult:
    """Aggregate outcome of one ``run_tag`` call."""

    run_id: int | None
    rows: list[TagRow]
    csv_path: Path
    items_processed: int
    items_failed: int
    items_tagged: int
    items_previewed: int
    items_no_metadata: int
    items_llm_failed: int
    cost_usd: float


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _csv_path(reports_dir: Path, *, dry_run: bool, now: datetime) -> Path:
    suffix = "_dryrun" if dry_run else ""
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return reports_dir / f"tag_report_{timestamp}{suffix}.csv"


def _write_csv(path: Path, rows: Iterable[TagRow]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_CSV_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "sha256": row.sha256,
                    "zotero_item_key": row.zotero_item_key or "",
                    "title": row.title,
                    "tema_applied": "|".join(row.tema_applied),
                    "metodo_applied": "|".join(row.metodo_applied),
                    "tema_rejected": "|".join(row.tema_rejected),
                    "metodo_rejected": "|".join(row.metodo_rejected),
                    "cost_usd": f"{row.cost_usd:.6f}",
                    "status": row.status,
                    "error": row.error or "",
                }
            )


# ─── LLM response parsing ────────────────────────────────────────────────


class _LLMTagResponse(BaseModel):
    tema: list[str] = Field(default_factory=list)
    metodo: list[str] = Field(default_factory=list)


def _parse_llm_response(usage: Any) -> _LLMTagResponse | None:
    """Parse ``UsageRecord.response`` into the tag schema, or ``None`` on failure."""
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
        return _LLMTagResponse.model_validate(raw)
    except ValidationError:
        return None


def _validate_tags(
    parsed: _LLMTagResponse, taxonomy: Taxonomy
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Split the LLM response into accepted + rejected tag ids.

    Returns ``(tema_applied, metodo_applied, tema_rejected, metodo_rejected)``
    in taxonomy-declaration order for determinism, deduped, with
    unknown ids surfaced under the rejected lists so the caller can log
    them.
    """
    tema_ids = taxonomy.tema_ids
    metodo_ids = taxonomy.metodo_ids
    tema_seen: set[str] = set()
    metodo_seen: set[str] = set()
    tema_applied: list[str] = []
    metodo_applied: list[str] = []
    tema_rejected: list[str] = []
    metodo_rejected: list[str] = []
    for raw_tag in parsed.tema:
        if not isinstance(raw_tag, str):
            continue
        tag = raw_tag.strip()
        if not tag or tag in tema_seen:
            continue
        tema_seen.add(tag)
        if tag in tema_ids:
            tema_applied.append(tag)
        else:
            tema_rejected.append(tag)
    for raw_tag in parsed.metodo:
        if not isinstance(raw_tag, str):
            continue
        tag = raw_tag.strip()
        if not tag or tag in metodo_seen:
            continue
        metodo_seen.add(tag)
        if tag in metodo_ids:
            metodo_applied.append(tag)
        else:
            metodo_rejected.append(tag)
    return tema_applied, metodo_applied, tema_rejected, metodo_rejected


# ─── Per-item tagging ────────────────────────────────────────────────────


async def _tag_one(
    item: Item,
    *,
    taxonomy: Taxonomy,
    openai_client: OpenAIClient,
    model: str,
) -> tuple[TagRow, list[str], list[str], float]:
    """Tag a single item. Returns ``(row, tema_applied, metodo_applied, cost)``.

    ``BudgetExceededError`` is not caught — the caller short-circuits the
    whole run when the cap trips, so partial state stays consistent.
    """
    metadata_json = item.metadata_json
    if not metadata_json:
        return (
            TagRow(
                sha256=item.id,
                zotero_item_key=item.zotero_item_key,
                title="",
                tema_applied=[],
                metodo_applied=[],
                tema_rejected=[],
                metodo_rejected=[],
                cost_usd=0.0,
                status="no_metadata",
                error="item.metadata_json is empty",
            ),
            [],
            [],
            0.0,
        )

    try:
        metadata = json.loads(metadata_json)
    except json.JSONDecodeError as exc:
        return (
            TagRow(
                sha256=item.id,
                zotero_item_key=item.zotero_item_key,
                title="",
                tema_applied=[],
                metodo_applied=[],
                tema_rejected=[],
                metodo_rejected=[],
                cost_usd=0.0,
                status="llm_failed",
                error=f"metadata_json_decode:{exc}",
            ),
            [],
            [],
            0.0,
        )
    title = str(metadata.get("title") or "")

    parsed: _LLMTagResponse | None = None
    last_error: str | None = None
    total_cost = 0.0
    for _attempt in range(_LLM_MAX_RETRIES + 1):
        try:
            usage = await openai_client.tag_paper(
                metadata=metadata,
                taxonomy=taxonomy.as_payload_dict(),
                model=model,
            )
        except BudgetExceededError:
            raise
        except Exception as exc:
            last_error = f"openai_error:{type(exc).__name__}:{exc}"
            continue
        total_cost += float(getattr(usage, "cost_usd", 0.0) or 0.0)
        parsed = _parse_llm_response(usage)
        if parsed is not None:
            break
        last_error = "llm_json_invalid"

    if parsed is None:
        return (
            TagRow(
                sha256=item.id,
                zotero_item_key=item.zotero_item_key,
                title=title,
                tema_applied=[],
                metodo_applied=[],
                tema_rejected=[],
                metodo_rejected=[],
                cost_usd=total_cost,
                status="llm_failed",
                error=last_error or "llm_no_response",
            ),
            [],
            [],
            total_cost,
        )

    tema_applied, metodo_applied, tema_rejected, metodo_rejected = _validate_tags(
        parsed, taxonomy
    )
    if not tema_applied and not metodo_applied:
        return (
            TagRow(
                sha256=item.id,
                zotero_item_key=item.zotero_item_key,
                title=title,
                tema_applied=[],
                metodo_applied=[],
                tema_rejected=tema_rejected,
                metodo_rejected=metodo_rejected,
                cost_usd=total_cost,
                status="no_valid_tags",
                error=None,
            ),
            [],
            [],
            total_cost,
        )

    return (
        TagRow(
            sha256=item.id,
            zotero_item_key=item.zotero_item_key,
            title=title,
            tema_applied=tema_applied,
            metodo_applied=metodo_applied,
            tema_rejected=tema_rejected,
            metodo_rejected=metodo_rejected,
            cost_usd=total_cost,
            status="tagged",  # caller adjusts to preview / dry_run later
            error=None,
        ),
        tema_applied,
        metodo_applied,
        total_cost,
    )


# ─── Eligible-items query ────────────────────────────────────────────────


def _select_eligible(session: Session, *, re_tag: bool) -> list[Item]:
    """Items that Stage 05 should process.

    Default: ``stage_completed >= 4 AND !in_quarantine AND
    zotero_item_key IS NOT NULL AND tags_json IS NULL`` (i.e. not yet
    tagged). ``re_tag=True`` drops the ``tags_json IS NULL`` clause so
    the caller can re-tag the corpus after a taxonomy edit.
    """
    stmt = (
        select(Item)
        .where(Item.stage_completed >= _PREREQ_STAGE)
        .where(Item.in_quarantine == False)  # noqa: E712
        .where(Item.zotero_item_key.is_not(None))  # type: ignore[union-attr]
    )
    if not re_tag:
        stmt = stmt.where(Item.tags_json.is_(None))  # type: ignore[union-attr]
    return list(session.exec(stmt))


# ─── Public entry points ─────────────────────────────────────────────────


def run_tag(
    *,
    preview: bool = False,
    apply: bool = False,
    re_tag: bool = False,
    dry_run: bool = False,
    max_cost: float | None = None,
    allow_template_taxonomy: bool = False,
    settings: Settings | None = None,
    engine: Engine | None = None,
    openai_client: OpenAIClient | None = None,
    zotero_client: ZoteroClient | None = None,
    taxonomy_path: Path | None = None,
    now: Callable[[], datetime] = _utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> TagResult:
    """Run Stage 05 tagging.

    Exactly one of ``preview`` / ``apply`` must be True (the CLI enforces
    this; callers passing both get an error). ``dry_run`` short-circuits
    writes regardless of mode, matching the global ``--dry-run`` flag
    semantics used by the other S1 stages.

    ``allow_template_taxonomy=True`` bypasses the ``status: template``
    refusal — intended for tests and deliberate testing runs; never set
    it by default.
    """
    if preview == apply:
        raise StageAbortedError(
            "Pass exactly one of --preview or --apply. Preview writes the "
            "CSV only; apply also patches Zotero and advances the pipeline."
        )
    return asyncio.run(
        _run_tag_async(
            preview=preview,
            apply=apply,
            re_tag=re_tag,
            dry_run=dry_run,
            max_cost=max_cost,
            allow_template_taxonomy=allow_template_taxonomy,
            settings=settings,
            engine=engine,
            openai_client=openai_client,
            zotero_client=zotero_client,
            taxonomy_path=taxonomy_path,
            now=now,
            sleep=sleep,
        )
    )


async def _run_tag_async(
    *,
    preview: bool,
    apply: bool,
    re_tag: bool,
    dry_run: bool,
    max_cost: float | None,
    allow_template_taxonomy: bool,
    settings: Settings | None,
    engine: Engine | None,
    openai_client: OpenAIClient | None,
    zotero_client: ZoteroClient | None,
    taxonomy_path: Path | None,
    now: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> TagResult:
    _ = sleep  # reserved for rate-pacing when we move to async batches
    settings = settings or Settings()
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    # Taxonomy first — fail early on a bad config.
    if taxonomy_path is None:
        taxonomy_path = _default_taxonomy_path()
    taxonomy = load_taxonomy(taxonomy_path)
    if taxonomy.status != "customized" and not allow_template_taxonomy:
        raise StageAbortedError(
            f"Taxonomy at {taxonomy_path} is marked status=template. "
            "Customize it per docs/plan_taxonomy.md §8 and set "
            "status=customized, or pass --allow-template-taxonomy for "
            "a deliberate testing run. Applying template tags to a real "
            "library corrupts the researcher's classification."
        )

    if openai_client is None:
        api_key = settings.openai.api_key.get_secret_value()
        if not api_key:
            raise StageAbortedError(
                "Stage 05 requires OPENAI_API_KEY. Set it in .env or pass "
                "openai_client explicitly."
            )
        budget = (
            max_cost
            if max_cost is not None
            else settings.budgets.max_cost_usd_stage_05
        )
        openai_client = OpenAIClient(api_key=api_key, budget_usd=budget)

    write_to_zotero = apply and not dry_run
    if zotero_client is None:
        zotero_client = ZoteroClient(
            library_id=settings.zotero.library_id,
            library_type=settings.zotero.library_type,
            api_key=settings.zotero.api_key.get_secret_value(),
            local=settings.zotero.local_api,
            local_api_host=settings.zotero.local_api_host or None,
            dry_run=not write_to_zotero,
        )

    model = settings.openai.model_tag
    run = Run(stage=_STAGE, status="running", started_at=now())
    rows: list[TagRow] = []
    total_cost = 0.0
    stopped_early = False

    bind(stage=_STAGE, dry_run=dry_run, mode="apply" if apply else "preview")
    log.info(
        "stage_started",
        mode="apply" if apply else "preview",
        dry_run=dry_run,
        re_tag=re_tag,
    )

    with Session(engine) as session:
        if write_to_zotero:
            session.add(run)
            session.flush()

        try:
            items = _select_eligible(session, re_tag=re_tag)
            log.info("stage_05.eligible_items", count=len(items))

            for item in items:
                try:
                    core_row, tema_applied, metodo_applied, cost = await _tag_one(
                        item,
                        taxonomy=taxonomy,
                        openai_client=openai_client,
                        model=model,
                    )
                except BudgetExceededError as exc:
                    log.warning("stage_05.budget_exceeded", error=str(exc))
                    stopped_early = True
                    break
                total_cost += cost

                if core_row.status == "no_metadata":
                    rows.append(core_row)
                    continue
                if core_row.status in ("llm_failed", "no_valid_tags"):
                    rows.append(core_row)
                    if write_to_zotero:
                        item.last_error = core_row.error or core_row.status
                        item.updated_at = now()
                    run.items_failed += 1
                    continue

                # Tagged successfully from the LLM's perspective.
                final_status: TagStatus
                if dry_run:
                    final_status = "dry_run"
                elif preview:
                    final_status = "preview"
                else:
                    final_status = "tagged"
                final_row = TagRow(
                    sha256=core_row.sha256,
                    zotero_item_key=core_row.zotero_item_key,
                    title=core_row.title,
                    tema_applied=tema_applied,
                    metodo_applied=metodo_applied,
                    tema_rejected=core_row.tema_rejected,
                    metodo_rejected=core_row.metodo_rejected,
                    cost_usd=core_row.cost_usd,
                    status=final_status,
                    error=None,
                )
                rows.append(final_row)

                if write_to_zotero and item.zotero_item_key:
                    try:
                        zotero_client.add_tags(
                            {"key": item.zotero_item_key},
                            tema_applied + metodo_applied,
                        )
                    except Exception as exc:
                        log.warning(
                            "stage_05.add_tags_failed",
                            sha256=item.id,
                            error=str(exc),
                        )
                        final_row = TagRow(
                            sha256=final_row.sha256,
                            zotero_item_key=final_row.zotero_item_key,
                            title=final_row.title,
                            tema_applied=final_row.tema_applied,
                            metodo_applied=final_row.metodo_applied,
                            tema_rejected=final_row.tema_rejected,
                            metodo_rejected=final_row.metodo_rejected,
                            cost_usd=final_row.cost_usd,
                            status="llm_failed",
                            error=f"add_tags:{type(exc).__name__}:{exc}",
                        )
                        rows[-1] = final_row
                        run.items_failed += 1
                        continue
                    item.tags_json = json.dumps(
                        {"tema": tema_applied, "metodo": metodo_applied}
                    )
                    item.stage_completed = max(item.stage_completed, _STAGE)
                    item.last_error = None
                    item.updated_at = now()
                run.items_processed += 1

            if stopped_early:
                run.status = "aborted"
            else:
                run.status = "succeeded"
        except StageAbortedError:
            run.status = "aborted"
            raise
        except Exception:
            run.status = "failed"
            raise
        finally:
            run.finished_at = now()
            run.cost_usd = total_cost
            if write_to_zotero:
                session.commit()

        run_id = run.id
        items_processed = run.items_processed
        items_failed = run.items_failed

    reports_folder = ensure_dir(settings.paths.reports_folder)
    csv_path = _csv_path(reports_folder, dry_run=dry_run or preview, now=now())
    _write_csv(csv_path, rows)

    result = TagResult(
        run_id=run_id,
        rows=rows,
        csv_path=csv_path,
        items_processed=items_processed,
        items_failed=items_failed,
        items_tagged=sum(1 for r in rows if r.status == "tagged"),
        items_previewed=sum(1 for r in rows if r.status == "preview"),
        items_no_metadata=sum(1 for r in rows if r.status == "no_metadata"),
        items_llm_failed=sum(1 for r in rows if r.status == "llm_failed"),
        cost_usd=total_cost,
    )
    log.info(
        "stage_finished",
        tagged=result.items_tagged,
        previewed=result.items_previewed,
        no_metadata=result.items_no_metadata,
        llm_failed=result.items_llm_failed,
        cost_usd=round(result.cost_usd, 6),
        csv=str(csv_path),
        stopped_early=stopped_early,
    )
    if stopped_early:
        raise StageAbortedError(
            "Stage 05 aborted: LLM budget exceeded. Partial tags have "
            "been written; re-run with --max-cost to bump the cap and "
            "pick up where we left off."
        )
    return result


def _default_taxonomy_path() -> Path:
    """Resolve ``config/taxonomy.yaml`` relative to the working directory.

    The Docker layout mounts ``./config`` at ``/app/config``; local dev
    runs from the repo root where ``config/taxonomy.yaml`` exists too.
    """
    container_path = Path("/app/config/taxonomy.yaml")
    if container_path.exists():
        return container_path
    return Path("config/taxonomy.yaml").resolve()


__all__ = [
    "TagResult",
    "TagRow",
    "TagStatus",
    "Taxonomy",
    "TaxonomyEntry",
    "load_taxonomy",
    "run_tag",
]
