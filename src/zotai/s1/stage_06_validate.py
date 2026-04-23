"""Stage 06 — validation report (plan_01 §3 Etapa 06).

Reads everything S1 has produced so far (``state.db`` + the latest
``excluded_report_*.csv`` + ``quarantine_report_*.csv``) and emits:

- ``reports/s1_validation_<ts>.html`` — navigable single-file report
  with links back to Zotero for flagged items.
- ``reports/s1_validation_<ts>.csv`` — flat metric summary (one row
  per metric) the researcher can diff across runs.

Sections:

1. **Completeness** — % of items with full metadata, tags, extractable
   text.
2. **Tag distribution** — counts, orphan tags (<3 uses), dominant tags
   (>30% of the corpus). Populated only once Stage 05 has run.
3. **Consistency** — items with year outside ``[1900, today_year+1]``,
   zero authors, empty title.
4. **Potential duplicates** — pairs where
   ``rapidfuzz.fuzz.ratio(title) > 90`` share a year. LATAM-heavy corpora
   often have preprint / published twins; the researcher decides.
5. **Stage 01 filtering** — counts from the most recent
   ``excluded_report_*.csv`` + items with ``needs_review=True``.
6. **Costs** — total USD spent, breakdown per stage + service from
   ``ApiCall``.
7. **Timing** — per-stage wall-clock from ``Run``.

Stage 06 is read-only: it never writes to Zotero or mutates
``state.db``. Safe to run repeatedly and to reorder with other stages.
"""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from rapidfuzz import fuzz
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from zotai.config import Settings
from zotai.s1.handler import StageAbortedError
from zotai.state import ApiCall, Item, Run, init_s1, make_s1_engine
from zotai.utils.fs import ensure_dir
from zotai.utils.logging import bind, get_logger

log = get_logger(__name__)

_STAGE: Final[int] = 6

_DUPLICATE_FUZZ_THRESHOLD: Final[int] = 90
_ORPHAN_TAG_THRESHOLD: Final[int] = 3
# Dominant: a tag that shows up on >30 % of tagged items tends to be
# noise — either too broad or a classifier default.
_DOMINANT_TAG_RATIO: Final[float] = 0.30
_YEAR_MIN: Final[int] = 1900
# Upper bound is computed at runtime from the clock so the validator
# doesn't become stale next January.
_YEAR_UPPER_SLACK: Final[int] = 1


# ─── Result shapes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompletenessStats:
    total_items: int
    items_in_main: int
    items_in_quarantine: int
    with_zotero_key: int
    with_metadata: int
    with_tags: int
    with_fulltext: int


@dataclass(frozen=True)
class TagDistributionStats:
    tag_counts: dict[str, int]
    orphan_tags: list[str]
    dominant_tags: list[str]
    items_tagged: int


@dataclass(frozen=True)
class ConsistencyIssue:
    sha256: str
    zotero_item_key: str | None
    reason: str


@dataclass(frozen=True)
class DuplicatePair:
    sha_a: str
    sha_b: str
    key_a: str | None
    key_b: str | None
    title_a: str
    title_b: str
    year: int
    score: float


@dataclass(frozen=True)
class CostBreakdownRow:
    stage: int
    service: str
    calls: int
    cost_usd: float


@dataclass(frozen=True)
class StageTiming:
    stage: int
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None
    status: str


@dataclass(frozen=True)
class Stage01Filtering:
    excluded_count: int
    needs_review_count: int
    excluded_by_reason: dict[str, int]
    excluded_csv_path: Path | None


@dataclass
class ValidationReport:
    """Aggregate of every check Stage 06 runs.

    Dataclass (not ``frozen``) so the assembly code can populate lists
    incrementally without constructor gymnastics; callers treat it as
    read-only.
    """

    generated_at: datetime
    completeness: CompletenessStats
    tag_distribution: TagDistributionStats
    consistency_issues: list[ConsistencyIssue] = field(default_factory=list)
    duplicate_pairs: list[DuplicatePair] = field(default_factory=list)
    cost_total_usd: float = 0.0
    cost_by_stage_service: list[CostBreakdownRow] = field(default_factory=list)
    timing_by_stage: list[StageTiming] = field(default_factory=list)
    stage_01_filtering: Stage01Filtering = field(
        default_factory=lambda: Stage01Filtering(0, 0, {}, None)
    )
    html_path: Path | None = None
    csv_path: Path | None = None


