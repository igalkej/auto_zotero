"""Tests for `zotai.utils.pdf`.

Most of this module is regex + string work, which is easy to cover with
unit tests. Actual PDF parsing is exercised with fixture PDFs once
Phase 2 (#3) lands; for Phase 1 we test the pure-function pieces.
"""

from __future__ import annotations

import pytest

from zotai.utils import pdf


@pytest.mark.parametrize(
    "text, expected",
    [
        ("see doi: 10.1234/abc.def for details", "10.1234/abc.def"),
        ("https://doi.org/10.1016/j.econlet.2020.109234 citation",
         "10.1016/j.econlet.2020.109234"),
        ("10.1145/3319535.3363232.", "10.1145/3319535.3363232"),
        ("no identifier here", None),
        ("", None),
    ],
)
def test_detect_doi(text: str, expected: str | None) -> None:
    assert pdf.detect_doi(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("arXiv:2301.12345", "2301.12345"),
        ("See https://arxiv.org/abs/2401.99999v2 for v2", "2401.99999"),
        ("just text", None),
    ],
)
def test_detect_arxiv(text: str, expected: str | None) -> None:
    assert pdf.detect_arxiv(text) == expected


def test_doi_regex_trims_trailing_punctuation() -> None:
    assert pdf.detect_doi("cf. 10.1234/example.") == "10.1234/example"
    assert pdf.detect_doi("(10.1234/example);") == "10.1234/example"
