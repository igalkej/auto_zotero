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

- **Spec**: `docs/plan_02_subsystem2.md` §10 — replaced the single-line PDF
  fetch priority (which excluded Sci-Hub as "illegal") with an explicit
  six-source cascade: OpenAlex OA URL → DOI resolver → Anna's Archive →
  Library Genesis → Sci-Hub → RSS URL. Each source is toggleable via
  `S2_PDF_SOURCES`. Rationale: the tool is local and personal, and the
  target audience relies on these services as standard fallbacks when
  institutional access falls short. Issue #16.
