# ─────────────────────────────────────────────────────────────
# scripts/run-pipeline.ps1 — Windows wrapper for S1 run-all.
#
# Runs `docker compose run --rm onboarding zotai s1 run-all` with
# sensible defaults. Any extra arguments are forwarded to the CLI
# verbatim — use e.g.:
#
#     scripts\run-pipeline.ps1 --yes --tag-mode preview
#
# Prerequisites:
#   * Docker Desktop for Windows running.
#   * WSL2 enabled and integrated with Docker Desktop.
#   * Zotero 7 desktop open (local API reachable on localhost:23119).
#   * .env filled in — see .env.example and docs/setup-windows.md.
#
# See docs/troubleshooting.md if the run aborts mid-stage.
# ─────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"

# Resolve the repo root relative to this script.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Resolve-Path (Join-Path $scriptDir "..")
Set-Location $repoRoot

if (-not (Test-Path ".env")) {
    Write-Host "⚠️  .env not found at $repoRoot\.env" -ForegroundColor Yellow
    Write-Host "    Copy .env.example → .env and fill in your credentials." -ForegroundColor Yellow
    exit 1
}

# `--rm` keeps the one-shot container from sticking around after the run.
# The `onboarding` profile must be active; without it the service is
# hidden from `docker compose run` (plan_00 + docker-compose.yml).
& docker compose --profile onboarding run --rm onboarding `
    zotai s1 run-all @args
exit $LASTEXITCODE
