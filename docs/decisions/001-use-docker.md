# ADR 001 — Docker as the distribution medium

**Status**: Accepted
**Date**: 2026-04-20
**Deciders**: project owner
**Supersedes**: —
**Superseded by**: —

---

## Context

The target audience is researchers (α scenario — see ADR 003): each runs the
toolkit against their own personal Zotero library, on their own machine. The
primary platform split is **Windows 10/11 + WSL2** and **Linux (Ubuntu
22.04+)**; macOS is nice-to-have. Python skill levels are mixed; none of them
want to fight `tesseract`, `ghostscript`, `poppler`, `ocrmypdf`, a specific
Python minor, and three native compilers before their first run.

Onboarding time is the scarcest resource in this project. If the first
experience is 90 minutes of `apt install`, `brew` errors, `WSL` path
confusion, and `pyenv` vs. system Python disputes, the toolkit simply does
not get adopted.

## Decision

Ship the toolkit as a **Docker image** orchestrated by `docker-compose`.

- Multi-stage `Dockerfile`: builder stage with compilation toolchain,
  final stage minimal (no dev headers, no uv cache).
- Non-root user `zotai` (UID 1000) in the final stage.
- System deps baked in: `tesseract-ocr`, `tesseract-ocr-spa`,
  `tesseract-ocr-eng`, `ocrmypdf`, `ghostscript`, Poppler libs.
- Python deps managed by `uv` inside the image.
- `docker-compose.yml` is the user-facing interface; `onboarding` and
  `dashboard` are the two services (S1 one-shot vs. S2 long-running).
- Volumes for the only state that must persist: `state.db`, `candidates.db`,
  `staging/`, `reports/`, and the ChromaDB shared with `zotero-mcp`.

The `README.md` quickstart assumes Docker Desktop (Windows/macOS) or Docker
Engine (Linux) is already installed. Installation of Docker itself is out of
scope for this project's docs.

## Consequences

### Positive

- **Reproducible builds**: `uv.lock` + `Dockerfile` freeze the entire stack,
  including native OCR tooling. Two users on different OSes get the same
  binary.
- **Onboarding time collapses** from hours to one `docker compose run` line.
- Native build complexity (`ocrmypdf` → `tesseract` → language packs)
  becomes an image-build concern, not a user concern.
- Cross-platform path issues (`/` vs. `\`, case sensitivity) are resolved
  once inside a Linux container — host paths only show up at volume mount
  boundaries.
- We can pin a single Python minor (3.11) independently of whatever the
  user has globally installed.

### Negative

- **Docker Desktop is a hard prerequisite** (free for personal and research
  use, but users must install it). This is accepted; the target audience
  has shown willingness.
- **Volume permission mismatches** on Linux hosts (UID 1000 inside the
  container vs. the host user's UID) require documentation but are a
  one-line fix (`--user $(id -u):$(id -g)`).
- **Image size** (~1.2 GB with Tesseract + language packs) — acceptable for
  a toolkit a user installs once.
- `zotero-mcp` (S3) cannot easily run inside the same container because
  Claude Desktop spawns it as a subprocess on the host. This is not a
  regression of Docker; it is a property of MCP's stdio transport. S3 is
  installed on the host via `uv tool install` (see `plan_03`).

### Neutral

- We do not ship a native binary, wheel, or pip package. Users who really
  want non-Docker local installs can clone the repo and run `uv sync`
  themselves, but that path is not supported.

## Alternatives considered

**A. `pip install zotero-ai-toolkit` (pypi package)**.
Rejected. Leaves users to install `tesseract`, `poppler`, `ghostscript`
themselves; adoption friction defeats the purpose. Pypi would still be
useful as a secondary distribution once Docker-first is proven.

**B. Native scripts per OS (`.sh`, `.ps1`) installing system deps**.
Rejected. Multiplies surface area: every package manager (`apt`, `brew`,
`choco`, `winget`) behaves differently, language packs have different
names, and the matrix explodes fast. `CLAUDE.md` explicitly bans bash
scripts as a cross-platform strategy for this reason.

**C. Conda / mamba environment**.
Rejected. Conda handles some native deps (tesseract is available on
conda-forge) but adds its own bootstrap step and its own class of
environment confusion. `uv` is the project's declared dependency manager
(see `CLAUDE.md` §Stack canónico); mixing uv + conda is worse than either
alone.

**D. `nix` flakes**.
Rejected on audience grounds. The target user base is not going to install
Nix to run a Zotero loader.

## References

- `CLAUDE.md` §Stack canónico, §Reglas sobre Docker
- `docs/plan_00_overview.md` §5 row 001
- `Dockerfile`, `docker-compose.yml`, `.dockerignore`
