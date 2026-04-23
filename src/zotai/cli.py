"""Typer CLI entry point.

Phase 1 wires the command tree and the global `--dry-run` / `--verbose`
options. Every subcommand is a stub that prints a clear "not yet
implemented in Phase N (#issue)" and exits 1 — real implementations land
with each Phase's PR.

The `zotai` console script is declared in `pyproject.toml`:
`zotai = "zotai.cli:app"`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from zotai.utils.logging import bind, configure_logging

app = typer.Typer(
    help="Zotero AI toolkit — retroactive import, prospective capture, MCP access.",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)

s1_app = typer.Typer(
    help="Subsystem 1 — retroactive capture pipeline (one-shot).",
    no_args_is_help=True,
)
s2_app = typer.Typer(
    help="Subsystem 2 — prospective capture worker + dashboard.",
    no_args_is_help=True,
)

app.add_typer(s1_app, name="s1")
app.add_typer(s2_app, name="s2")


@app.callback()
def _root(
    ctx: typer.Context,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Report what would happen without modifying Zotero or the DB.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Emit DEBUG-level logs.",
        ),
    ] = False,
) -> None:
    """Configure logging and stash global flags in the Typer context."""
    configure_logging(level="DEBUG" if verbose else "INFO")
    bind(dry_run=dry_run)
    ctx.ensure_object(dict)
    ctx.obj["dry_run"] = dry_run
    ctx.obj["verbose"] = verbose


def _not_implemented(stage: str, phase: int, issue: int) -> None:
    typer.secho(
        f"`{stage}` is not yet implemented — scheduled for Phase {phase} "
        f"(#{issue}). Any flags passed are parsed but ignored until then.",
        err=True,
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=2)


# ─── S1 commands ──────────────────────────────────────────────────────────


@s1_app.command("inventory")
def s1_inventory(
    ctx: typer.Context,
    folder: Annotated[
        list[Path] | None,
        typer.Option(
            "--folder",
            exists=True,
            file_okay=False,
            dir_okay=True,
            resolve_path=True,
            help="Source folder(s) to scan. Repeat for multiple; falls back to PDF_SOURCE_FOLDERS.",
        ),
    ] = None,
    retry_errors: Annotated[
        bool,
        typer.Option(
            "--retry-errors",
            help=(
                "Re-run extraction on previously-seen items that still carry "
                "a last_error (useful after a transient I/O or pdfplumber "
                "failure — same hash, same file content)."
            ),
        ),
    ] = False,
    skip_llm_gate: Annotated[
        bool,
        typer.Option(
            "--skip-llm-gate",
            help=(
                "Skip Branch 3 of the classifier (LLM gate). Ambiguous PDFs "
                "are kept as academic with needs_review=True without calling "
                "OpenAI — useful when OPENAI_API_KEY is absent."
            ),
        ),
    ] = False,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            help=(
                "Override MAX_COST_USD_STAGE_01 for this invocation. Hard "
                "cap on the LLM gate's cumulative spend; the stage aborts "
                "when exceeded."
            ),
        ),
    ] = None,
) -> None:
    """Stage 01 — scan PDFs, classify academic vs. non-academic, persist."""
    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_01_inventory import run_inventory

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run
    folders = folder or settings.paths.pdf_source_folders
    if not folders:
        typer.secho(
            "No source folders — pass --folder or set PDF_SOURCE_FOLDERS.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    try:
        result = run_inventory(
            folders,
            dry_run=dry_run,
            retry_errors=retry_errors,
            skip_llm_gate=skip_llm_gate,
            max_cost=max_cost,
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"duplicates={result.duplicates} invalid={result.invalid} "
        f"excluded={result.excluded} cost=${result.llm_cost_usd:.4f} "
        f"csv={result.csv_path}"
    )


@s1_app.command("ocr")
def s1_ocr(
    ctx: typer.Context,
    force_ocr: Annotated[
        bool,
        typer.Option(
            "--force-ocr",
            help=(
                "Re-OCR pages that already carry text. Default mode passes "
                "skip_text=True to ocrmypdf so existing text layers survive."
            ),
        ),
    ] = False,
    parallel: Annotated[
        int | None,
        typer.Option(
            "--parallel",
            help=(
                "Number of ocrmypdf workers. Defaults to "
                "OCR_PARALLEL_PROCESSES; pass 1 to run sequentially."
            ),
        ),
    ] = None,
) -> None:
    """Stage 02 — OCR scanned PDFs into the staging volume."""
    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_02_ocr import run_ocr

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run

    try:
        result = run_ocr(
            force_ocr=force_ocr,
            parallel=parallel,
            dry_run=dry_run,
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"applied={result.items_applied} resumed={result.items_resumed} "
        f"csv={result.csv_path}"
    )


@s1_app.command("import")
def s1_import(
    ctx: typer.Context,
    batch_size: Annotated[
        int,
        typer.Option(
            "--batch-size",
            help="Items per batch before sleeping. Default 50.",
        ),
    ] = 50,
    batch_pause_seconds: Annotated[
        float,
        typer.Option(
            "--batch-pause-seconds",
            help=(
                "Seconds to sleep between batches. Default 30 — gives the "
                "Zotero desktop sync breathing room. Set to 0 to disable."
            ),
        ),
    ] = 30.0,
) -> None:
    """Stage 03 — import PDFs into Zotero (Route A/C)."""
    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_03_import import run_import

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run

    try:
        result = run_import(
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
            dry_run=dry_run,
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"route_a={result.items_route_a} route_c={result.items_route_c} "
        f"deduped={result.items_deduped} csv={result.csv_path}"
    )


@s1_app.command("enrich")
def s1_enrich(
    ctx: typer.Context,
    substage: Annotated[
        str,
        typer.Option(
            "--substage",
            help=(
                "Which enrichment substage to run. '04a' (identifier "
                "extraction), '04b' (OpenAlex fuzzy), '04c' (Semantic "
                "Scholar fuzzy), '04d' (gpt-4o-mini extraction — costs "
                "money, bounded by MAX_COST_USD_STAGE_04), '04e' "
                "(Quarantine), or 'all' for the full per-item cascade "
                "04a → 04b → 04c → 04d → 04e. See plan_01 §3 Etapa 04."
            ),
        ),
    ] = "04a",
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            help=(
                "Override MAX_COST_USD_STAGE_04 for this invocation "
                "(hard cap on 04d's LLM spend). Once the budget trips, "
                "'all' routes remaining items directly to 04e."
            ),
        ),
    ] = None,
) -> None:
    """Stage 04 — enrichment cascade (04a-04e + 'all' orchestrator)."""
    from typing import cast

    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_04_enrich import EnrichSubstage, run_enrich

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run

    if substage not in ("04a", "04b", "04c", "04d", "04e", "all"):
        typer.secho(
            f"Substage '{substage}' is not valid. Choose one of: "
            "'04a' | '04b' | '04c' | '04d' | '04e' | 'all'.",
            err=True,
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=2)

    try:
        result = run_enrich(
            substage=cast(EnrichSubstage, substage),
            dry_run=dry_run,
            max_cost=max_cost,
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    quarantine_part = (
        f" quarantine_csv={result.quarantine_csv_path}"
        if result.quarantine_csv_path
        else ""
    )
    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"enriched_04a={result.items_enriched_04a} "
        f"enriched_04b={result.items_enriched_04b} "
        f"enriched_04c={result.items_enriched_04c} "
        f"enriched_04d={result.items_enriched_04d} "
        f"quarantined={result.items_quarantined} "
        f"no_progress={result.items_no_progress} "
        f"skipped_generic_title={result.items_skipped_generic_title} "
        f"csv={result.csv_path}{quarantine_part}"
    )


@s1_app.command("tag")
def s1_tag(
    ctx: typer.Context,
    preview: Annotated[
        bool,
        typer.Option(
            "--preview",
            help="Write tag proposals to reports/tag_report_<ts>.csv without touching Zotero.",
        ),
    ] = False,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Apply the tags to Zotero and advance stage_completed to 5.",
        ),
    ] = False,
    re_tag: Annotated[
        bool,
        typer.Option(
            "--re-tag",
            help="Re-tag items that already have tags (default skips them).",
        ),
    ] = False,
    max_cost: Annotated[
        float | None,
        typer.Option(
            "--max-cost",
            help="Override MAX_COST_USD_STAGE_05 for this invocation.",
        ),
    ] = None,
    allow_template_taxonomy: Annotated[
        bool,
        typer.Option(
            "--allow-template-taxonomy",
            help=(
                "Proceed even when config/taxonomy.yaml is marked "
                "status=template. Useful for integration tests on the "
                "default taxonomy before the researcher customizes it; "
                "do NOT set on a real run."
            ),
        ),
    ] = False,
) -> None:
    """Stage 05 — LLM tagging against the TEMA/METODO taxonomy."""
    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_05_tag import run_tag

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run

    if preview == apply:
        typer.secho(
            "Pass exactly one of --preview or --apply.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    try:
        result = run_tag(
            preview=preview,
            apply=apply,
            re_tag=re_tag,
            dry_run=dry_run,
            max_cost=max_cost,
            allow_template_taxonomy=allow_template_taxonomy,
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"tagged={result.items_tagged} previewed={result.items_previewed} "
        f"no_metadata={result.items_no_metadata} "
        f"llm_failed={result.items_llm_failed} "
        f"cost=${result.cost_usd:.4f} csv={result.csv_path}"
    )


@s1_app.command("validate")
def s1_validate(
    open_report: Annotated[
        bool,
        typer.Option(
            "--open-report",
            help="After writing the report, open the HTML in the default browser.",
        ),
    ] = False,
) -> None:
    """Stage 06 — generate validation report (HTML + CSV)."""
    import webbrowser

    from zotai.config import Settings
    from zotai.s1.handler import StageAbortedError
    from zotai.s1.stage_06_validate import run_validate

    settings = Settings()

    try:
        report = run_validate(settings=settings)
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    assert report.html_path is not None and report.csv_path is not None
    typer.echo(
        f"items={report.completeness.total_items} "
        f"main={report.completeness.items_in_main} "
        f"quarantine={report.completeness.items_in_quarantine} "
        f"tagged={report.tag_distribution.items_tagged} "
        f"issues={len(report.consistency_issues)} "
        f"duplicates={len(report.duplicate_pairs)} "
        f"cost=${report.cost_total_usd:.4f} "
        f"html={report.html_path} csv={report.csv_path}"
    )
    if open_report:
        webbrowser.open(report.html_path.as_uri())


@s1_app.command("run-all")
def s1_run_all(
    ctx: typer.Context,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip inter-stage confirmation prompts — run straight through.",
        ),
    ] = False,
    tag_mode: Annotated[
        str,
        typer.Option(
            "--tag-mode",
            help=(
                "How Stage 05 handles tags: 'apply' commits to Zotero "
                "(default); 'preview' writes the CSV only and stops "
                "run-all before Stage 06 so the researcher can review."
            ),
        ),
    ] = "apply",
    allow_template_taxonomy: Annotated[
        bool,
        typer.Option(
            "--allow-template-taxonomy",
            help=(
                "Proceed even when config/taxonomy.yaml is marked "
                "status=template — Stage 05's safety gate is relaxed "
                "for deliberate testing runs. Never set on a real run."
            ),
        ),
    ] = False,
) -> None:
    """Run stages 01-06 sequentially with inter-stage prompts."""
    from typing import cast

    from zotai.config import Settings
    from zotai.s1.run_all import TagMode, format_summary, run_all

    if tag_mode not in ("apply", "preview"):
        typer.secho(
            f"Invalid --tag-mode '{tag_mode}'. Choose 'apply' or 'preview'.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(code=2)

    settings = Settings()
    dry_run = bool(ctx.obj.get("dry_run", False)) or settings.behavior.dry_run

    result = run_all(
        yes=yes,
        dry_run=dry_run,
        tag_mode=cast(TagMode, tag_mode),
        allow_template_taxonomy=allow_template_taxonomy,
        settings=settings,
    )
    typer.echo(format_summary(result))
    if not result.completed:
        raise typer.Exit(code=1)


@s1_app.command("status")
def s1_status() -> None:
    """Print per-stage counts, costs, and errors from `state.db`."""
    from zotai.config import Settings
    from zotai.s1.status import compute_status, format_status

    settings = Settings()
    snapshot = compute_status(settings=settings)
    typer.echo(format_status(snapshot))


# ─── S2 commands ──────────────────────────────────────────────────────────


@s2_app.command("fetch-once")
def s2_fetch_once() -> None:
    """Run one RSS fetch cycle and persist new candidates.

    Step 0 of the cycle is the embedding-index reconcile (ADR 015), so
    this command keeps the ChromaDB invariant in sync even when the
    in-process scheduler is disabled (`S2_WORKER_DISABLED=true`).
    """
    _not_implemented("s2 fetch-once", 11, 12)


@s2_app.command("backfill-index")
def s2_backfill_index(
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the interactive confirmation prompt that shows the "
            "estimated cost before embedding starts.",
        ),
    ] = False,
) -> None:
    """Embed every non-quarantined Zotero item into ChromaDB.

    First-run command for S2 after S1 has populated the Zotero library.
    Same reconcile_embeddings() code as the worker's step 0, but with
    `max_per_cycle` lifted, a progress bar, and its own budget cap
    (`S2_MAX_COST_USD_BACKFILL`, default $3.00). Idempotent — re-running
    after a partial backfill resumes from where it left off.

    Defined by ADR 015 §2; implementation lands in S2 Sprint 1 (#12).
    """
    _ = yes
    _not_implemented("s2 backfill-index", 11, 12)


@s2_app.command("reconcile")
def s2_reconcile() -> None:
    """Run a single reconcile_embeddings() cycle without RSS fetch.

    Useful for: forcing the propagation of a recent push, debugging an
    index/library divergence, or running from an external cron job that
    drives reconciliation independently of the worker. Bounded by
    `S2_MAX_EMBED_PER_CYCLE` and `S2_SAFE_DELETE_RATIO` defaults from
    `.env` (same as the worker).
    """
    _not_implemented("s2 reconcile", 11, 12)


@s2_app.command("dashboard")
def s2_dashboard() -> None:
    """Start the FastAPI dashboard on 127.0.0.1:${S2_DASHBOARD_PORT}."""
    _not_implemented("s2 dashboard", 11, 12)


if __name__ == "__main__":  # pragma: no cover
    app()
