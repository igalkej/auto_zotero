# ADR 012 — APScheduler in-process is the default; cron/Task Scheduler is the documented alternative

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

S2's worker (plan_02 §9) runs one fetch cycle every N hours: pull
configured RSS feeds, deduplicate, enrich, score, persist. There are
two plausible ways to get the cycle to run on a schedule:

- **In-process**: APScheduler living inside the dashboard's FastAPI
  app, firing the `run_fetch_cycle()` coroutine on an interval. The
  dashboard and worker share one Python process, one set of DB
  connections, and one Compose service.
- **Out-of-process**: an OS-level scheduler (cron on Linux/macOS,
  Task Scheduler on Windows) invoking `docker compose run --rm
  onboarding zotai s2 fetch-once` at the desired cadence, independent
  of whether the dashboard is up.

plan_02 §9 named APScheduler as "decisión preliminar" without fully
committing, leaving the choice floating. The review that produced this
ADR found that scenario α (single researcher, heterogeneous usage
patterns) does not have one right answer: some users keep the
dashboard open continuously and want everything bundled; others open
the dashboard once a week for the triage session and want fetches to
happen regardless.

## Decision

**APScheduler in-process is the default. cron / Task Scheduler is
documented as an alternative for users whose dashboard is not up 24/7.
Both paths call the same `zotai s2 fetch-once` code path.**

Concretely:

1. **Default behaviour (APScheduler).** When the `dashboard` Compose
   service starts, `src/zotai/s2/worker.py` registers a job with
   APScheduler that invokes `run_fetch_cycle()` every
   `S2_FETCH_INTERVAL_HOURS` (default 6). This is the single-command
   experience advertised in the README quickstart (`docker compose up
   dashboard`). Both the worker and the FastAPI routes share one
   process, one `candidates.db` connection pool, one set of env vars.

2. **Opt-out switch.** Setting `S2_WORKER_DISABLED=true` in `.env`
   prevents APScheduler from registering the job. The dashboard still
   serves `/inbox`, but the job never fires from inside the container.
   This is the switch a user flips when they prefer to drive fetches
   externally.

3. **Alternative path (cron / Task Scheduler).** With
   `S2_WORKER_DISABLED=true`, users install an OS-level scheduler job
   that runs:

   ```bash
   docker compose run --rm onboarding zotai s2 fetch-once
   ```

   `docs/setup-linux.md` ships a `crontab -e` example; `docs/setup-
   windows.md` ships a Task Scheduler XML snippet. Both Phase 9 (#10)
   deliverables.

4. **Single implementation.** `zotai s2 fetch-once` (exposed via the
   CLI in `cli.py` stub form since Phase 1, wired in Phase 11 #12) and
   APScheduler's in-process callback both call the same
   `run_fetch_cycle()` function. There is no duplicated scheduling
   logic. Anything that changes in the fetch cycle (rate limits, error
   handling, budget enforcement) applies to both paths uniformly.

5. **Dashboard exposes `/worker/run-now`.** Per plan_02 §8.1, the
   dashboard surfaces a button that triggers an immediate fetch cycle.
   This works regardless of whether APScheduler is enabled — it calls
   `run_fetch_cycle()` directly in a background task — so a user who
   has disabled APScheduler can still force a fetch from the UI when
   they want one.

## Consequences

### Positive

- **Defaults match the 80% case.** Most researchers leave the
  dashboard running; for them, "it just works" is the APScheduler
  path and there is nothing to configure.
- **Power users are not blocked.** A user on a laptop that they close
  nightly can get reliable fetches by flipping one env var and
  setting up cron once. No custom S2 support code required.
- **One code path for fetch logic.** Tests cover
  `run_fetch_cycle()` directly; both schedulers are thin wrappers
  around it. A bug fixed in the logic is fixed everywhere.
- **Scheduler choice is observable.** A
  `/metrics` panel (plan_02 §8.1) surfaces which path is active (
  "APScheduler (next run in 2h 15m)" vs "External scheduler
  (last fetch: 4h ago)"), so the user always knows which knob to
  twist.
- **Matches Compose lifecycle.** APScheduler stops cleanly when the
  dashboard container stops (FastAPI's lifespan callback kills the
  scheduler), so there is no orphaned worker process.

### Negative

- **APScheduler default has a failure mode.** If the dashboard crashes
  overnight and Docker's `restart: unless-stopped` does not bring it
  back (e.g. the user `docker compose down`'d it), fetches silently
  stop. The `/metrics` panel catches this on the user's next visit,
  but there is no active notification. For v1, that is acceptable —
  S2 is a weekly-touch workflow, not a SRE-monitored service. Adding
  alerting is out of scope (plan_02 §14).
- **Two code paths to document.** `docs/s2-user-guide.md` (Phase
  14 #15) must cover both. Users have one more knob to think about
  than a single-scheduler design would have required.
- **Cross-container invocation from cron.** On Windows, Task Scheduler
  launching `docker compose run ...` has to inherit the right
  environment (Docker Desktop running, WSL available). We document
  the gotcha in `docs/setup-windows.md` and accept it as part of the
  Windows installation complexity that ADR 001 already absorbs.

### Neutral

- **Scheduler choice is reversible.** Flipping between the two paths
  requires no data migration — both produce the same rows in
  `candidates.db`. A user can start with APScheduler and switch to
  cron later (or vice versa) with a single env var change.

## Alternatives considered

**A. APScheduler only, no cron path.**
Rejected. Breaks the use case of users who run the dashboard on
demand. They would have to keep it running 24/7 just to get the
background fetches, which doesn't match how the triage workflow is
meant to be used (15-20 min/week, not continuously).

**B. cron/Task Scheduler only, no in-process scheduler.**
Rejected. Requires every user to write an OS-level scheduled task
during setup. For a user on Windows who does not know Task Scheduler,
this is a meaningful onboarding hurdle — and more importantly, the
"default experience" in the README quickstart becomes 5+ lines of
setup instructions per OS. ADR 001 explicitly trades off in favour of
"fewer manual steps" for the default path.

**C. A separate worker service in Compose with its own container.**
Rejected. Two containers sharing `candidates.db` is a write
contention risk (SQLite concurrent writes), adds a second service to
the Compose file, and duplicates the env-var surface. The in-process
approach avoids all three.

**D. systemd timer units (Linux-only).**
Rejected as the primary path. systemd timers are more robust than
cron but do not help Windows users, and the target audience includes
both OSes. Documented as a footnote in `docs/setup-linux.md` for
users who prefer systemd over cron.

**E. `S2_WORKER_MODE={apscheduler|cron|disabled}` enum instead of a
boolean.**
Rejected. The enum adds a third state ("cron") that behaves
identically to `disabled` from the container's perspective — the
scheduler code does nothing either way. The difference (cron is
running on the host) is external to the container. A single boolean
keeps the container's behaviour simple and pushes the host-side
detail where it belongs, in the setup docs.

## References

- `docs/plan_02_subsystem2.md` §9 (worker), §8.1 (`/worker/run-now`),
  §15 (dependencies)
- `docs/decisions/001-use-docker.md` — the distribution boundary ADR
  that this one builds on
- Issue #15 — Phase 14 S2 Sprint 4 will wire APScheduler; the cron
  recipe is a Phase 9 (#10) deliverable
- `src/zotai/cli.py` — `zotai s2 fetch-once` command wiring (stub as
  of Phase 1; implementation in Phase 11 #12)
