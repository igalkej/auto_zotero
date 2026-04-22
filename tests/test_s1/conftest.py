"""Stage 01 fixtures.

Rather than pulling in ``reportlab`` / ``pypdf`` as a test dependency, we
hand-roll minimal PDF-1.4 bytes. The builder supports N pages with
optional text per page; ``None`` produces a blank page (simulating a
scanned PDF).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest

Kind = Literal[
    "text_doi",
    "text_no_doi",
    "scanned",
    "fake",
    "corrupt",
    "factura_1page",
    "dni_scanned",
    "ambiguous_short",
    "paper_keywords",
]


def _pdf_bytes(page_texts: list[str | None]) -> bytes:
    """Assemble a PDF-1.4 with ``len(page_texts)`` pages.

    Each element is either the text for that page (rendered in
    Helvetica), or ``None`` for a page with an empty content stream
    (i.e. a page with no extractable text — what you'd get from a pure
    scan). The shared font object keeps the file small.
    """
    n = len(page_texts)
    if n == 0:
        raise ValueError("need at least 1 page")

    # Object numbering plan:
    # 1              → Catalog
    # 2              → Pages (kids array)
    # 3 .. 3+n-1     → Page objects
    # 3+n .. 3+2n-1  → Content streams
    # 3+2n           → Font (shared)
    page_obj_ids = list(range(3, 3 + n))
    content_obj_ids = list(range(3 + n, 3 + 2 * n))
    font_obj_id = 3 + 2 * n

    streams: list[bytes] = []
    for text in page_texts:
        if text is None:
            streams.append(b"")
        else:
            escaped = (
                text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
            )
            streams.append(
                f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
            )

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = b" ".join(f"{pid} 0 R".encode() for pid in page_obj_ids)
    objects.append(
        b"<< /Type /Pages /Kids [" + kids + f"] /Count {n}".encode() + b" >>"
    )
    for i, _pid in enumerate(page_obj_ids):
        cid = content_obj_ids[i]
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Contents {cid} 0 R /Resources << /Font << /F1 {font_obj_id} 0 R >> >> >>"
            ).encode()
        )
    for stream in streams:
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

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
    # 1-page PDF, long prose + DOI → heuristic_accept via DOI.
    "text_doi": _pdf_bytes([_LONG + " doi: 10.1234/example.2024"]),
    # 1-page PDF, long prose, no academic markers → ambiguous → LLM gate.
    "text_no_doi": _pdf_bytes([_LONG]),
    # 3-page scan (no text). page_count > 2 → heuristic_reject skipped →
    # ambiguous → LLM gate (or needs_review if skipped). Survives Stage 01.
    "scanned": _pdf_bytes([None, None, None]),
    "fake": b"<html>not a pdf</html>\n",
    "corrupt": b"%PDF-1.4\n" + (b"garbage" * 50),
    # 1 page + billing keyword → heuristic_reject("billing_keyword:factura").
    # Single keyword only — multiple would leave the returned reason
    # dependent on frozenset iteration order.
    "factura_1page": _pdf_bytes(["factura 001-00012345 total: $1500"]),
    # 2-page scanned document with no text → heuristic_reject("short_no_text").
    "dni_scanned": _pdf_bytes([None, None]),
    # 5 pages of generic prose, no academic markers → ambiguous (LLM gate).
    "ambiguous_short": _pdf_bytes(
        [
            "Page body one without markers.",
            "Page body two generic content.",
            "Page body three generic content.",
            "Page body four generic content.",
            "Page body five generic content.",
        ]
    ),
    # 3-page paper-like PDF with Abstract + References + Keywords →
    # heuristic_accept via the keyword branch (no DOI).
    "paper_keywords": _pdf_bytes(
        [
            "Abstract. We study the fiscal multiplier. Keywords: economy.",
            "References. Smith 2020. Jones 2021.",
            "Conclusion body.",
        ]
    ),
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
        "MAX_COST_USD_STAGE_01",
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