# ─── Aggregators ─────────────────────────────────────────────────────────


def _compute_completeness(items: list[Item]) -> CompletenessStats:
    total = len(items)
    in_quarantine = sum(1 for it in items if it.in_quarantine)
    return CompletenessStats(
        total_items=total,
        items_in_main=total - in_quarantine,
        items_in_quarantine=in_quarantine,
        with_zotero_key=sum(1 for it in items if it.zotero_item_key),
        with_metadata=sum(1 for it in items if it.metadata_json),
        with_tags=sum(1 for it in items if it.tags_json),
        with_fulltext=sum(1 for it in items if it.has_text),
    )


def _compute_tag_distribution(items: list[Item]) -> TagDistributionStats:
    counter: Counter[str] = Counter()
    items_tagged = 0
    for it in items:
        if not it.tags_json:
            continue
        items_tagged += 1
        try:
            parsed = json.loads(it.tags_json)
        except json.JSONDecodeError:
            continue
        for key in ("tema", "metodo"):
            values = parsed.get(key) or []
            if isinstance(values, list):
                counter.update(v for v in values if isinstance(v, str))
    orphan = [tag for tag, n in counter.items() if n < _ORPHAN_TAG_THRESHOLD]
    dominant_floor = max(1, int(items_tagged * _DOMINANT_TAG_RATIO))
    dominant = [tag for tag, n in counter.items() if n > dominant_floor]
    return TagDistributionStats(
        tag_counts=dict(counter.most_common()),
        orphan_tags=sorted(orphan),
        dominant_tags=sorted(dominant),
        items_tagged=items_tagged,
    )


def _extract_title(metadata_json: str | None) -> str:
    if not metadata_json:
        return ""
    try:
        data = json.loads(metadata_json)
    except json.JSONDecodeError:
        return ""
    title = data.get("title")
    return title.strip() if isinstance(title, str) else ""


def _extract_year(metadata_json: str | None) -> int | None:
    if not metadata_json:
        return None
    try:
        data = json.loads(metadata_json)
    except json.JSONDecodeError:
        return None
    date_val = data.get("date")
    if isinstance(date_val, int):
        return date_val
    if isinstance(date_val, str):
        match = re.search(r"\b(\d{4})\b", date_val)
        if match is not None:
            return int(match.group(1))
    return None


def _extract_author_count(metadata_json: str | None) -> int:
    if not metadata_json:
        return 0
    try:
        data = json.loads(metadata_json)
    except json.JSONDecodeError:
        return 0
    creators = data.get("creators")
    if not isinstance(creators, list):
        return 0
    return sum(1 for c in creators if isinstance(c, dict))


def _compute_consistency(items: list[Item], *, now_year: int) -> list[ConsistencyIssue]:
    year_upper = now_year + _YEAR_UPPER_SLACK
    issues: list[ConsistencyIssue] = []
    for it in items:
        if it.in_quarantine:
            # Quarantined items surface through their own report; skipping
            # them here keeps the consistency view focused on the main
            # library.
            continue
        if not it.metadata_json:
            continue
        title = _extract_title(it.metadata_json)
        if not title:
            issues.append(
                ConsistencyIssue(
                    sha256=it.id,
                    zotero_item_key=it.zotero_item_key,
                    reason="missing_title",
                )
            )
        author_count = _extract_author_count(it.metadata_json)
        if author_count == 0:
            issues.append(
                ConsistencyIssue(
                    sha256=it.id,
                    zotero_item_key=it.zotero_item_key,
                    reason="zero_authors",
                )
            )
        year = _extract_year(it.metadata_json)
        if year is not None and not (_YEAR_MIN <= year <= year_upper):
            issues.append(
                ConsistencyIssue(
                    sha256=it.id,
                    zotero_item_key=it.zotero_item_key,
                    reason=f"year_out_of_range:{year}",
                )
            )
    return issues


