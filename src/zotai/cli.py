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
from typing import Annotated, Optional

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
        f"`{stage}` is not yet implemented — scheduled for Phase {phase} (#{issue}).",
        err=True,
        fg=typer.colors.YELLOW,
    )
    raise typer.Exit(code=1)


# ─── S1 commands ──────────────────────────────────────────────────────────


@s1_app.command("inventory")
def s1_inventory(
    ctx: typer.Context,
    folder: Annotated[
        Optional[list[Path]],
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
) -> None:
    """Stage 01 — scan PDFs, hash, detect DOI."""
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
            settings=settings,
        )
    except StageAbortedError as exc:
        typer.secho(f"Stage aborted: {exc}", err=True, fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    typer.echo(
        f"processed={result.items_processed} failed={result.items_failed} "
        f"duplicates={result.duplicates} invalid={result.invalid} "
        f"csv={result.csv_path}"
    )


@s1_app.command("ocr")
def s1_ocr(
    force_ocr: Annotated[bool, typer.Option("--force-ocr")] = False,
    parallel: Annotated[Optional[int], typer.Option("--parallel")] = None,
) -> None:
    """Stage 02 — OCR scanned PDFs."""
    _ = force_ocr, parallel
    _not_implemented("s1 ocr", 3, 4)


@s1_app.command("import")
def s1_import(
    batch_size: Annotated[int, typer.Option("--batch-size")] = 50,
) -> None:
    """Stage 03 — import PDFs into Zotero (Route A/B/C)."""
    _ = batch_size
    _not_implemented("s1 import", 4, 5)


@s1_app.command("enrich")
def s1_enrich(
    substage: Annotated[Optional[str], typer.Option("--substage")] = None,
    max_cost: Annotated[Optional[float], typer.Option("--max-cost")] = None,
) -> None:
    """Stage 04 — enrichment cascade (04a-04e)."""
    _ = substage, max_cost
    _not_implemented("s1 enrich", 5, 6)


@s1_app.command("tag")
def s1_tag(
    preview: Annotated[bool, typer.Option("--preview")] = False,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    re_tag: Annotated[bool, typer.Option("--re-tag")] = False,
    max_cost: Annotated[Optional[float], typer.Option("--max-cost")] = None,
) -> None:
    """Stage 05 — LLM tagging against the TEMA/METODO taxonomy."""
    _ = preview, apply, re_tag, max_cost
    _not_implemented("s1 tag", 6, 7)


@s1_app.command("validate")
def s1_validate(
    open_report: Annotated[bool, typer.Option("--open-report")] = False,
) -> None:
    """Stage 06 — generate validation report (HTML + CSV)."""
    _ = open_report
    _not_implemented("s1 validate", 7, 8)


@s1_app.command("run-all")
def s1_run_all(
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Run stages 01-06 sequentially with inter-stage prompts."""
    _ = yes
    _not_implemented("s1 run-all", 8, 9)


@s1_app.command("status")
def s1_status() -> None:
    """Print per-stage counts, costs, and errors from `state.db`."""
    _not_implemented("s1 status", 8, 9)


# ─── S2 commands ──────────────────────────────────────────────────────────


@s2_app.command("fetch-once")
def s2_fetch_once() -> None:
    """Run one RSS fetch cycle and persist new candidates."""
    _not_implemented("s2 fetch-once", 11, 12)


@s2_app.command("dashboard")
def s2_dashboard() -> None:
    """Start the FastAPI dashboard on 127.0.0.1:${S2_DASHBOARD_PORT}."""
    _not_implemented("s2 dashboard", 11, 12)


if __name__ == "__main__":  # pragma: no cover
    app()
