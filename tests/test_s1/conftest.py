"""Stage 01 fixtures.

Rather than pulling in ``reportlab`` / ``pypdf`` as a test dependency, we
hand-roll minimal PDF-1.4 bytes. The builder is intentionally tiny: one
page, Helvetica Type-1, optional text content stream.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

import pytest

Kind = Literal["text_doi", "text_no_doi", "scanned", "fake", "corrupt"]


def _pdf_bytes(page_text: str | None) -> bytes:
    if page_text:
        escaped = (
            page_text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
        )
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    else:
        stream = b""

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length "
        + str(len(stream)).encode()
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    body = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{idx} 0 obj\n".encode() + obj + b"\nendobj\n"

    xref_offset = len(body)
    body += f"xref\n0 {len(objects) + 1}\n".encode()
    body += b"0000000000 65535 f \n"
    for off in offsets:
        body += f"{off:010d} 00000 n \n".encode()
    body += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(body)


_LONG = "This paper discusses the fiscal multiplier at length. " * 5

_KIND_TO_BYTES: dict[Kind, bytes] = {
    "text_doi": _pdf_bytes(_LONG + " doi: 10.1234/example.2024"),
    "text_no_doi": _pdf_bytes(_LONG),
    "scanned": _pdf_bytes(None),
    "fake": b"<html>not a pdf</html>\n",
    "corrupt": b"%PDF-1.4\n" + (b"garbage" * 50),
}


@pytest.fixture(autouse=True)
def _isolate_settings_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Strip env vars and pin cwd so Settings() can't absorb a real .env."""
    for var in (
        "STATE_DB",
        "REPORTS_FOLDER",
        "STAGING_FOLDER",
        "PDF_SOURCE_FOLDERS",
        "DRY_RUN",
        "LOG_LEVEL",
        "USER_EMAIL",
        "MAX_COST_USD_TOTAL",
        "MAX_COST_USD_STAGE_04",
        "MAX_COST_USD_STAGE_05",
    ):
        monkeypatch.delenv(var, raising=False)
    for prefix in ("ZOTERO_", "OPENAI_", "SEMANTIC_SCHOLAR_", "OCR_", "S2_"):
        for suffix in (
            "API_KEY",
            "LIBRARY_ID",
            "LIBRARY_TYPE",
            "LOCAL_API",
            "PDF_SOURCES",
            "DASHBOARD_HOST",
            "DASHBOARD_PORT",
        ):
            monkeypatch.delenv(prefix + suffix, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def pdf_builder(tmp_path: Path) -> Callable[..., Path]:
    """Return a callable that writes a fixture PDF and returns its path."""

    def _build(
        kind: Kind, *, directory: Path | None = None, name: str | None = None
    ) -> Path:
        target_dir = directory if directory is not None else tmp_path / "pdfs"
        target_dir.mkdir(parents=True, exist_ok=True)
        filename = name if name is not None else f"{kind}.pdf"
        target = target_dir / filename
        target.write_bytes(_KIND_TO_BYTES[kind])
        return target

    return _build
