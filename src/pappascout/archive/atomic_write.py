"""Atomic writes into the synchronised folder (AD-7).

The archive lives in a synchronised folder two machines share, so a write
left half-finished would show up on the other machine as a truncated
file. Every write therefore goes first into a temporary file
``<name>.tmp-<host>-<pid>-<random>`` and only reaches the target once it is
complete, through ``os.replace``. The host name in that name stops two
machines from using the same temporary file; the process id and the random
part stop two parallel runs on the same machine from doing so as well.

If a write is interrupted, the target either does not exist or is the old
intact version, and the temporary file is cleaned up.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "host_tag",
    "temp_suffix",
    "atomic_path",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@lru_cache(maxsize=1)
def host_tag() -> str:
    """The host name in a form that is valid inside a file name."""
    name = socket.gethostname() or "unknown-host"
    return _UNSAFE.sub("-", name).strip("-").lower() or "unknown-host"


def temp_suffix() -> str:
    """A unique temporary-file suffix, e.g. ``.tmp-desktop-1234-9f3a1c07``.

    Every call returns a different value: the host name separates two
    machines, the process id and the random part separate two parallel runs
    on the same machine. Without them two simultaneous writes would destroy
    each other's files.
    """
    return f".tmp-{host_tag()}-{os.getpid()}-{secrets.token_hex(4)}"


def _temp_path(target: Path) -> Path:
    return target.with_name(target.name + temp_suffix())


@contextmanager
def atomic_path(target: Path | str) -> Iterator[Path]:
    """Yield a temporary path that is moved to the target only on success.

    Usage::

        with atomic_path(path) as tmp:
            df.write_parquet(tmp)

    The target directory is created if needed. On an exception the temporary
    file is removed and the target is left untouched -- the old version stays
    in place, intact.

    Args:
        target: The final file path.

    Yields:
        Path of the temporary file the content is written to.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(target)
    try:
        yield tmp
        if not tmp.exists():
            raise FileNotFoundError(
                f"The atomic write produced no file at {tmp}. "
                "Write the content to the temporary path you were given."
            )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_bytes(target: Path | str, data: bytes) -> Path:
    """Write bytes atomically. Returns the target path."""
    target = Path(target)
    with atomic_path(target) as tmp:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    return target


def atomic_write_text(
    target: Path | str, text: str, encoding: str = "utf-8"
) -> Path:
    """Write text atomically. Returns the target path."""
    return atomic_write_bytes(target, text.encode(encoding))


def atomic_write_json(target: Path | str, obj: Any, indent: int = 2) -> Path:
    """Write JSON atomically as UTF-8. Returns the target path."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True)
    return atomic_write_text(target, text + "\n")
