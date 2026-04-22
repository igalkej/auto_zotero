# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **S1 Stage 01 — inventory** (#3): `zotai.s1.stage_01_inventory.run_inventory`
  walks configured PDF folders, validates magic bytes, hashes via SHA-256,
  detects DOIs from the first 3 pages, and persists `Item` rows with
  `stage_completed=1`. Duplicates (same hash, new path) are reported in
  `reports/inventory_report_<ts>.csv` without overwriting the winner's
  `source_path`; re-runs are no-ops (`status=unchanged`). A new
  `--retry-errors` flag re-invokes extraction on existing rows whose prior
  pass left a `last_error` (transient I/O / pdfplumber failures), reporting
  them as `retried` on success or `error` if the failure persists. The CLI
  command `zotai s1 inventory [--folder PATH ...] [--retry-errors]` is now
  functional and honours the root `--dry-run` flag (writes a `_dryrun`-
  suffixed CSV and skips DB writes). Test suite covers the five canonical
  fixtures, dedup, dry-run, re-run idempotence, retry-errors (transient +
  persistent), CSV contents, CLI wiring, and the stage-abort threshold.
- **Scaffolding** (#1): initial project skeleton — `pyproject.toml` with uv/hatchling,
  multi-stage `Dockerfile`, `docker-compose.yml` with `onboarding` and `dashboard`
  services, `.env.example`, Alembic config with an empty migrations directory,
  source package tree (`src/zotai/{s1,s2,s2/dashboard,api,utils}`), test harness
  (`tests/conftest.py`, per-subsystem test packages), config templates
  (`config/{taxonomy,feeds,scoring}.yaml`, feeds all `active: false`),
  `scripts/healthcheck.py`, MIT `LICENSE`.
- **Shared infrastructure** (#2): `zotai.config` (pydantic-settings groups for
  Zotero / OpenAI / Semantic Scholar / OCR / paths / budgets / behaviour / S2),
  `zotai.state` (SQLModel tables for S1 + S2, separate engines, `init_s1` /
  `init_s2` helpers, `metadata` exported for Alembic), `zotai.cli` (Typer app
  with `s1` / `s2` sub-apps and stubbed commands), `zotai.utils.{logging,http,
  fs,pdf}`, `zotai.api.{zotero,openalex,semantic_scholar,openai_client}` with
  dry-run and budget enforcement, `zotai.s1.handler.stage_item_handler`
  decorator, test suite (`tests/test_config.py`, `tests/test_state.py`,
  `tests/test_utils/test_{fs,pdf}.py`). `alembic/env.py` now points at
  `zotai.state.metadata` instead of `None`.

### Changed

- **Spec**: `docs/plan_01_subsystem1.md` §3 Etapa 01 — added an
  **academic / non-academic classifier** upstream of the rest of the S1
  pipeline (plan §3.1). Hybrid strategy: (1) zero-cost positive
  heuristic accept on DOI / arXiv / ISBN / academic keywords in pages
  1-3; (2) zero-cost negative heuristic reject on `pages ≤ 2` combined
  with absent text or billing keywords (`factura`, `recibo`, `invoice`,
  `CUIT`, `CUIL`, `DNI`, `ticket`, etc.); (3) LLM gate (`gpt-4o-mini`)
  for the ambiguous remainder, budgeted via a new
  `MAX_COST_USD_STAGE_01=1.00` env var (~$0.12 / 1000 PDFs expected).
  Rejected PDFs land in `reports/excluded_report_<ts>.csv` and never
  enter `state.db`, so they consume no OCR or downstream API calls.
  New `Item.classification` and `Item.needs_review` columns documented
  (implementation lands in a separate PR — Phase 2.5). New CLI flags
  `--skip-llm-gate` and `--max-cost N` on `zotai s1 inventory`.
  `plan_glossary.md` gained entries for *Clasificador académico / no-
  académico*, *Excluded report*, and *Needs review*. `README.md` and
  `CLAUDE.md` updated accordingly. Rationale: researchers' source
  folders (`Downloads/`, etc.) are mixed content; running the whole
  pipeline on a DNI photo or electricity bill wastes OCR + API budget
  and pollutes Zotero. Option C (híbrido) from the 2026-04-21 review.
- **Spec**: `docs/plan_01_subsystem1.md` §3 Etapa 03 — removed Ruta B
  (Zotero "Retrieve Metadata for PDFs" recognizer applied to orphan
  attachments). Items without a detected DOI, or where Ruta A's
  translator fails, now fall directly to Ruta C → Etapa 04 enrichment
  cascade. Expected distribution in §126 adjusted: Ruta A 50-60%, Ruta
  C 40-50% (previously A 50-60% / B 15-20% / C 20-35%). `plan_glossary.md`
  "Ruta A/B/C" entry renamed to "Ruta A/C"; the `import_route` column
  comment in `src/zotai/state.py` now reads `'A' | 'C'`. Rationale: the
  Zotero recognizer endpoint is not a stable public API and its
  programmatic invocation is brittle between Zotero Desktop versions;
  Etapa 04's cascade (04a-d) already covers the no-DOI case with
  multiple sources and better LATAM/ES coverage; dropping from 3 to 2
  routes shrinks Etapa 03's blast radius. (Option D from the 2026-04-21
  alignment review.)
- **Spec**: `docs/plan_02_subsystem2.md` §10 — replaced the single-line PDF
  fetch priority (which excluded Sci-Hub as "illegal") with an explicit
  six-source cascade: OpenAlex OA URL → DOI resolver → Anna's Archive →
  Library Genesis → Sci-Hub → RSS URL. Each source is toggleable via
  `S2_PDF_SOURCES`. Rationale: the tool is local and personal, and the
  target audience relies on these services as standard fallbacks when
  institutional access falls short. Issue #16.
