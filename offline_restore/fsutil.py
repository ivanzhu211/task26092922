"""Low-level crash-safe filesystem helpers.

Threat model: only forced process termination (SIGKILL / power-off of the
process) is considered, not power loss, media damage or external edits.
Replacements are still fsynced so the directory change ordering is sound.
"""

from __future__ import annotations

import errno
import os
import shutil

_COPY_CHUNK = 1024 * 1024


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def fsync_dir(path: str) -> None:
    """fsync a directory so that entry creation/removal/rename within it
    is flushed."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_parent(path: str) -> None:
    fsync_dir(os.path.dirname(os.path.abspath(path)) or ".")


def _stream_copy(src: str, dst: str) -> None:
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            chunk = fin.read(_COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
        fout.flush()
        os.fsync(fout.fileno())


def hardlink_or_copy(src: str, dst: str) -> None:
    """Place an identical copy of ``src`` at ``dst``.

    Prefers a hard link (never a symbolic link): it costs no extra disk
    bytes and cannot pull the whole backup into memory. If hard links are
    unsupported (cross-device filesystem), falls back to a streaming copy
    with fixed-size buffers. ``dst`` must not already exist.
    """
    try:
        os.link(src, dst, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.ENOSYS):
            _stream_copy(src, dst)
        else:
            raise


def durable_replace(src: str, dst: str | None) -> None:
    """Atomically rename ``src`` onto ``dst`` (dst may be absent) and
    fsync the containing directory.

    After this returns, ``src`` no longer exists and ``dst`` holds its
    inode. Under the forced-kill threat model the rename is already
    visible once ``os.replace`` returns; the fsync additionally covers
    the directory metadata.
    """
    if dst is None:
        raise ValueError("durable_replace requires a destination")
    os.replace(src, dst)
    fsync_parent(dst)


def remove_file_if_exists(path: str) -> bool:
    """Unlink ``path`` if present; fsync the parent when a removal
    happened. Returns whether a file was removed."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    fsync_parent(path)
    return True


def remove_tree(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
