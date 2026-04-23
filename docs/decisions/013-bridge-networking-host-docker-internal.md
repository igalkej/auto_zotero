# ADR 013 — Bridge networking + `host.docker.internal` instead of `network_mode: host`

**Status**: Accepted
**Date**: 2026-04-22
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

Both Compose services (`onboarding` for S1, `dashboard` for S2) need
to reach **Zotero Desktop's local API** at `localhost:23119` on the
user's host — that is the transport that `pyzotero` with `local=True`
uses (verified by reading `pyzotero.zotero.Zotero.__init__`: it
hardcodes `self.endpoint = "http://localhost:23119/api"`).

The initial `docker-compose.yml` solved "reach the host's
`localhost:23119`" by setting `network_mode: host` on both services.
That works on Linux. It does **not** work on Docker Desktop for Mac or
Windows: Docker's documentation is explicit that `network_mode: host`
is a no-op there (the container still runs inside Docker Desktop's
Linux VM and does not get the macOS/Windows host's loopback). CLAUDE.md
declares Windows/WSL2 and Linux as primary targets plus macOS as
"nice-to-have", and the project's README promises cross-platform
distribution as the motivation for Docker (ADR 001) in the first
place. `network_mode: host` silently defeats that promise for two of
the three target OSs.

A secondary issue: under `network_mode: host`, the `ports:
- "127.0.0.1:8000:8000"` entry on the `dashboard` service is a no-op
(the container is already on the host network). Readers of the Compose
file could not tell which of the two mechanisms was actually binding
the port, and the compose was self-contradictory on Mac/Windows where
neither path worked.

We need one configuration that reaches the host's Zotero API
identically on Linux, macOS, and Windows, without adding per-OS
overrides.

## Decision

**Drop `network_mode: host`. Use default bridge networking with
Docker's cross-platform `host.docker.internal:host-gateway` alias, and
route `pyzotero` to it via a new `ZOTERO_LOCAL_API_HOST` env var that
`ZoteroClient` honours.**

Concretely:

1. **Compose** (`docker-compose.yml`):
   - Remove `network_mode: host` from both services.
   - Add `extra_hosts: - "host.docker.internal:host-gateway"` to both.
     Docker 20.10+ (released 2020-12) implements the `host-gateway`
     magic on Linux; Docker Desktop for Mac and Windows define
     `host.docker.internal` natively. The alias resolves to the host's
     reachable IP on all three OSs.
   - Set `environment.ZOTERO_LOCAL_API_HOST:
     ${ZOTERO_LOCAL_API_HOST:-http://host.docker.internal:23119}`. The
     `${VAR:-default}` syntax lets a user override in `.env` or their
     shell without editing Compose.
   - Change the `dashboard` container's uvicorn bind from
     `--host 127.0.0.1` to `--host 0.0.0.0`. Under bridge networking
     the container's loopback is not the host's loopback, so binding
     uvicorn to `127.0.0.1` inside the container would leave the
     `ports: - "127.0.0.1:8000:8000"` mapping unreachable. The host
     side of the mapping remains `127.0.0.1`, so the dashboard is
     still localhost-only from the user's perspective.

2. **Settings** (`src/zotai/config.py`):
   - Add `ZoteroSettings.local_api_host: str = ""`. Empty default
     means "use pyzotero's hardcoded `localhost:23119`" — which is
     correct when running outside Docker (tests, direct host
     execution).

3. **Client** (`src/zotai/api/zotero.py`):
   - `ZoteroClient.__init__` takes a `local_api_host: str | None`
     parameter. When `local=True` and a host is given, override
     pyzotero's `self._client.endpoint` to `"{host}/api"`. The
     override is one assignment after pyzotero's normal init — no
     fork, no subclass, no monkey-patching of pyzotero's classes.

4. **Example env** (`.env.example`):
   - Add `ZOTERO_LOCAL_API_HOST=` with a comment explaining that the
     Compose layer sets the default inside the container and the user
     overrides it here only if Zotero lives elsewhere.

## Consequences

### Positive

- **One Compose file, three OSs.** The default `docker compose up
  dashboard` works on Linux, macOS, and Windows with Docker Desktop.
  No per-OS overrides, no conditional docs, no "Linux-only" asterisk.
- **`ports:` mapping is now truthful.** Under bridge networking the
  mapping is what binds the dashboard to `127.0.0.1:8000` on the host.
  A reader of the Compose file can follow the wiring in one place.
- **Host-side reach is explicit.** `ZOTERO_LOCAL_API_HOST` is the one
  knob users turn if they put Zotero on a different host (rare, but
  trivially supported).
- **Non-Docker execution keeps working.** With `local_api_host=""`,
  `ZoteroClient` leaves pyzotero's default untouched. Tests that
  instantiate `ZoteroClient` directly, or users running the CLI on
  the host, reach `localhost:23119` as before.
- **S3 unaffected.** `zotero-mcp` runs on the host (ADR 006, plan_03
  §5), not inside Docker — it does not need this wiring and this ADR
  does not touch it.

### Negative

- **Requires Docker ≥ 20.10.** Released 2020-12; safe on any
  reasonably-current install (Docker Desktop has not shipped a
  version without `host-gateway` in years). Documented as the minimum
  in `docs/setup-{linux,windows}.md` (Phase 9 deliverable).
- **Reaches the host via its default gateway, not via loopback.**
  With `network_mode: host` the container literally was the host from
  a network point of view. Under bridge mode it is one hop away, on
  the docker bridge's gateway IP. The user's firewall must allow the
  container → host traffic on port 23119; on all three target OSs the
  default Zotero install binds on `0.0.0.0:23119` and the Docker
  bridge traffic is permitted by default. Worth calling out in the
  troubleshooting doc so a user with a locked-down host firewall
  knows where to look.
- **Zotero Desktop must listen on an address the bridge can reach.**
  Zotero's default is `0.0.0.0:23119`, so this is met without user
  action. If a future Zotero release starts binding only to
  `127.0.0.1`, we would need a socat sidecar. Not a concern today but
  worth naming.
- **The `pyzotero` endpoint override is a post-init assignment to an
  attribute that pyzotero treats as public-ish but never documented
  as a public API.** If pyzotero refactors to make `endpoint` a
  property or moves the URL composition, our override breaks. Tests
  pin the current behaviour (the override test fails loudly if
  pyzotero ever changes). A future fork of pyzotero, or contributing
  an upstream `local_host` parameter, would be cleaner.

### Neutral

- **Reversible.** If a future decision puts S2 back on host
  networking (e.g. to colocate with a host-bound service), dropping
  `extra_hosts` and adding `network_mode: host` back is a one-line
  change. The `ZoteroClient.local_api_host` override stays correct
  either way.

## Alternatives considered

**A. Keep `network_mode: host`; narrow CLAUDE.md to Linux-only.**
Rejected. ADR 001's whole argument for adopting Docker is
cross-platform distribution; abandoning Mac/Win contradicts that
decision and shrinks the audience the project promised to serve.

**B. Per-OS Compose overrides (`docker-compose.linux.yml` +
`docker-compose.desktop.yml`) via `COMPOSE_FILE`.**
Rejected. Doubles the Compose surface for a problem that one
`extra_hosts` entry solves. Documentation would have to explain which
file to use when, and the overrides drift over time. Not worth it.

**C. `socat` sidecar forwarding `localhost:23119` inside the
container to `host.docker.internal:23119`.**
Rejected. Adds a second process per service and a new failure mode
(sidecar crash), for no benefit over `extra_hosts` + endpoint
override. Pointless when pyzotero already exposes `self.endpoint` for
post-init adjustment.

**D. Run Zotero Desktop in a container next to ours.**
Rejected. Zotero Desktop is a user-facing GUI application; it is not
distributed as a container image and is explicitly meant to run on
the user's desktop with their personal library. Containerising it
would also break S3 (Claude Desktop talks to `zotero-mcp`, which
talks to Zotero Desktop on the host).

**E. Subclass `pyzotero.Zotero` with a configurable endpoint.**
Rejected as premature. A post-init assignment achieves the same result
with one line and no subclassing. If this becomes load-bearing (many
call sites, many overrides), a proper subclass or a pyzotero upstream
PR is the right move then. Today it is not.

## References

- `docs/decisions/001-use-docker.md` — the ADR this one preserves
  the intent of: cross-platform distribution as the motivation for
  Docker in the first place
- `docs/plan_01_subsystem1.md` §3 Etapa 03 — Stage 03's reliance on
  Zotero local API
- `docker-compose.yml` — the compose change that lands with this ADR
- `src/zotai/api/zotero.py` — `ZoteroClient.__init__` `local_api_host`
  parameter
- `src/zotai/config.py` — `ZoteroSettings.local_api_host`
- `.env.example` — `ZOTERO_LOCAL_API_HOST` user-facing override
- Docker networking reference:
  https://docs.docker.com/reference/compose-file/services/#extra_hosts
  (host-gateway special alias)
- `pyzotero.zotero.Zotero.__init__` — the hardcoded
  `http://localhost:23119/api` endpoint we override
