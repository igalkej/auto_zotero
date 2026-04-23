# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **S2 settings — ADR 015 / ADR 017 env vars surfaced in `S2Settings`**:
  nine knobs that `.env.example` and `plan_02` §12 already document now
  round-trip through `zotai.config.S2Settings` instead of being silently
  dropped by `extra="ignore"`. Fields added with defaults matching
  `.env.example`: `max_embed_per_cycle=50`, `safe_delete_ratio=0.10`,
  `max_cost_usd_backfill=3.0`, `query_bm25_weight=0.4`,
  `pdf_fetch_max_attempts_per_candidate=6`,
  `pdf_fetch_timeout_seconds=30`, `pdf_fetch_max_minutes_weekly=20`,
  `pdf_fetch_circuit_breaker_threshold=5`, `worker_disabled=False`.
  Validators enforce the documented ranges: `_positive` (>=1) extended
  to cover `max_embed_per_cycle` +
  `pdf_fetch_max_attempts_per_candidate` +
  `pdf_fetch_timeout_seconds` + `pdf_fetch_max_minutes_weekly`; new
  `_non_negative` (>=0) on `max_cost_usd_backfill` and
  `pdf_fetch_circuit_breaker_threshold` (the latter accepts 0 because
  plan_02 §10.4 documents `0` as the way to disable the breaker); new
  `_unit_interval` on `safe_delete_ratio` and `query_bm25_weight`. No
  runtime code consumes these yet — S2 Sprint 1 (#12) is the first
  consumer; the point of this PR is to keep the CLAUDE.md "fail-loud"
  principle: users who set these in `.env` today (following
  `.env.example`) are heard by the settings layer instead of being
  silently ignored. Covered by 7 new tests in `tests/test_config.py`
  (defaults, env reads including `threshold=0` and `worker_disabled`,
  plus validator rejections for out-of-range values). Full suite: 133
  passed; `mypy --strict src/zotai/config.py` clean.

- **S1 Stage 04 — substage 04a** (#6, first of three PRs for Stage 04):
  `zotai.s1.stage_04_enrich.run_enrich(substage="04a")` walks items with
  `import_route='C' AND stage_completed=3` and runs aggressive
  identifier extraction on the PDF's first 3 pages. When a *new* DOI is
  found (i.e. one not already in `Item.detected_doi`), the function
  retries Route A from Stage 03: `OpenAlexClient.work_by_doi` →
  `map_openalex_to_zotero` (imported from stage_03) → `create_items` →
  reparent the orphan attachment under the new parent via
  `update_item({..., parentItem: new_parent_key})`. Dedup with ADR 014
  applies: if an existing Zotero item already has the DOI + a PDF, we
  link the `Item` row to that key but do NOT reparent (avoids
  duplicating PDFs). Status matrix: `enriched_04a` / `no_progress` (no
  new DOI, OpenAlex 404, or quality gate failed) / `failed` (network
  error, missing orphan) / `dry_run` / `skipped_already_enriched`.
  Per-item reports land in `reports/enrich_report_<ts>.csv` with columns
  `sha256, source_path, zotero_item_key_before, zotero_item_key_after,
  substage_resolved, new_doi, status, error`. CLI `zotai s1 enrich
  [--substage 04a] [--dry-run]` is wired; other `--substage` values
  exit with a "not yet implemented" message until the follow-up PRs.
  Covered by 9 tests: happy path, no-new-DOI, DOI matches Stage 01's,
  OpenAlex 404, quality gate fail, dedup + existing PDF (link without
  reparent), dry-run, idempotent re-run, "substage 04b raises". Full
  suite: 126 passed; `mypy --strict` clean.
  Follow-up PRs: 04b + 04c in the next PR; 04d + 04e + full cascade
  orchestrator + ADRs 005 + 008 in the third.
- **ADR 015 Fase 2 validation tooling**:
  `scripts/validate_chromadb_schema.py` — standalone script that
  populates a ChromaDB with the schema ADR 015 §6 prescribes (Zotero-
  style 8-char IDs, real OpenAI `text-embedding-3-large` embeddings,
  metadata `{title, year, item_type, doi, source, indexed_at,
  source_subsystem}`). CLI flags: `--path`, `--collection-name`
  (default `zotero_library`), `--num-items`, `--embedding-model`,
  `--seed`. Requires `OPENAI_API_KEY` and the `s2` optional
  dependencies (`chromadb>=0.5`). `docs/decisions/015-validation-checklist.md`
  — user-side manual checklist for the five-step validation against
  `zotero-mcp serve` + Claude Desktop that ADR 015 §5 requires before
  Fase 3 code lands. Bloqueante para Fase 3.

### Changed

- **Corpus LATAM reality check** (plan_01): §3 Etapa 03 and §3 Etapa 04
  acknowledge explicitly that the "50-60% Ruta A" estimate assumes an
  anglo-dominant corpus. For LATAM-heavy corpora (CEPAL Review, Desarrollo
  Económico, Estudios Económicos, BID / CAF / BCRA papers, SciELO /
  RedALyC journals), OpenAlex and Semantic Scholar coverage drops to
  20-50% per journal, and the realistic split is closer to Ruta A 30-40% /
  Ruta C 60-70%. Etapa 04 budget guidance updated: users with
  LATAM-heavy corpora should bump `MAX_COST_USD_STAGE_04` from 2.00 to
  ~4.00 before their first run. Quarantine success criterion amended:
  <10% for anglo-dominant, <25% for LATAM-heavy, until the v1.1
  extension with LATAM-specific metadata sources (REDIB / SciELO /
  La Referencia / RedALyC) lands per the tracking issue. No code
  changes — this is a realism adjustment to the spec.
- **S2 PDF fetch cascade robustness** (plan_02 §10): two new subsections
  added to the push flow to harden Sci-Hub / LibGen / Anna's Archive
  against the operational reality that mirrors rotate domains, serve
  HTML-of-error with 200 OK, and occasionally show CAPTCHA.
  - **§10.3 Verificación post-descarga**: every source must deliver a
    file that passes 4 checks in order — `Content-Type` starts with
    `application/pdf`, magic bytes `%PDF-`, size ≥ 50 KB,
    `pdfplumber.open()` parses without exception. Fail any → treat as
    miss, try next source.
  - **§10.4 Circuit breaker por fuente**: in-memory consecutive-failure
    counter per source within one `run_fetch_cycle()`; 5 failures in a
    row → skip that source for the rest of the cycle. Configurable via
    `S2_PDF_FETCH_CIRCUIT_BREAKER_THRESHOLD=5` (default). Protects the
    weekly wall-clock budget from being drained by a fully-down mirror.
  `.env.example` and `plan_02` §12 reflect the new env var. Docs only;
  implementation lands with Sprint 2 (#13).
- **S2 query scoring — ADR 017**: `score_queries` moves from pure
  dense cosine to a convex hybrid with BM25 — `α·BM25 + (1-α)·cos`,
  default `α=0.4`. Fixes the known recall gap of dense-only retrieval
  on short queries (3-7 tokens), which is exactly the shape of the
  researcher's persistent queries. SQLite FTS5 (built-in since 3.9,
  2015) backs BM25 — no new dependency. Changes: `plan_02` §7.3
  rewritten with hybrid formula + FTS5 schema snippet, §5 notes the
  new `candidate_fts` virtual table in `candidates.db` with sync
  triggers, §12 adds `S2_QUERY_BM25_WEIGHT` env var, §15 adds SQLite
  ≥ 3.9 to the dependency list. `config/scoring.yaml` gains a
  `query_scoring.bm25_weight: 0.4` block. `.env.example` mirrors the
  env var with guidance on the useful range. `plan_00` §5 decisions
  table gets rows for ADR 016 (RRF, landing via #43) and ADR 017
  (this PR). Docs + config only; implementation lands with Sprint 2
  (#13). Calibration path (grid search over α once ≥100 decisions
  exist) deferred to a successor ADR, same pattern as ADR 016.

- **Architecture (Fase 1 of ADR 015 — docs alignment)**: rippled the
  S2-owns-embeddings inversion across all the documents that used to
  describe the pre-ADR-015 ownership model.
  - `plan_00_overview.md` §4 + §5: clarified that "S3" in the
    S1 → S3 → S2 order means setup of `zotero-mcp serve` only — no
    `update-db` is part of any operational flow under ADR 015. Decisions
    table extended with rows 010-015 (was missing 010+).
  - `plan_01_subsystem1.md` §10: line about ChromaDB in "Fuera de
    alcance" inverted ("responsabilidad de S2 (ver ADR 015). S1 no
    escribe a ChromaDB bajo ninguna circunstancia").
  - `plan_02_subsystem2.md` §4 (architecture diagram), §5 (data model),
    §7.2 (semantic score fallback now `min_corpus_size`-based, not
    "empty"-based), §9 (worker pseudocode now opens with reconcile
    step), §10 (push does not write ChromaDB directly), §11 (Sprint 1
    grows the indexing module + `backfill-index` / `reconcile` CLI
    deliverables; Sprint 3 simplified), §12 (new env vars
    `S2_MAX_EMBED_PER_CYCLE`, `S2_SAFE_DELETE_RATIO`,
    `S2_MAX_COST_USD_BACKFILL`), §15 (dependencies inverted: S2 is the
    owner; S3 setup is no longer a prerequisite).
  - `plan_03_subsystem3.md` §4.3 (ownership flipped, mount becomes
    `:rw`), §5.2 (removed "Build del índice inicial" step), §7.1 (the
    re-indexing section is now obsolete; the user is redirected to
    `zotai s2 reconcile` / the worker's automatic cycle), §8
    (S2/S3 integration direction inverted), §9 (deliverables: removed
    `scripts/reindex-s3.{sh,ps1}`).
  - `plan_glossary.md`: "Chroma DB" entry inverted; new entries for
    "Reconciliación de embeddings" and "Backfill de índice".
  - `CLAUDE.md` §"Contratos entre subsistemas": the diagram now shows
    ChromaDB explicitly with arrows from S2 (write) and to S3 (read);
    the "solo a través de Zotero" claim softened to "ChromaDB is the
    one exception — S2-owned derived state".
  - `.env.example`: `S2_CHROMA_PATH` comment inverted to mark `:rw`
    ownership; added `S2_MAX_EMBED_PER_CYCLE`, `S2_SAFE_DELETE_RATIO`,
    `S2_MAX_COST_USD_BACKFILL` block with cross-references to ADR 015.
  - `config/scoring.yaml`: added `semantic_scoring.min_corpus_size: 50`.
  - `src/zotai/cli.py`: stubbed `zotai s2 backfill-index` and
    `zotai s2 reconcile` (point to Phase 11 / #12 like the rest of S2
    stubs); enriched the `s2 fetch-once` docstring to call out the
    reconcile step.
  - No Python runtime code modified — purely editorial + CLI stubs +
    one YAML key. Tests still pass (115). Code module + empirical
    validation come in subsequent PRs per the orden de trabajo.
- **S2 composite score — ADR 016**: `score_composite` now defaults to
  Reciprocal Rank Fusion (RRF, Cormack/Clarke/Büttcher 2009) instead
  of the previous linear weighted mean. Fixes two problems with the
  old default: (1) `w_t=1, w_s=w_q=2` were arbitrary pre-data
  guesses; (2) the weighted mean buried papers with high score on one
  criterion and low on the others — exactly the "out-of-distribution
  but matches a persistent query" case S2 is supposed to surface.
  Changes: `config/scoring.yaml` gains `composite_score.method: rrf`
  (with `rrf_k: 60`); the legacy `weights:` block stays as opt-in
  when `method=weighted_mean`. `plan_02` §7.4 rewritten with RRF
  pseudocode + calibration-deferred note explaining why RRF today and
  a logistic-regression calibration via a successor ADR once ≥100
  triage decisions exist. Dashboard `/inbox` (Phase 12) also exposes
  per-criterion sort so a "queries=#1" paper can be surfaced
  independently. Docs + config only; runtime lands with Sprint 2
  (#13).

- **Architecture — ADR 015**: S2 becomes the owner of the ChromaDB
  embeddings index; S3 (`zotero-mcp serve`) is reduced to a pure
  reader. The project no longer invokes `zotero-mcp update-db` in
  any operational flow. S2's worker runs a reconciliation cycle
  (diff-based add / safe-guarded delete) before each fetch, and a new
  `zotai s2 backfill-index` command handles the initial population.
  This inversion fixes: cross-platform cron fragility, silent
  staleness between ad-hoc `update-db` runs, and the host/container
  coreography that the previous design required. The decision
  supersedes portions of ADR 006 and ADR 009, and amends ADR 011
  (bind mount flag `:ro` → `:rw`). This PR lands only the ADR file
  and the ADR 011 amendment; the ripple of doc alignments across
  plans / glossary / CLAUDE.md / .env.example / scoring.yaml, the
  empirical `zotero-mcp` schema validation, and the code module land
  in follow-up PRs per the orden de trabajo.
- **S1 Stage 03 dedup** (ADR 014): when Ruta A finds that a DOI is
  already in the user's Zotero library, the stage now checks the
  existing item for PDF attachments before adding ours.
  - If the existing item already has a PDF child → skip attach,
    `ImportRow.status = "deduped"`. Preserves the user's curated
    state; avoids duplicated PDF children under one item.
  - If the existing item has no PDF (metadata-only prior import) →
    attach as before, `ImportRow.status = "deduped_pdf_added"`.
  Both statuses count toward `items_deduped` and `items_route_a`; the
  CSV surfaces which branch ran via the `status` column. HTML
  snapshots and non-PDF attachments do not count as "has PDF". New
  `ZoteroClient.children(item_key)` helper; three new tests
  (`_attaches_when_existing_has_no_pdf`,
  `_skips_attach_when_existing_has_pdf`,
  `_skips_non_pdf_attachment`); `plan_01` §3 Etapa 03 Edge cases line
  updated to reference the policy.
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