def _compute_duplicates(items: list[Item]) -> list[DuplicatePair]:
    """Find candidate duplicate pairs by ``(fuzz>90, same year)``.

    Quadratic over the main-library items; fine for typical corpora of
    ~1000-2000 items (a few million comparisons, each a short string).
    If scale changes, swap for a blocking strategy (e.g. group by year
    first).
    """
    main_items = [it for it in items if not it.in_quarantine and it.metadata_json]
    prepared: list[tuple[Item, str, int | None]] = []
    for it in main_items:
        title = _extract_title(it.metadata_json)
        year = _extract_year(it.metadata_json)
        if title:
            prepared.append((it, title, year))
    pairs: list[DuplicatePair] = []
    for i, (item_a, title_a, year_a) in enumerate(prepared):
        for item_b, title_b, year_b in prepared[i + 1 :]:
            if year_a is None or year_b is None or year_a != year_b:
                continue
            score = float(fuzz.ratio(title_a, title_b))
            if score > _DUPLICATE_FUZZ_THRESHOLD:
                pairs.append(
                    DuplicatePair(
                        sha_a=item_a.id,
                        sha_b=item_b.id,
                        key_a=item_a.zotero_item_key,
                        key_b=item_b.zotero_item_key,
                        title_a=title_a,
                        title_b=title_b,
                        year=year_a,
                        score=score,
                    )
                )
    return pairs


def _compute_cost_breakdown(
    api_calls: list[ApiCall], runs: list[Run]
) -> tuple[float, list[CostBreakdownRow]]:
    total = sum(r.cost_usd for r in runs)
    per_key: Counter[tuple[int, str]] = Counter()
    cost_per_key: dict[tuple[int, str], float] = {}
    run_stage_by_id = {r.id: r.stage for r in runs if r.id is not None}
    for call in api_calls:
        stage = run_stage_by_id.get(call.run_id, 0)
        key = (stage, call.service)
        per_key[key] += 1
        cost_per_key[key] = cost_per_key.get(key, 0.0) + call.cost_usd
    rows = sorted(
        (
            CostBreakdownRow(
                stage=stage,
                service=service,
                calls=n,
                cost_usd=cost_per_key[(stage, service)],
            )
            for (stage, service), n in per_key.items()
        ),
        key=lambda r: (r.stage, r.service),
    )
    # Fall back to Run.cost_usd totals if ApiCall rows are absent (older
    # runs might not have populated ApiCall — we still want a number).
    if total == 0.0 and cost_per_key:
        total = sum(cost_per_key.values())
    return total, rows


def _compute_timings(runs: list[Run]) -> list[StageTiming]:
    rows: list[StageTiming] = []
    for r in sorted(runs, key=lambda x: (x.stage, x.started_at)):
        duration: float | None = None
        if r.finished_at is not None and r.started_at is not None:
            duration = (r.finished_at - r.started_at).total_seconds()
        rows.append(
            StageTiming(
                stage=r.stage,
                started_at=r.started_at,
                finished_at=r.finished_at,
                duration_seconds=duration,
                status=r.status,
            )
        )
    return rows


# ─── Stage 01 filtering (external reports) ───────────────────────────────


