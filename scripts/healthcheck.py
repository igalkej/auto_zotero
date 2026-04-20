#!/usr/bin/env python3
"""Lightweight healthcheck used by Docker HEALTHCHECK and docker-compose.

Behaviour:
    * If the S2 dashboard env vars are configured, HTTP-probe
      `GET http://{host}:{port}/healthz`.
    * Otherwise, succeed as long as the `zotai` package imports cleanly
      (so the `onboarding` service stays healthy between runs).

Exits with code 0 on success, 1 on failure. Avoids third-party deps so it
can run in the minimal runtime image even before `uv sync` resolved extras.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def _check_dashboard() -> int:
    host = os.environ.get("S2_DASHBOARD_HOST", "127.0.0.1")
    port = os.environ.get("S2_DASHBOARD_PORT", "8000")
    url = f"http://{host}:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            return 0 if 200 <= resp.status < 400 else 1
    except (urllib.error.URLError, TimeoutError, OSError):
        return 1


def _check_import() -> int:
    try:
        import zotai  # noqa: F401
    except Exception:  # pragma: no cover - import-time failure
        return 1
    return 0


def main() -> int:
    if os.environ.get("S2_DASHBOARD_PORT"):
        return _check_dashboard()
    return _check_import()


if __name__ == "__main__":
    sys.exit(main())
