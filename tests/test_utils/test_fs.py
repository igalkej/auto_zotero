"""Tests for `zotai.utils.fs`."""

from __future__ import annotations

import hashlib
from pathlib import Path

from zotai.utils import fs


def test_file_sha256_matches_hashlib(tmp_path: Path) -> None:
    content = b"hello world\n" * 1000
    f = tmp_path / "x.bin"
    f.write_bytes(content)
    assert fs.file_sha256(f) == hashlib.sha256(content).hexdigest()


def test_file_sha256_handles_multi_chunk(tmp_path: Path) -> None:
    content = b"x" * (3 * (1 << 20) + 17)  # > 3 MiB, non-aligned
    f = tmp_path / "big.bin"
    f.write_bytes(content)
    assert fs.file_sha256(f) == hashlib.sha256(content).hexdigest()


def test_validate_pdf_magic_true(tmp_path: Path) -> None:
    f = tmp_path / "real.pdf"
    f.write_bytes(b"%PDF-1.7\nstuff")
    assert fs.validate_pdf_magic(f) is True


def test_validate_pdf_magic_false_on_html(tmp_path: Path) -> None:
    f = tmp_path / "fake.pdf"
    f.write_bytes(b"<html>")
    assert fs.validate_pdf_magic(f) is False


def test_validate_pdf_magic_false_on_missing(tmp_path: Path) -> None:
    assert fs.validate_pdf_magic(tmp_path / "missing.pdf") is False


def test_ensure_dir_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    fs.ensure_dir(target)
    fs.ensure_dir(target)
    assert target.is_dir()


def test_disk_space_check(tmp_path: Path) -> None:
    # We can't know the exact free space, but 1 byte should always fit
    # and a petabyte-sized request should not.
    assert fs.disk_space_check(tmp_path, 1) is True
    assert fs.disk_space_check(tmp_path, 10**18) is False


def test_safe_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.txt"
    src.write_text("content")
    dst = tmp_path / "nested" / "dst.txt"
    out = fs.safe_copy(src, dst)
    assert out == dst
    assert dst.read_text() == "content"
