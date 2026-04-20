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
            "MODEL_TAG",
            "MODEL_EXTRACT",
            "EMBEDDING_MODEL",
            "LANGUAGES",
            "PARALLEL_PROCESSES",
            "FETCH_INTERVAL_HOURS",
            "CANDIDATES_DB",
            "CHROMA_PATH",
            "ZOTERO_INBOX_COLLECTION",
            "PDF_SOURCES",
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
