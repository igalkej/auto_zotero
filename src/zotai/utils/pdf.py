"""PDF text + metadata extraction helpers built on pdfplumber.

Phase 1 exposes the primitives used by:
- Stage 01 (inventory): `extract_text_pages`, `detect_doi`, `has_text_layer`
- Stage 04 (enrichment): `extract_probable_title`, `detect_arxiv`

The functions are deliberately narrow — no network, no DB, easy to unit-test
with fixture PDFs.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pdfplumber

_DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", re.IGNORECASE)
_ARXIV_RE = re.compile(
    r"(?:arXiv:|arxiv\.org/abs/)(\d{4}\.\d{4,5})(?:v\d+)?",
    re.IGNORECASE,
)

# Generic titles that should short-circuit title-based lookups.
_TITLE_BLACKLIST: frozenset[str] = frozenset(
    {
        "abstract",
        "acknowledgements",
        "appendix",
        "bibliography",
        "chapter 1",
        "contents",
        "introduction",
        "references",
        "summary",
        "table of contents",
    }
)

_DEFAULT_PAGES = 3
_HAS_TEXT_THRESHOLD = 100


def extract_text_pages(path: Path, max_pages: int = _DEFAULT_PAGES) -> list[str]:
    """Return the text of the first `max_pages` pages (empty string on blank pages)."""
    texts: list[str] = []
    with pdfplumber.open(path) as pdf:
        for idx, page in enumerate(pdf.pages):
            if idx >= max_pages:
                break
            text = page.extract_text() or ""
            texts.append(text)
    return texts


def detect_doi(text: str) -> str | None:
    """Return the first DOI found in `text`, or None."""
    match = _DOI_RE.search(text)
    if match is None:
        return None
    # Trim trailing punctuation commonly captured by the regex boundary.
    return match.group(0).rstrip(".,;:)")


def detect_arxiv(text: str) -> str | None:
    """Return the arXiv id (e.g. '2301.12345') found in `text`, or None."""
    match = _ARXIV_RE.search(text)
    return match.group(1) if match else None


def has_text_layer(path: Path, threshold: int = _HAS_TEXT_THRESHOLD) -> bool:
    """True iff the first page of the PDF yields at least `threshold` chars of text."""
    pages = extract_text_pages(path, max_pages=1)
    if not pages:
        return False
    return len(pages[0]) >= threshold


def _iter_lines(chars: list[dict[str, Any]]) -> Iterator[tuple[float, float, str]]:
    """Group pdfplumber chars into lines keyed by rounded `top`.

    Yields (average_font_size, top, text) per line.
    """
    by_line: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for ch in chars:
        top_val = ch.get("top")
        if top_val is None:
            continue
        by_line[round(float(top_val))].append(ch)

    for top_key, char_list in by_line.items():
        sizes = [float(c.get("size", 0.0)) for c in char_list if c.get("size") is not None]
        if not sizes:
            continue
        avg_size = sum(sizes) / len(sizes)
        text = "".join(str(c.get("text", "")) for c in char_list).strip()
        if text:
            yield avg_size, float(top_key), text


def extract_probable_title(path: Path) -> str | None:
    """Return the largest-font line on page 1, or None if the heuristic fails.

    Skips generic headings listed in `_TITLE_BLACKLIST` and lines shorter than
    five words — both make downstream fuzzy matching noisy (plan_01 §3 Stage 04).
    """
    with pdfplumber.open(path) as pdf:
        if not pdf.pages:
            return None
        page = pdf.pages[0]
        chars = page.chars or []
    if not chars:
        return None

    lines = sorted(_iter_lines(chars), key=lambda triple: (-triple[0], triple[1]))
    for _size, _top, text in lines:
        normalized = text.lower().strip()
        if normalized in _TITLE_BLACKLIST:
            continue
        if len(text.split()) < 5:
            continue
        return text
    return None


__all__ = [
    "detect_arxiv",
    "detect_doi",
    "extract_probable_title",
    "extract_text_pages",
    "has_text_layer",
]
