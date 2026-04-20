"""Shared pytest configuration.

Phase 0 — only the bare minimum. Real fixtures (Zotero mocks, fixture PDFs,
DB factories) land with Phase 1 (#2) and Phase 2 (#3).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return Path(__file__).resolve().parent.parent
