#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# scripts/run-pipeline.sh — Linux / macOS wrapper for S1 run-all.
#
# Runs ``docker compose run --rm onboarding zotai s1 run-all`` with
# sensible defaults. Any extra arguments are forwarded to the CLI
# verbatim — use e.g.:
#
#     scripts/run-pipeline.sh --yes --tag-mode preview
#
# Prerequisites:
#   * Docker Engine or Docker Desktop running.
#   * Zotero 7 desktop open (local API reachable on localhost:23119).
#   * .env filled in — see .env.example and docs/setup-linux.md.
#
# See docs/troubleshooting.md if the run aborts mid-stage.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

# Resolve the repo root relative to this script — works whether the
# user invoked it from repo root or any subdirectory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

if [[ ! -f .env ]]; then
    echo "⚠️  .env not found at ${REPO_ROOT}/.env"
    echo "    Copy .env.example → .env and fill in your credentials."
    exit 1
fi

# `--rm` keeps the one-shot container from sticking around after the run.
# The `onboarding` profile must be active; without it the service is
# hidden from `docker compose run` (plan_00 + docker-compose.yml).
exec docker compose --profile onboarding run --rm onboarding \
    zotai s1 run-all "$@"
