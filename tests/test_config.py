"""Tests for `zotai.config`."""

from __future__ import annotations

from pathlib import Path

import pytest

from zotai.config import (
    OcrSettings,
    PathSettings,
    S2Settings,
    Settings,
    ZoteroSettings,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip known env vars + pin CWD so pydantic-settings can't pick up a real `.env`."""
    for prefix in (
        "ZOTERO_",
        "OPENAI_",
        "SEMANTIC_SCHOLAR_",
        "OCR_",
        "S2_",
    ):
        for var in (
            "API_KEY",
            "LIBRARY_ID",
            "LIBRARY_TYPE",
            "LOCAL_API",
            "LOCAL_API_HOST",
            "MODEL_TAG",
            "MODEL_EXTRACT",
            "EMBEDDING_MODEL",
            "LANGUAGES",
            "PARALLEL_PROCESSES",
            "FETCH_INTERVAL_HOURS",
            "CANDIDATES_DB",
            "CHROMA_PATH",
            "WORKER_DISABLED",
            "ZOTERO_INBOX_COLLECTION",
            "MAX_EMBED_PER_CYCLE",
            "SAFE_DELETE_RATIO",
            "MAX_COST_USD_BACKFILL",
            "QUERY_BM25_WEIGHT",
            "PDF_SOURCES",
            "PDF_FETCH_MAX_ATTEMPTS_PER_CANDIDATE",
            "PDF_FETCH_TIMEOUT_SECONDS",
            "PDF_FETCH_MAX_MINUTES_WEEKLY",
            "PDF_FETCH_CIRCUIT_BREAKER_THRESHOLD",
            "DASHBOARD_HOST",
            "DASHBOARD_PORT",
            "MAX_COST_USD_DAILY",
            "MAX_COST_USD_MONTHLY",
        ):
            monkeypatch.delenv(prefix + var, raising=False)
    for key in (
        "PDF_SOURCE_FOLDERS",
        "STAGING_FOLDER",
        "STATE_DB",
        "REPORTS_FOLDER",
        "MAX_COST_USD_TOTAL",
        "MAX_COST_USD_STAGE_04",
        "MAX_COST_USD_STAGE_05",
        "DRY_RUN",
        "LOG_LEVEL",
        "USER_EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


def test_settings_load_with_defaults() -> None:
    s = Settings()
    assert s.zotero.library_type == "user"
    assert s.zotero.local_api is True
    # Empty default means "use pyzotero's hardcoded localhost:23119"; the
    # docker-compose layer sets host.docker.internal at the container env,
    # so the settings layer stays OS-agnostic. See ADR 013.
    assert s.zotero.local_api_host == ""
    assert s.openai.model_tag == "gpt-4o-mini"
    assert s.ocr.languages == "spa+eng"
    assert s.ocr.parallel_processes == 4
    assert s.s2.fetch_interval_hours == 6
    assert s.s2.pdf_sources == [
        "openaccess",
        "doi",
        "annas",
        "libgen",
        "scihub",
        "rss",
    ]


def test_settings_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOTERO_API_KEY", "abc123")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "42")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("S2_PDF_SOURCES", "openaccess, doi, scihub")
    monkeypatch.setenv("OCR_PARALLEL_PROCESSES", "8")

    s = Settings()
    assert s.zotero.api_key.get_secret_value() == "abc123"
    assert s.zotero.library_id == "42"
    assert s.openai.api_key.get_secret_value() == "sk-test"
    assert s.s2.pdf_sources == ["openaccess", "doi", "scihub"]
    assert s.ocr.parallel_processes == 8


def test_zotero_library_type_rejects_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_LIBRARY_TYPE", "organization")
    with pytest.raises(Exception):  # pydantic ValidationError
        ZoteroSettings()


def test_ocr_parallel_rejects_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_PARALLEL_PROCESSES", "0")
    with pytest.raises(Exception):
        OcrSettings()


def test_pdf_source_folders_parses_csv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PDF_SOURCE_FOLDERS", "/a,/b , /c/d")
    s = PathSettings()
    assert s.pdf_source_folders == [Path("/a"), Path("/b"), Path("/c/d")]


def test_s2_dashboard_port_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_DASHBOARD_PORT", "0")
    with pytest.raises(Exception):
        S2Settings()


def test_s2_indexing_and_query_defaults() -> None:
    """ADR 015 + ADR 017 knobs declared in `.env.example` round-trip through
    ``Settings()`` instead of being silently dropped by ``extra='ignore'``.
    """
    s = S2Settings()
    assert s.max_embed_per_cycle == 50
    assert s.safe_delete_ratio == 0.10
    assert s.max_cost_usd_backfill == 3.0
    assert s.query_bm25_weight == 0.4
    assert s.pdf_fetch_max_attempts_per_candidate == 6
    assert s.pdf_fetch_timeout_seconds == 30
    assert s.pdf_fetch_max_minutes_weekly == 20
    assert s.pdf_fetch_circuit_breaker_threshold == 5
    assert s.worker_disabled is False


def test_s2_reads_indexing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("S2_MAX_EMBED_PER_CYCLE", "200")
    monkeypatch.setenv("S2_SAFE_DELETE_RATIO", "0.05")
    monkeypatch.setenv("S2_MAX_COST_USD_BACKFILL", "5.0")
    monkeypatch.setenv("S2_QUERY_BM25_WEIGHT", "0.6")
    monkeypatch.setenv("S2_PDF_FETCH_CIRCUIT_BREAKER_THRESHOLD", "0")
    monkeypatch.setenv("S2_WORKER_DISABLED", "true")
    s = S2Settings()
    assert s.max_embed_per_cycle == 200
    assert s.safe_delete_ratio == 0.05
    assert s.max_cost_usd_backfill == 5.0
    assert s.query_bm25_weight == 0.6
    # ``0`` is the documented way to disable the circuit breaker
    # (plan_02 §10.4), so the non-negative validator must accept it.
    assert s.pdf_fetch_circuit_breaker_threshold == 0
    assert s.worker_disabled is True


def test_s2_safe_delete_ratio_rejects_out_of_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_SAFE_DELETE_RATIO", "1.5")
    with pytest.raises(Exception):
        S2Settings()


def test_s2_query_bm25_weight_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_QUERY_BM25_WEIGHT", "-0.1")
    with pytest.raises(Exception):
        S2Settings()


def test_s2_max_embed_per_cycle_rejects_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_MAX_EMBED_PER_CYCLE", "0")
    with pytest.raises(Exception):
        S2Settings()


def test_s2_circuit_breaker_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_PDF_FETCH_CIRCUIT_BREAKER_THRESHOLD", "-1")
    with pytest.raises(Exception):
        S2Settings()


def test_s2_max_cost_backfill_rejects_negative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("S2_MAX_COST_USD_BACKFILL", "-1.0")
    with pytest.raises(Exception):
        S2Settings()
