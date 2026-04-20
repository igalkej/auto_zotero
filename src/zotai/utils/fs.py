"""Filesystem helpers.

All of these are pure wrappers around `pathlib` / `shutil` / `hashlib`, so
they type-check under `mypy --strict` without third-party stubs.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

_PDF_MAGIC = b"%PDF-"
_DEFAULT_CHUNK = 1 << 20  # 1 MiB


def ensure_dir(path: Path) -> Path:
    """Create `path` (and parents) if missing; return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def file_sha256(path: Path, chunk_size: int = _DEFAULT_CHUNK) -> str:
    """Streamed SHA-256 of a file, returned as a lowercase hex digest."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def validate_pdf_magic(path: Path) -> bool:
    """True iff the first five bytes of the file are `%PDF-`.

    Guards against mis-named files (e.g. `report.pdf` that's actually HTML).
    """
    try:
        with path.open("rb") as f:
            head = f.read(len(_PDF_MAGIC))
    except OSError:
        return False
    return head == _PDF_MAGIC


def disk_space_available(path: Path) -> int:
    """Free bytes on the filesystem that contains `path`."""
    target = path if path.exists() else path.parent
    return shutil.disk_usage(target).free


def disk_space_check(path: Path, required_bytes: int) -> bool:
    """True iff there are at least `required_bytes` free on `path`'s filesystem."""
    return disk_space_available(path) >= required_bytes


def safe_copy(src: Path, dst: Path) -> Path:
    """Copy `src` to `dst`, creating parents; preserve mtime/perm bits."""
    ensure_dir(dst.parent)
    shutil.copy2(src, dst)
    return dst


__all__ = [
    "disk_space_available",
    "disk_space_check",
    "ensure_dir",
    "file_sha256",
    "safe_copy",
    "validate_pdf_magic",
]
