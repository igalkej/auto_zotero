# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Networking**: the `onboarding` and `dashboard` Compose services
  switch from `network_mode: host` to default bridge networking with
  `extra_hosts: - "host.docker.internal:host-gateway"` so the same
  setup works on Linux, macOS, and Windows uniformly (the previous
  `network_mode: host` silently no-ops on Docker Desktop). `pyzotero`'s
  hardcoded `http://localhost:23119/api` endpoint is now overridable
  via `ZOTERO_LOCAL_API_HOST` (new `.env` key, default
  `http://host.docker.internal:23119` inside the Compose containers;
  empty outside Docker → pyzotero's default stands). New
  `ZoteroSettings.local_api_host` setting + `ZoteroClient(..,
  local_api_host=...)` constructor kwarg; Stage 03 wires it through
  from settings. The `dashboard` container's uvicorn bind moves from
  `127.0.0.1` to `0.0.0.0` *inside* the container so the
  `127.0.0.1:8000:8000` port mapping (now truthful under bridge mode)
  actually reaches it — the host-side binding remains localhost-only.
  Covered by new `tests/test_api/test_zotero.py` (endpoint respects
  default, override, trailing-slash strip, and is ignored when
  `local=False`). See ADR 013.

### Added

- **S1 Stage 03 — import to Zotero** (#5): `zotai.s1.stage_03_import.run_import`
  walks every item with `stage_completed=2 AND has_text=True AND
  classification='academic' AND zotero_item_key IS NULL` and pushes them
  into Zotero using two routes per ADR 010 and plan_01 §3 Etapa 03.
  **Route A** (DOI present): `OpenAlexClient.work_by_doi` fetches the
  bibliographic record; `map_openalex_to_zotero` maps OpenAlex's schema
  to Zotero's (title, creators with Western-order name split, DOI,
  year, venue, `itemType` from a table covering journal articles,
  chapters, books, dissertations, preprints, reports, proceedings),
  reconstructing the abstract from OpenAlex's inverted index and
  dropping items that fail the quality gate (missing title or zero
  authors). Before creation, we quicksearch Zotero for an existing
  item with the same DOI and link to it instead of duplicating.
  **Route C** absorbs items without a DOI, items whose DOI isn't in
  OpenAlex, and items that fail the quality gate — all land as top-
  level orphan attachments that Stage 04's cascade will enrich. In
  both routes the attachment is the OCR'd `staging/<hash>.pdf` if
  Stage 02 produced one, otherwise the original `Item.source_path`;
  Zotero copies the file to `~/Zotero/storage/<attach_key>/` in the
  default **stored** mode. A connectivity probe (`items(limit=1)`)
  runs before the first batch and aborts cleanly with
  `StageAbortedError` if the Zotero local API is unreachable (desktop
  not open, wrong keys), so users see the failure up-front rather
  than mid-run. Processing is batched (default 50 items, 30 s sleep
  between batches — both overridable via `--batch-size` and
  `--batch-pause-seconds`); re-runs skip items that already have a
  `zotero_item_key`. The CLI command `zotai s1 import
  [--batch-size N] [--batch-pause-seconds S]` is now functional and
  honours the root `--dry-run` flag (connectivity still probed but no
  Zotero writes, no DB mutations, `_dryrun`-suffixed CSV). Per-item
  reports land in `reports/import_report_<ts>.csv`. Covered by 18
  tests: the mapping matrix (full record, missing title, no authors,
  book chapter, unknown type default, single-token name), Route A
  success, Route A fall-through on 404 and on missing title, Route A
  dedup against an existing DOI, Route C orphan creation, the
  "prefer staging over source" rule, eligibility filters
  (already-imported / no text / prior stage), connectivity abort,
  dry-run, batching cadence, CSV shape, and a CLI smoke test.
- **S1 Stage 02 — OCR** (#4): `zotai.s1.stage_02_ocr.run_ocr` walks every
  item with `has_text=False AND stage_completed=1`, copies the source
  PDF to `staging/<sha256>.pdf`, runs `ocrmypdf.ocr()` (default
  `skip_text=True`; `force_ocr=True` with the `--force-ocr` flag), and
  batches DB updates at the end. OCR happens on `multiprocessing.Pool`
  with `OCR_PARALLEL_PROCESSES` workers (default 4, overridable with
  `--parallel N`; `N<=1` runs sequentially). Before the first copy the
  stage verifies free disk on the staging volume ≥ 2× the corpus size;
  if not, it aborts cleanly with `StageAbortedError`. Resume-safe: when
  `staging/<hash>.pdf` already exists with a text layer, the worker
  skips the `ocrmypdf` call. Items where OCR fails advance to
  `stage_completed=2` anyway with `ocr_failed=True` and the error in
  `last_error` so Stage 03 sees them. The CLI command `zotai s1 ocr
  [--force-ocr] [--parallel N]` is now functional and honours the root
  `--dry-run` flag (no file I/O, no DB writes, `_dryrun`-suffixed CSV).
  Per-item reports land in `reports/ocr_report_<ts>.csv`. Tests cover
  the happy path, the two failure modes (`ocrmypdf` exception + OCR
  produces no text), no-op on already-processed items, disk-space
  abort, dry-run, resume semantics, `--force-ocr` flag plumbing, and
  CLI wiring. `ocrmypdf` is monkeypatched in every test so Tesseract
  is not a test dependency.
- **S1 Stage 01 — classifier** (#24): three-branch academic / non-academic
  gate upstream of the rest of the S1 pipeline (plan_01 §3.1).
  (1) Positive heuristic — zero-cost accept on DOI / arXiv / valid ISBN
  / academic keyword hit in pages 1-3. (2) Negative heuristic — zero-cost
  reject when `page_count <= 2` and either a billing / personal-document
  keyword is present on page 1 or the PDF has no extractable text
  (keyword match is preferred because it carries the more informative
  rejection reason). (3) LLM gate — `gpt-4o-mini` JSON-mode call for
  the ambiguous remainder, with one retry on malformed JSON and a
  conservative bias (ambiguity → keep as academic with
  `needs_review=True`). Landed as `src/zotai/s1/classifier.py`
  (`heuristic_accept`, `heuristic_reject`, `llm_gate`, `classify`), a
  new `OpenAIClient.classify_document` helper, and a new
  `utils.pdf.count_pages`. Integrated into `stage_01_inventory`: accepted
  items persist with `classification='academic' [+ needs_review]`;
  rejected items never enter `state.db` and are written to
  `reports/excluded_report_<ts>.csv` (columns: `source_path`, `sha256`,
  `size_bytes`, `page_count`, `rejection_reason`, `classifier_branch`,
  `llm_reason`). `inventory_report.csv` gained `classification`,
  `needs_review`, and `rejection_reason` columns. CLI flags
  `--skip-llm-gate` (ambiguous → needs_review without OpenAI) and
  `--max-cost N` (per-run override of `MAX_COST_USD_STAGE_01`) added to
  `zotai s1 inventory`. `BudgetSettings.max_cost_usd_stage_01` with
  default `1.0` + `.env.example` line `MAX_COST_USD_STAGE_01=1.00`.
  Alembic migration `20260422_classifier_columns` adds
  `Item.classification` and `Item.needs_review` for existing DBs.
  Covered by `tests/test_s1/test_classifier.py` (pure-function matrix)
  and new integration scenarios in `tests/test_s1/test_stage_01.py`
  (factura / DNI rejection, keyword acceptance, LLM mocking,
  `--skip-llm-gate`, budget-exceeded abort, re-run does not
  reclassify). As a drive-by, `_run_inventory_async` now snapshots
  `Run` fields before the session closes to avoid a
  `DetachedInstanceError` introduced by newer SQLAlchemy semantics.
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