def _latest_csv(reports_folder: Path, prefix: str) -> Path | None:
    """Return the newest ``<prefix>_*.csv`` in ``reports_folder`` or None."""
    if not reports_folder.is_dir():
        return None
    matches = sorted(
        reports_folder.glob(f"{prefix}_*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    # Skip `_dryrun`-suffixed files; they should not count as the
    # representative last run.
    for path in matches:
        if "_dryrun" in path.name:
            continue
        return path
    return None


def _compute_stage_01_filtering(
    items: list[Item], reports_folder: Path
) -> Stage01Filtering:
    needs_review = sum(1 for it in items if it.needs_review)
    excluded_csv = _latest_csv(reports_folder, "excluded_report")
    excluded_count = 0
    excluded_by_reason: Counter[str] = Counter()
    if excluded_csv is not None and excluded_csv.exists():
        with excluded_csv.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                excluded_count += 1
                reason = (row.get("rejection_reason") or "").strip() or "unknown"
                excluded_by_reason[reason] += 1
    return Stage01Filtering(
        excluded_count=excluded_count,
        needs_review_count=needs_review,
        excluded_by_reason=dict(excluded_by_reason),
        excluded_csv_path=excluded_csv,
    )


# ─── HTML + CSV renderers ────────────────────────────────────────────────


_HTML_HEAD = """<!DOCTYPE html>
<html lang=\"en\">
<head>
<meta charset=\"utf-8\">
<title>S1 Validation Report — {ts}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
h1, h2 {{ border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }}
th {{ background: #f5f5f5; }}
.kpi {{ display: inline-block; padding: 0.4rem 0.8rem; margin: 0.2rem 0.4rem 0.2rem 0; background: #f0f4f8; border-radius: 4px; }}
.warn {{ color: #a15c00; }}
.ok {{ color: #1e6e1e; }}
code {{ background: #f5f5f5; padding: 0 0.2rem; border-radius: 2px; }}
</style>
</head>
<body>
"""

_HTML_TAIL = "\n</body>\n</html>\n"


def _zotero_link(
    library_id: str, library_type: str, item_key: str | None
) -> str:
    if not item_key:
        return ""
    lt = "users" if library_type == "user" else "groups"
    url = f"https://www.zotero.org/{lt}/{library_id}/items/{item_key}"
    return f"<a href=\"{url}\">{item_key}</a>"


def _fmt_datetime(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _render_html(
    report: ValidationReport, *, library_id: str, library_type: str
) -> str:
    ts = report.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")
    parts: list[str] = [_HTML_HEAD.format(ts=html.escape(ts))]
    parts.append(f"<h1>S1 Validation Report</h1><p>Generated: {html.escape(ts)}</p>")

    # ── 1. Completeness ─────────────────────────────────────────────
    c = report.completeness
    parts.append("<h2>1. Completeness</h2>")
    parts.append(
        f"<div class=\"kpi\">Total items: <b>{c.total_items}</b></div>"
        f"<div class=\"kpi\">Main library: <b>{c.items_in_main}</b></div>"
        f"<div class=\"kpi\">Quarantine: <b>{c.items_in_quarantine}</b></div>"
    )
    parts.append(
        "<table><tr><th>Field</th><th>Items</th><th>%</th></tr>"
        + _row("with Zotero key", c.with_zotero_key, c.total_items)
        + _row("with metadata", c.with_metadata, c.total_items)
        + _row("with tags", c.with_tags, c.total_items)
        + _row("with extractable text", c.with_fulltext, c.total_items)
        + "</table>"
    )

    # ── 2. Tag distribution ─────────────────────────────────────────
    t = report.tag_distribution
    parts.append("<h2>2. Tag distribution</h2>")
    parts.append(f"<p>Items tagged: <b>{t.items_tagged}</b></p>")
    if t.tag_counts:
        parts.append(
            "<table><tr><th>Tag</th><th>Count</th></tr>"
            + "".join(
                f"<tr><td>{html.escape(tag)}</td><td>{n}</td></tr>"
                for tag, n in t.tag_counts.items()
            )
            + "</table>"
        )
    else:
        parts.append("<p><em>No tagged items yet — run <code>zotai s1 tag --apply</code>.</em></p>")
    if t.orphan_tags:
        parts.append(
            f"<p class=\"warn\"><b>Orphan tags</b> (used &lt; {_ORPHAN_TAG_THRESHOLD}x): "
            + ", ".join(html.escape(tag) for tag in t.orphan_tags)
            + "</p>"
        )
    if t.dominant_tags:
        parts.append(
            f"<p class=\"warn\"><b>Dominant tags</b> (&gt; {int(_DOMINANT_TAG_RATIO * 100)} %): "
            + ", ".join(html.escape(tag) for tag in t.dominant_tags)
            + "</p>"
        )

    # ── 3. Consistency ──────────────────────────────────────────────
    parts.append("<h2>3. Consistency issues</h2>")
    if report.consistency_issues:
        parts.append(
            "<table><tr><th>SHA-256</th><th>Zotero</th><th>Reason</th></tr>"
            + "".join(
                f"<tr><td><code>{html.escape(issue.sha256[:12])}…</code></td>"
                f"<td>{_zotero_link(library_id, library_type, issue.zotero_item_key)}</td>"
                f"<td>{html.escape(issue.reason)}</td></tr>"
                for issue in report.consistency_issues
            )
            + "</table>"
        )
    else:
        parts.append('<p class="ok">No consistency issues detected.</p>')

    # ── 4. Potential duplicates ─────────────────────────────────────
    parts.append("<h2>4. Potential duplicate pairs</h2>")
    if report.duplicate_pairs:
        parts.append(
            "<table><tr><th>Year</th><th>Score</th><th>Title A</th><th>Title B</th><th>Zotero A</th><th>Zotero B</th></tr>"
            + "".join(
                f"<tr><td>{pair.year}</td><td>{pair.score:.1f}</td>"
                f"<td>{html.escape(pair.title_a)}</td>"
                f"<td>{html.escape(pair.title_b)}</td>"
                f"<td>{_zotero_link(library_id, library_type, pair.key_a)}</td>"
                f"<td>{_zotero_link(library_id, library_type, pair.key_b)}</td></tr>"
                for pair in report.duplicate_pairs
            )
            + "</table>"
        )
    else:
        parts.append('<p class="ok">No potential duplicates detected.</p>')

    # ── 5. Stage 01 filtering ───────────────────────────────────────
    f01 = report.stage_01_filtering
    parts.append("<h2>5. Stage 01 filtering</h2>")
    parts.append(
        f"<div class=\"kpi\">Excluded PDFs: <b>{f01.excluded_count}</b></div>"
        f"<div class=\"kpi\">Needs review: <b>{f01.needs_review_count}</b></div>"
    )
    if f01.excluded_csv_path:
        parts.append(
            f"<p>Latest excluded report: <code>{html.escape(str(f01.excluded_csv_path))}</code></p>"
        )
    if f01.excluded_by_reason:
        parts.append(
            "<table><tr><th>Reason</th><th>Count</th></tr>"
            + "".join(
                f"<tr><td>{html.escape(r)}</td><td>{n}</td></tr>"
                for r, n in sorted(f01.excluded_by_reason.items())
            )
            + "</table>"
        )

    # ── 6. Costs ────────────────────────────────────────────────────
    parts.append("<h2>6. Costs</h2>")
    parts.append(
        f"<div class=\"kpi\">Total spent: <b>${report.cost_total_usd:.4f}</b></div>"
    )
    if report.cost_by_stage_service:
        parts.append(
            "<table><tr><th>Stage</th><th>Service</th><th>Calls</th><th>USD</th></tr>"
            + "".join(
                f"<tr><td>{row.stage:02d}</td><td>{html.escape(row.service)}</td>"
                f"<td>{row.calls}</td><td>${row.cost_usd:.4f}</td></tr>"
                for row in report.cost_by_stage_service
            )
            + "</table>"
        )

    # ── 7. Timings ──────────────────────────────────────────────────
    parts.append("<h2>7. Timings</h2>")
    if report.timing_by_stage:
        parts.append(
            "<table><tr><th>Stage</th><th>Status</th><th>Started</th><th>Finished</th><th>Duration</th></tr>"
            + "".join(
                f"<tr><td>{t.stage:02d}</td><td>{html.escape(t.status)}</td>"
                f"<td>{html.escape(_fmt_datetime(t.started_at))}</td>"
                f"<td>{html.escape(_fmt_datetime(t.finished_at))}</td>"
                f"<td>{(f'{t.duration_seconds:.1f}s') if t.duration_seconds is not None else ''}</td></tr>"
                for t in report.timing_by_stage
            )
            + "</table>"
        )

    parts.append(_HTML_TAIL)
    return "".join(parts)


def _row(label: str, numerator: int, denominator: int) -> str:
    pct = (100.0 * numerator / denominator) if denominator else 0.0
    return (
        f"<tr><td>{html.escape(label)}</td><td>{numerator}</td>"
        f"<td>{pct:.1f} %</td></tr>"
    )


_SUMMARY_CSV_COLUMNS: Final[tuple[str, ...]] = ("section", "metric", "value")


def _write_summary_csv(path: Path, report: ValidationReport) -> None:
    rows: list[tuple[str, str, str]] = []
    c = report.completeness
    rows.extend(
        [
            ("completeness", "total_items", str(c.total_items)),
            ("completeness", "items_in_main", str(c.items_in_main)),
            ("completeness", "items_in_quarantine", str(c.items_in_quarantine)),
            ("completeness", "with_zotero_key", str(c.with_zotero_key)),
            ("completeness", "with_metadata", str(c.with_metadata)),
            ("completeness", "with_tags", str(c.with_tags)),
            ("completeness", "with_fulltext", str(c.with_fulltext)),
            ("tag_distribution", "items_tagged", str(report.tag_distribution.items_tagged)),
            (
                "tag_distribution",
                "unique_tags",
                str(len(report.tag_distribution.tag_counts)),
            ),
            (
                "tag_distribution",
                "orphan_tags",
                str(len(report.tag_distribution.orphan_tags)),
            ),
            (
                "tag_distribution",
                "dominant_tags",
                str(len(report.tag_distribution.dominant_tags)),
            ),
            ("consistency", "issues_total", str(len(report.consistency_issues))),
            ("duplicates", "pairs_total", str(len(report.duplicate_pairs))),
            (
                "stage_01_filtering",
                "excluded_count",
                str(report.stage_01_filtering.excluded_count),
            ),
            (
                "stage_01_filtering",
                "needs_review_count",
                str(report.stage_01_filtering.needs_review_count),
            ),
            ("costs", "total_usd", f"{report.cost_total_usd:.6f}"),
        ]
    )
    for row in report.cost_by_stage_service:
        rows.append(
            (
                "costs",
                f"stage_{row.stage:02d}__{row.service}_usd",
                f"{row.cost_usd:.6f}",
            )
        )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(_SUMMARY_CSV_COLUMNS)
        writer.writerows(rows)


# ─── Public entry points ─────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def run_validate(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    now: Iterable[datetime] | None = None,
) -> ValidationReport:
    """Assemble the validation report and write HTML + CSV to disk.

    ``now`` is a kw-only iterable for tests — when given, its first
    value is used as the report timestamp; otherwise ``datetime.now``.
    Returns the assembled :class:`ValidationReport` populated with the
    paths of the written files.
    """
    settings = settings or Settings()
    if engine is None:
        engine = make_s1_engine(str(settings.paths.state_db))
        init_s1(engine)

    if now is None:
        generated_at = _utc_now()
    else:
        iterator = iter(now)
        try:
            generated_at = next(iterator)
        except StopIteration as exc:
            raise StageAbortedError("Empty `now` iterable") from exc

    bind(stage=_STAGE)
    log.info("stage_started")

    with Session(engine) as session:
        items = list(session.exec(select(Item)))
        runs = list(session.exec(select(Run)))
        api_calls = list(session.exec(select(ApiCall)))

    completeness = _compute_completeness(items)
    tag_distribution = _compute_tag_distribution(items)
    consistency = _compute_consistency(items, now_year=generated_at.year)
    duplicates = _compute_duplicates(items)
    cost_total, cost_rows = _compute_cost_breakdown(api_calls, runs)
    timings = _compute_timings(runs)
    filtering = _compute_stage_01_filtering(items, settings.paths.reports_folder)

    report = ValidationReport(
        generated_at=generated_at,
        completeness=completeness,
        tag_distribution=tag_distribution,
        consistency_issues=consistency,
        duplicate_pairs=duplicates,
        cost_total_usd=cost_total,
        cost_by_stage_service=cost_rows,
        timing_by_stage=timings,
        stage_01_filtering=filtering,
    )

    reports_folder = ensure_dir(settings.paths.reports_folder)
    timestamp = generated_at.strftime("%Y%m%d_%H%M%S")
    html_path = reports_folder / f"s1_validation_{timestamp}.html"
    csv_path = reports_folder / f"s1_validation_{timestamp}.csv"

    library_id = settings.zotero.library_id or ""
    library_type = settings.zotero.library_type
    html_path.write_text(
        _render_html(report, library_id=library_id, library_type=library_type),
        encoding="utf-8",
    )
    _write_summary_csv(csv_path, report)
    report.html_path = html_path
    report.csv_path = csv_path

    log.info(
        "stage_finished",
        total_items=completeness.total_items,
        consistency_issues=len(consistency),
        duplicates=len(duplicates),
        cost_total_usd=round(cost_total, 6),
        html=str(html_path),
        csv=str(csv_path),
    )
    return report


__all__ = [
    "CompletenessStats",
    "ConsistencyIssue",
    "CostBreakdownRow",
    "DuplicatePair",
    "Stage01Filtering",
    "StageTiming",
    "TagDistributionStats",
    "ValidationReport",
    "run_validate",
]
