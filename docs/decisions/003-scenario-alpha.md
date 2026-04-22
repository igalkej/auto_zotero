# ADR 003 — Distribution scenario α (personal libraries, shared repo)

**Status**: Accepted
**Date**: 2026-04-20
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Early in project design we named three candidate distribution scenarios for
the toolkit:

- **α — shared repo, personal libraries.** A small group of researchers
  share this git repo. Each clone the repo and run the toolkit against
  their own Zotero library. No central server, no shared library, no
  cross-user state.
- **β — shared repo, shared Zotero group library.** Same repo, but the
  target Zotero library is a Zotero Groups library that several
  researchers push into (S2) and read from (S3).
- **γ — hosted service.** A cloud-hosted instance that users authenticate
  into and that processes their libraries remotely.

Each scenario changes load-bearing decisions: multi-user auth, conflict
handling on S1 re-ingestion, shared cost/budget attribution, PII / research
ethics at rest, SLA implications, who pays OpenAI bills, etc.

We need to pick one, now, so the rest of the architecture can stop
hedging.

## Decision

Adopt **scenario α** as the single supported distribution mode for v1 and
design toward it without compromise.

Concretely this means:

- Each researcher owns their own Zotero library and their own `state.db` /
  `candidates.db` / ChromaDB.
- There is no concept of users, roles, or permissions in the code. The
  implicit user is "whoever has the filesystem and the API keys".
- The dashboard binds to `127.0.0.1` and has no authentication (see
  `plan_02` §8.3).
- API keys (Zotero, OpenAI) live in each user's local `.env`. `.env` is
  `.gitignored`; only `.env.example` is versioned.
- Budgets are per-user, enforced locally. There is no aggregate spend view
  across users.
- Each user customises `config/taxonomy.yaml`, `config/feeds.yaml`, and
  `config/scoring.yaml` in their own clone. Merging those back is not a
  project concern — users diverge on purpose.
- All three subsystems communicate only via Zotero (`plan_00` §3). Even
  within scenario α, the loose coupling is retained because it is the
  right shape regardless.

## Consequences

### Positive

- **Radical simplification.** No auth story, no RBAC, no tenant model, no
  shared-state conflicts, no "whose budget did that embed call charge
  against". Entire problem categories evaporate.
- **Privacy by default.** Each user's PDFs and metadata never leave their
  machine (except to the APIs they explicitly configure — OpenAI,
  OpenAlex, Semantic Scholar, Zotero). No multi-tenant data isolation
  concerns because there is only one tenant.
- **Per-user cost model matches per-user value.** Each researcher pays
  their own OpenAI bill and gets their own tailored library. No
  attribution problem.
- **Onboarding is linear:** `git clone` → `cp .env.example .env` →
  `docker compose run onboarding zotai s1 run-all`. No "request access"
  step.
- **The three subsystems stay decoupled.** S2 and S3 can run on users who
  never ran S1 (they degrade gracefully); S1 can run without S3 ever
  having been installed. This is a direct consequence of "communication
  only via Zotero" and is preserved by scenario α by construction.

### Negative

- **No knowledge sharing across researchers.** If two users in the same
  lab curate their libraries separately, each does its own triage work in
  S2 and its own tagging in S1. There is no "Alice accepted this paper,
  so Bob sees it pre-vetted." Accepted; this is a v1.1+ feature in
  scenario β.
- **Feed lists and taxonomies diverge.** Users who want to keep their
  `config/` files aligned must do so manually (a shared Slack snippet, a
  fork, a config repo). Not a project concern.
- **No operational visibility across the group.** If three users all hit
  budget issues, nothing aggregates that.
- **α is not a migration path to β/γ for free.** Moving to β requires
  rethinking auth, concurrent writes to one Zotero library, de-duplication
  across user pushes, and shared budget policy. Some of that work will be
  wasted if β is ever adopted, but less than the inverse (designing for β
  upfront and never using it).

### Neutral

- Scenario γ is explicitly out of scope. If the project ever offers a
  hosted mode, it will be a new product, not a refactor of this one.

## Alternatives considered

**A. Scenario β (shared library) from v1.**
Rejected. Requires auth (Basic Auth minimum, OIDC realistically), a
policy for conflicting pushes to the same DOI, shared budget accounting,
write permissions to one library from multiple users, and probably a
central server to arbitrate. That is 3–6× the scope of v1. The underlying
user need ("I want my lab to share a library") can also be served by
Zotero Groups directly, without this toolkit; our S2 could be pointed at
a Zotero Groups library later with modest adjustment.

**B. Scenario γ (hosted).**
Rejected. Requires operational commitment (uptime, billing, compliance,
PII handling at rest for other people's PDFs) that is not what this
project is. The product is a toolkit, not a service.

**C. "Supports all three, user picks at runtime".**
Rejected. Every degree of freedom in distribution mode bloats the code and
the test matrix. Pick one, build it well; migrate if demand appears.

## References

- `CLAUDE.md` §Identidad del proyecto — "Escenario de distribución: α"
- `docs/plan_00_overview.md` §5 row 003
- `docs/plan_02_subsystem2.md` §8.3 (dashboard auth posture)
- `docs/plan_03_subsystem3.md` §1 (S3 runs on the user's host)
