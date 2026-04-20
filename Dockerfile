# syntax=docker/dockerfile:1.7
# ─────────────────────────────────────────────────────────────
# zotero-ai-toolkit — multi-stage Docker build.
#
# Stage 1 (builder): installs uv, resolves deps into a venv.
# Stage 2 (runtime): slim image with system deps for OCR and a non-root user.
# ─────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.11

# ─── Stage 1: builder ────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# uv binary
COPY --from=ghcr.io/astral-sh/uv:0.4 /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (better layer caching)
COPY pyproject.toml ./
# uv.lock will be added once generated; copy conditionally to avoid build break
COPY uv.loc[k] ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-deps-check ".[s2]" || \
    VIRTUAL_ENV=/opt/venv uv pip install ".[s2]"

# Copy source after deps are resolved
COPY src/ ./src/

RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/opt/venv uv pip install --no-deps -e .

# ─── Stage 2: runtime ────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

# System deps required for OCR + PDF handling
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-spa \
        tesseract-ocr-eng \
        ocrmypdf \
        ghostscript \
        libpoppler-cpp-dev \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r zotai --gid 1000 && \
    useradd  -r -g zotai --uid 1000 --create-home --shell /bin/bash zotai

# Venv from builder
COPY --from=builder --chown=zotai:zotai /opt/venv /opt/venv

# App source (editable install already registered in /opt/venv)
WORKDIR /app
COPY --chown=zotai:zotai src/        ./src/
COPY --chown=zotai:zotai config/     ./config/
COPY --chown=zotai:zotai scripts/    ./scripts/
COPY --chown=zotai:zotai alembic/    ./alembic/
COPY --chown=zotai:zotai alembic.ini ./alembic.ini
COPY --chown=zotai:zotai pyproject.toml ./pyproject.toml

# Workspace (mounted as volumes in docker-compose)
RUN mkdir -p /workspace/staging /workspace/reports && \
    chown -R zotai:zotai /workspace

USER zotai

# Healthcheck (S2 dashboard). Onboarding service overrides via `--no-healthcheck` profile.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python /app/scripts/healthcheck.py || exit 1

# Default command — overridden per service in docker-compose.yml
CMD ["zotai", "--help"]
