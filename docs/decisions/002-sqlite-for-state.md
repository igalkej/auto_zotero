# ADR 002 — SQLite for pipeline state

**Status**: Accepted
**Date**: 2026-04-20
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

The S1 pipeline tracks ~1000 PDFs across six stages (inventory → OCR →
import → enrich → tag → validate). Each stage reads where the previous one
left off and writes its own outputs. The pipeline must be **idempotent**
(re-runnable without corrupting state) and **resumable** (interruption in
stage 04 must not repeat stages 01–03). S2 has a parallel but independent
store for candidates, feeds, triage metrics, and persistent queries.

The process runs inside a Docker container, on a single researcher's
laptop, with no concurrent writers. Read/write volume is modest:
low-thousands of rows per table, writes batched at stage boundaries, reads
dominated by "give me all items at `stage_completed=3`".

## Decision

Use **SQLite** as the persistence layer for both `state.db` (S1) and
`candidates.db` (S2). Access via `sqlmodel` (= SQLAlchemy + pydantic).
Schema migrations via `alembic`.

- One file per database (`state.db`, `candidates.db`), mounted as Docker
  volumes so they survive container rebuilds.
- The two DBs are disjoint by design: S1 tables are grouped in `S1_TABLES`
  and S2 tables in `S2_TABLES` in `src/zotai/state.py`; each `init_s1` /
  `init_s2` helper only creates its own slice. This enforces the
  "communication between subsystems only via Zotero" contract from
  `plan_00` §3.
- Alembic's `target_metadata` is the shared `SQLModel.metadata`; migrations
  are authored against the S1 schema. S2 tables are created on dashboard
  startup and evolve in code in v1 (see `plan_02` §5).

## Consequences

### Positive

- **Zero setup.** No server process, no port, no auth, no role management.
  The "database" is just a file; `docker-compose up` is all that is needed.
  This matches the α scenario (see ADR 003) where each user owns a
  single-machine instance.
- **Inspectable.** The user can open `state.db` in the `sqlite3` CLI,
  DBeaver, VSCode extensions, or `datasette` to audit state without any
  project-specific tooling. This is load-bearing for trust: users must be
  able to see what the pipeline did.
- **Idempotence is trivial.** PDF identity is `SHA-256(bytes)`, used as
  `Item.id` (primary key). Re-running stage 01 produces zero inserts,
  because every PDF already has a row. No "upsert" ceremony required.
- **Transactions.** SQLite gives us atomic stage transitions for free; a
  `KeyboardInterrupt` mid-stage leaves the DB in the pre-stage state.
- **Embedded.** The DB travels with the Docker volume. Backup = copy a
  file. Restore = move the file back.
- **SQLModel gives one type per row.** Pydantic validation + typed ORM
  rows + `mypy --strict` all line up with minimal ceremony.

### Negative

- **Not suitable for multi-writer workloads.** We explicitly do not have
  those (single-user, single-machine). If scenario β (shared library) is
  ever adopted, this ADR must be revisited.
- **Schema changes require alembic discipline.** Adding a column post-v1
  needs a migration, not just an `ALTER TABLE` in code. See issue #24
  (classifier columns) for the first real exercise.
- **Concurrent access from the FastAPI dashboard + the APScheduler worker
  (both in S2)** must be handled with SQLite's default WAL mode; this is a
  one-line configuration, but it is a constraint to remember.
- **No native JSON operators** (SQLite has `json_*` but they require the
  JSON1 extension; it is bundled on modern builds but pinning matters).
  We store JSON blobs in `metadata_json` / `tags_json` / `authors_json`
  TEXT columns and decode in Python.

### Neutral

- The schema is small (four tables in S1, five in S2). SQLite is more than
  enough; Postgres would be overkill for current scale.

## Alternatives considered

**A. Postgres (dockerized).**
Rejected. Adds a second service to `docker-compose`, a second volume,
connection pooling, role management, and a port exposure — none of which
buys us anything while we remain single-writer. Would be the right choice
if we ever adopted scenario β (shared library, multiple users pushing via
S2); at that point this ADR gets revisited.

**B. Flat files (JSON / Parquet).**
Rejected. We lose atomic transactions, we gain custom locking, and every
"give me items at stage 3" becomes a full scan and parse. SQLite solves
exactly this problem.

**C. DuckDB.**
Rejected for now. DuckDB is excellent for analytical reads but the
pipeline's hot path is OLTP-ish: per-item inserts, per-item updates,
per-stage counts. SQLite is purpose-built for that. DuckDB could help
later for the Etapa 06 validation report if it ever outgrows
pandas/SQL, but that is a separate decision.

**D. Redis / in-memory with periodic snapshots.**
Rejected. Redis is a service; the point of this ADR is to avoid services.
"In-memory with snapshots" is what SQLite is, only worse.

## References

- `CLAUDE.md` §Stack canónico — "Storage local: SQLite para estado del
  pipeline"
- `docs/plan_00_overview.md` §5 row 002
- `docs/plan_01_subsystem1.md` §4 (schema + alembic)
- `docs/plan_02_subsystem2.md` §5 (candidates.db schema)
- `src/zotai/state.py` — `S1_TABLES` / `S2_TABLES` grouping, `init_s1` /
  `init_s2` helpers
