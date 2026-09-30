"""Streaming hashing / length helpers (bounded memory)."""

from __future__ import annotations

import hashlib
import os

from .errors import VerificationError

READ_CHUNK = 1024 * 1024


def file_size_and_sha256(path: str) -> tuple[int, str]:
    """Return (size, hex sha256) for ``path`` using fixed-size reads."""
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    return total, digest.hexdigest()


def verify_file(path: str, expected_size: int, expected_sha256: str) -> None:
    """Raise VerificationError unless length and SHA-256 match.

    The hash is verified on a single streaming pass; memory usage is one
    fixed-size chunk regardless of file size.
    """
    try:
        stat_size = os.stat(path).st_size
    except FileNotFoundError as exc:
        raise VerificationError(f"source missing: {path}", target=path) from exc
    if stat_size != expected_size:
        raise VerificationError(
            f"size mismatch for {path}: "
            f"expected {expected_size}, found {stat_size}",
            target=path,
        )
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
    actual = digest.hexdigest()
    if total != expected_size or actual.lower() != expected_sha256.lower():
        raise VerificationError(
            f"sha256 mismatch for {path}: expected {expected_sha256}, "
            f"found {actual}",
            target=path,
        )
