"""FAISS path I/O with an ASCII bridge and atomic destination replacement."""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Any

_BRIDGE_MARKER = "__anam_ascii_bridge__"


def needs_bridge(path: Any) -> bool:
    """Windows FAISS uses narrow filesystem paths; bridge non-ASCII filenames."""
    return os.name == "nt" and not os.fsdecode(path).isascii()


def safe_temp_dir() -> str:
    """Prefer the process TEMP directory when it is ASCII and writable."""
    candidates = [tempfile.gettempdir()]
    if os.name == "nt":
        candidates.append(
            os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
        )
    for candidate in candidates:
        if (
            candidate.isascii()
            and os.path.isdir(candidate)
            and os.access(candidate, os.W_OK)
        ):
            return candidate
    raise OSError("无法找到可写的纯 ASCII FAISS 临时目录")


def safe_temp_file(prefix: str) -> str:
    """Create an ASCII bridge file; the caller owns its lifetime."""
    fd, path = tempfile.mkstemp(
        prefix=f"{prefix}_", suffix=".faiss", dir=safe_temp_dir()
    )
    os.close(fd)
    return path


def _remove_quietly(path: str | None) -> None:
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def read_index_safe(path: Any, *args, _reader=None, **kwargs):
    """Read through an ASCII temporary file when the destination needs a bridge."""
    if _reader is None:
        import faiss

        _reader = faiss.read_index
    target = os.fsdecode(path)
    if not needs_bridge(target):
        return _reader(target, *args, **kwargs)
    bridge = safe_temp_file("anam_faiss_read")
    try:
        shutil.copy2(target, bridge)
        return _reader(bridge, *args, **kwargs)
    finally:
        _remove_quietly(bridge)


def write_index_safe(index: Any, path: Any, *args, _writer=None, **kwargs) -> None:
    """Stage the complete index beside the target, then atomically replace it.

    Bridged writes serialize in the process ASCII TEMP directory, then copy into
    destination-side staging. A failed cross-volume copy cannot touch the healthy
    target, and the final replacement is always within the destination directory.
    """
    if _writer is None:
        import faiss

        _writer = faiss.write_index
    target = os.fsdecode(path)
    directory = os.path.dirname(target) or "."
    os.makedirs(directory, exist_ok=True)
    fd, staging = tempfile.mkstemp(prefix=".anam_faiss_", suffix=".tmp", dir=directory)
    os.close(fd)
    bridge = None
    try:
        if needs_bridge(target):
            bridge = safe_temp_file("anam_faiss_write")
            _writer(index, bridge, *args, **kwargs)
            shutil.copy2(bridge, staging)
        else:
            _writer(index, staging, *args, **kwargs)
        os.replace(staging, target)
    finally:
        _remove_quietly(bridge)
        _remove_quietly(staging)


def bridge_installed() -> bool:
    """Report whether both process-wide FAISS path functions carry the bridge."""
    try:
        import faiss
    except Exception:
        return False
    return bool(
        getattr(faiss.read_index, _BRIDGE_MARKER, False)
        and getattr(faiss.write_index, _BRIDGE_MARKER, False)
    )
