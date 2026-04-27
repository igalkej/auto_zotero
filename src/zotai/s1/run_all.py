"""End-to-end S1 orchestrator (plan_01 §6).

``run_all`` walks Stages 01 → 06 in sequence, printing a summary after
each stage and pausing for Y/n confirmation before the next one (unless
``--yes`` is passed). Each stage is invoked through its public
``run_*`` function — we don't re-implement their logic, we just
compose them.

Behaviour:

- **Default mode: ``tag_mode="apply"``.** Stage 05 writes tags to
  Zotero. Switch to ``"preview"`` when the researcher wants to review
  the tag CSV before committing; ``run_all`` then stops *after* Stage
  05 because subsequent stages have nothing to add beyond what preview
  already produced.
- **``KeyboardInterrupt``** between stages is caught and converted to
  an orderly exit: whatever state the last fully-completed stage
  committed is durable, and the message tells the user what the next
  stage would have been.
- **Stage aborts** (``StageAbortedError``) propagate to the caller
  with a cleanly-framed message — ``run_all`` does not retry.
- **Costs and counts** are accumulated in a ``RunAllResult`` so the
  caller can print a final summary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.s1.stage_01_inventory import InventoryResult, run_inventory
from zotai.s1.stage_02_ocr import OcrResult, run_ocr
from zotai.s1.stage_03_import import ImportResult, run_import
from zotai.s1.stage_04_enrich import EnrichResult, run_enrich
from zotai.s1.stage_05_tag import TagResult, run_tag
from zotai.s1.stage_06_validate import ValidationReport, run_validate
from zotai.utils.logging import bind, get_logger

log = get_logger(__name__)

TagMode = Literal["apply", "preview"]


@dataclass
class StageOutcome:
    """One line in ``RunAllResult.stages`` — result of a single stage."""

    stage: int
    name: str
    summary: str
    succeeded: bool
    skipped: bool = False


@dataclass
class RunAllResult:
    """Aggregate of ``run_all``."""

    stages: list[StageOutcome] = field(default_factory=list)
    inventory: InventoryResult | None = None
    ocr: OcrResult | None = None
    import_: ImportResult | None = None
    enrich: EnrichResult | None = None
    tag: TagResult | None = None
    validation: ValidationReport | None = None
    total_cost_usd: float = 0.0
    completed: bool = False
    stopped_at_stage: int | None = None
    stopped_reason: str | None = None


# ─── Prompting ───────────────────────────────────────────────────────────

ConfirmFn = Callable[[str], bool]


def _default_confirm(question: str) -> bool:
    """Default Y/n reader. Empty answer = yes (plan_01 §6)."""
    # Imported locally so tests that never touch real stdin don't fail on
    # `input()` under pytest capture.
    resp = input(f"{question} [Y/n] ").strip().lower()
    return resp in ("", "y", "yes", "s", "si", "sí")


# ─── Orchestrator ────────────────────────────────────────────────────────


def run_all(
    *,
    yes: bool = False,
    dry_run: bool = False,
    tag_mode: TagMode = "apply",
    allow_template_taxonomy: bool = False,
    settings: Settings | None = None,
    confirm: ConfirmFn | None = None,
    echo: Callable[[str], None] = print,
) -> RunAllResult:
    """Run the S1 pipeline end-to-end.

    ``confirm`` lets tests pin the Y/n answers deterministically;
    production passes the default stdin reader.
    """
    settings = settings or Settings()
    confirm = confirm or _default_confirm
    result = RunAllResult()

    bind(stage=0, mode="run_all", dry_run=dry_run, tag_mode=tag_mode, yes=yes)
    log.info("run_all.started")

    def _check(outcome: StageOutcome) -> bool:
        """Record a stage + ask whether to continue; return True to proceed."""
        result.stages.append(outcome)
        echo(f"[{outcome.stage:02d}/06] {outcome.name}: {outcome.summary}")
        if not outcome.succeeded:
            result.stopped_at_stage = outcome.stage
            result.stopped_reason = outcome.summary
            return False
        if yes:
            return True
        proceed = confirm("Continue to next stage?")
        if not proceed:
            result.stopped_at_stage = outcome.stage
            result.stopped_reason = "user declined to continue"
        return proceed

    try:
        # ── 01: Inventory ───────────────────────────────────────────
        folders = settings.paths.pdf_source_folders
        if not folders:
            raise StageAbortedError(
                "PDF_SOURCE_FOLDERS is empty. Configure it in .env or "
                "run stage 01 directly with --folder PATH."
            )
        inv = run_inventory(folders, dry_run=dry_run, settings=settings)
        result.inventory = inv
        ok = _check(
            StageOutcome(
                stage=1,
                name="inventory",
                summary=(
                    f"processed={inv.items_processed} duplicates={inv.duplicates} "
                    f"invalid={inv.invalid} excluded={inv.excluded} "
                    f"cost=${inv.llm_cost_usd:.4f}"
                ),
                succeeded=True,
            )
        )
        result.total_cost_usd += inv.llm_cost_usd
        if not ok:
            return result

        # ── 02: OCR ─────────────────────────────────────────────────
        ocr = run_ocr(dry_run=dry_run, settings=settings)
        result.ocr = ocr
        ok = _check(
            StageOutcome(
                stage=2,
                name="ocr",
                summary=(
                    f"processed={ocr.items_processed} failed={ocr.items_failed} "
                    f"applied={ocr.items_applied} resumed={ocr.items_resumed}"
                ),
                succeeded=True,
            )
        )
        if not ok:
            return result

        # ── 03: Import ──────────────────────────────────────────────
        imp = run_import(dry_run=dry_run, settings=settings)
        result.import_ = imp
        ok = _check(
            StageOutcome(
                stage=3,
                name="import",
                summary=(
                    f"processed={imp.items_processed} failed={imp.items_failed} "
                    f"route_a={imp.items_route_a} route_c={imp.items_route_c} "
                    f"deduped={imp.items_deduped}"
                ),
                succeeded=True,
            )
        )
        if not ok:
            return result

        # ── 04: Enrich (cascade) ────────────────────────────────────
        enr = run_enrich(substage="all", dry_run=dry_run, settings=settings)
        result.enrich = enr
        ok = _check(
            StageOutcome(
                stage=4,
                name="enrich",
                summary=(
                    f"processed={enr.items_processed} failed={enr.items_failed} "
                    f"04a={enr.items_enriched_04a} 04b={enr.items_enriched_04b} "
                    f"04bs={enr.items_enriched_04bs} 04bd={enr.items_enriched_04bd} "
                    f"04c={enr.items_enriched_04c} 04d={enr.items_enriched_04d} "
                    f"quarantined={enr.items_quarantined}"
                ),
                succeeded=True,
            )
        )
        if not ok:
            return result

        # ── 05: Tag ─────────────────────────────────────────────────
        tag = run_tag(
            preview=(tag_mode == "preview"),
            apply=(tag_mode == "apply"),
            dry_run=dry_run,
            allow_template_taxonomy=allow_template_taxonomy,
            settings=settings,
        )
        result.tag = tag
        result.total_cost_usd += tag.cost_usd
        ok = _check(
            StageOutcome(
                stage=5,
                name="tag",
                summary=(
                    f"processed={tag.items_processed} failed={tag.items_failed} "
                    f"tagged={tag.items_tagged} previewed={tag.items_previewed} "
                    f"llm_failed={tag.items_llm_failed} cost=${tag.cost_usd:.4f}"
                ),
                succeeded=True,
            )
        )
        if not ok:
            return result
        if tag_mode == "preview":
            # Preview mode stops before validate — Stage 06 would run
            # fine but its headline number ("% tagged") is misleading
            # until the tags are actually applied.
            result.stages.append(
                StageOutcome(
                    stage=6,
                    name="validate",
                    summary="skipped (tag_mode=preview)",
                    succeeded=True,
                    skipped=True,
                )
            )
            result.completed = True
            return result

        # ── 06: Validate ────────────────────────────────────────────
        report = run_validate(settings=settings)
        result.validation = report
        result.stages.append(
            StageOutcome(
                stage=6,
                name="validate",
                summary=(
                    f"items={report.completeness.total_items} "
                    f"main={report.completeness.items_in_main} "
                    f"quarantine={report.completeness.items_in_quarantine} "
                    f"issues={len(report.consistency_issues)} "
                    f"duplicates={len(report.duplicate_pairs)}"
                ),
                succeeded=True,
            )
        )
        echo(f"[06/06] validate: {result.stages[-1].summary}")
        result.completed = True
        return result

    except KeyboardInterrupt:
        next_stage = len(result.stages) + 1
        result.stopped_at_stage = next_stage
        result.stopped_reason = "keyboard_interrupt"
        echo(
            f"\n⚠️  Interrupted before stage {next_stage:02d}. Finished stages "
            "are committed in state.db; re-run `zotai s1 run-all` (or the "
            "specific stage) to resume."
        )
        return result
    except StageAbortedError as exc:
        next_stage = len(result.stages) + 1
        result.stopped_at_stage = next_stage
        result.stopped_reason = f"aborted:{exc}"
        echo(f"⚠️  Stage {next_stage:02d} aborted: {exc}")
        return result
    finally:
        log.info(
            "run_all.finished",
            completed=result.completed,
            stopped_at=result.stopped_at_stage,
            reason=result.stopped_reason,
            total_cost_usd=round(result.total_cost_usd, 6),
        )


def format_summary(result: RunAllResult) -> str:
    """Render a ``RunAllResult`` as a plain-text final summary."""
    lines: list[str] = []
    lines.append("")
    lines.append("─── run-all summary ───────────────────────────────────────")
    for s in result.stages:
        marker = "skipped" if s.skipped else ("ok" if s.succeeded else "fail")
        lines.append(f"  [{s.stage:02d}/06] {s.name:<10} {marker:<7} {s.summary}")
    lines.append("")
    lines.append(f"total cost: ${result.total_cost_usd:.4f}")
    if result.completed:
        lines.append("status: completed ✓")
    else:
        lines.append(
            f"status: stopped at stage {result.stopped_at_stage:02d} — "
            f"{result.stopped_reason}"
            if result.stopped_at_stage is not None
            else "status: stopped (no stage recorded)"
        )
    return "\n".join(lines)


__all__ = [
    "ConfirmFn",
    "RunAllResult",
    "StageOutcome",
    "TagMode",
    "format_summary",
    "run_all",
]


# Avoid unused-import warnings when the module is loaded for tests.
_ = Any
