# ADR 011 — ChromaDB is shared via a Docker bind mount, not duplicated

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

ChromaDB is written by `zotero-mcp` (Subsystem 3) on the host and read
by Subsystem 2 from inside the Docker container. Before this ADR, the
two specs named it with two different paths:

- `plan_02_subsystem2.md` §12 used `S2_CHROMA_PATH=/workspace/chroma_db`
  — a container-side path, consistent with the `/workspace/*`
  convention already used for `state.db`, `candidates.db`,
  `staging/`, and `reports/`.
- `plan_03_subsystem3.md` §4.3 and §8 used
  `~/.config/zotero-mcp/chroma_db/` — the host path where
  `zotero-mcp setup` plants its index by default, and where the
  `zotero-mcp update-db` cron job keeps writing.

Both statements were true — each subsystem saw the store from its own
vantage point — but read together they implied two different file
paths, and did not say how they become the same ChromaDB. A careful
reader of the specs could not answer "what mounts where" without
guessing.

The two requirements this ADR has to reconcile:

1. S1/S2 containers should name paths as `/workspace/*` (ADR 002 /
   CLAUDE.md Docker rules). Breaking that convention for one special
   case would make the Compose file harder to read and encourage more
   exceptions.
2. `zotero-mcp` runs on the host (see plan_03 §5 — requires host
   Python 3.11 and a Claude Desktop installation), so whatever location
   it writes to has to be the *real* location on the host filesystem.
   We cannot ask `zotero-mcp` to write into `/workspace/chroma_db`; it
   has no such directory.

## Decision

**The canonical path is `/workspace/chroma_db` inside the container.
The dashboard Compose service bind-mounts the host's zotero-mcp
ChromaDB directory to that container path, read-only for S2.**

Concretely, the dashboard service in `docker-compose.yml` gains a
volume entry (alongside the existing `./workspace:/workspace` and
`./config:/app/config`) of the form:

```yaml
- ${ZOTERO_MCP_CHROMA_HOST_PATH:-${HOME}/.config/zotero-mcp/chroma_db}:/workspace/chroma_db:ro
```

Rules:

- **Specs and code name the path as `/workspace/chroma_db`.** This is
  what `S2_CHROMA_PATH` defaults to in `.env.example` and what the S2
  code opens.
- **The host-side source is configurable** via
  `ZOTERO_MCP_CHROMA_HOST_PATH` in `.env`, defaulting to
  `${HOME}/.config/zotero-mcp/chroma_db` — the same default path that
  `zotero-mcp setup` uses.
- **The mount is read-only (`:ro`).** S2 is a consumer of the index,
  not a writer. `zotero-mcp` owns writes. If S2 ever tries to write,
  Docker fails loudly instead of silently producing an S2-only copy
  that drifts from the S3 index.
- **The `onboarding` service does not mount it.** S1 has no reason to
  touch ChromaDB; keeping the mount off that service avoids surprising
  the user during the first run (when ChromaDB may not yet exist on
  the host).
- **Empty / missing ChromaDB degrades gracefully.** If the host path
  does not exist yet (user has not run S1 → S3 setup in order), S2's
  `score_semantic` returns `0.5` (neutral) per plan_02 §7.3, and the
  dashboard keeps working. The user sees a one-time warning pointing
  at `docs/s3-setup.md`.

The Compose wiring itself lands in Phase 9 (#10) as part of Docker
finalization; Phase 11 (#12) adds the degradation warning in the
dashboard. This ADR fixes the pattern so those phases do not relitigate
it.

## Consequences

### Positive

- **One canonical name.** Every place in the repo that names this
  index — specs, `.env.example`, code, dashboard error messages — says
  `/workspace/chroma_db`. There is no second path to keep in sync.
- **No copy, no sync job.** The S3 writer and the S2 reader look at the
  same bytes. When `zotero-mcp update-db --fulltext` runs nightly, the
  dashboard sees the fresh index on its next query with no additional
  plumbing.
- **S2 cannot corrupt the S3 index.** The read-only mount is enforced
  at the kernel level. A bug in S2 that tries to `chroma_client.add()`
  fails immediately instead of producing a subtle divergence.
- **Host path is overridable.** Users who already have ChromaDB in a
  non-default location (custom `ZOTERO_MCP_HOME`, Windows user with
  OneDrive-redirected `$HOME`, etc.) set
  `ZOTERO_MCP_CHROMA_HOST_PATH` in `.env` without touching Compose.

### Negative

- **Order of execution matters more than before.** If S2 starts before
  S3 has ever run, the bind mount source is missing. Docker Compose
  handles this by creating an empty directory at the source path; the
  S2 code must then treat an empty ChromaDB as "degrade to neutral
  score", not "crash on open". This is already required by plan_02
  §7.3, so the ADR does not add new code — just calls out the tie.
- **Windows/WSL path translation.** Docker Desktop on Windows translates
  `${HOME}/.config/zotero-mcp/chroma_db` correctly when WSL is the
  Docker backend, but users who run Docker Desktop without WSL (rare
  on Windows 11) may need to set `ZOTERO_MCP_CHROMA_HOST_PATH` to a
  Windows-style path. Documented in `docs/setup-windows.md` (Phase 9).
- **`chroma_db` name is now a Compose-managed path name, not a freely
  chosen one.** Renaming it later requires coordinated updates across
  ADR 011, Compose, `.env.example`, and both plans. A rename should go
  through an ADR update.

### Neutral

- **S2 could eventually run on the host too.** If the Compose setup is
  dropped in a future revision, the host-side S2 reads ChromaDB
  directly at its native path and this ADR stops applying. That
  scenario is not in scope for v1; noting it here so a future migration
  can cite ADR 011 as "formerly load-bearing, now obsolete".

## Alternatives considered

**A. Name the path `~/.config/zotero-mcp/chroma_db/` everywhere,
bind-mount into the container at the same path.**
Rejected. Breaks the `/workspace/*` convention inside the container
and creates awkward Compose syntax (`${HOME}/.config/...:${HOME}/.config/...`).
Also couples the container's filesystem layout to the host user's
`$HOME`, which is leaky.

**B. S2 maintains its own ChromaDB and runs a sync job against S3's.**
Rejected. Doubles disk usage, adds a sync job that can drift, and
introduces a third consistency model (S3 index vs S2 copy vs Zotero
library state). The gracefully-degrading "empty means neutral" model
already covers the case S3 had not indexed yet.

**C. S2 does not use ChromaDB at all; compute embeddings on demand per
candidate.**
Rejected. Defeats the purpose of `score_semantic` being cheap (the
index exists precisely to avoid re-embedding the library on every
scoring call). On a 1500-paper library, per-query embedding would cost
~$0.005 per scored candidate vs ~$0 against the cached index — the
nightly full index cost is already paid by S3.

**D. Bind-mount a named volume (`docker volume create zotero-mcp-chroma`)
and document a separate sync step from the host into that volume.**
Rejected. Named volumes are opaque to the host user — `zotero-mcp`
would have to write into a Docker-managed path instead of its default.
That leaks Docker into S3's surface, which ADR 001 explicitly avoids
for anything the user interacts with outside the container.

## References

- `docs/plan_02_subsystem2.md` §7.3, §12, §15
- `docs/plan_03_subsystem3.md` §4.3, §8, §11
- `docs/decisions/001-use-docker.md` — Docker as the distribution
  boundary
- `docs/decisions/002-sqlite-for-state.md` — the parallel case for
  state.db: one canonical path, one writer, consumers read from the
  same file
- `CLAUDE.md` — "Reglas sobre Docker": `/workspace/*` is the canonical
  container path naming
