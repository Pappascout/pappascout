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

``os.replace`` is atomic when it happens -- but on Windows it does not
always get to start. Measured on this machine on 2026-09-10: a filter
driver refuses the rename for a few milliseconds with ``PermissionError
[WinError 5]``. The rename is therefore retried on a rising delay; see
:func:`_replace_with_retry`.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
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


#: The waits, in seconds, between a refused ``os.replace`` and the next
#: attempt: 1, 2, 5, 10, 25, 50 ms. Six waits after the first attempt, so
#: seven calls in all, and their sum -- 93 ms -- is the whole budget.
#:
#: The numbers come from a measurement and not from a habit. On 2026-09-10
#: the block was timed on this machine: it cleared in **1-11 ms**, usually on
#: the first retry, and the worst case seen was **10.6 ms**. 93 ms is roughly
#: nine times that worst case, so a block that is going to clear has cleared
#: long before the budget runs out, while a block that never clears costs a
#: tenth of a second and then fails.
#:
#: The waits rise rather than repeat, so the common case -- cleared on the
#: first retry -- pays 1 ms and not a flat average.
_REPLACE_RETRY_DELAYS: tuple[float, ...] = (0.001, 0.002, 0.005, 0.010, 0.025, 0.050)


def _replace_with_retry(tmp: Path, target: Path) -> None:
    """Rename onto the target, retried while Windows refuses to start.

    ``os.replace`` is atomic **when it happens**. What was wrong was the
    assumption that it always gets to start. Measured on this machine on
    2026-09-10, six times over four days: the call raises
    ``PermissionError [WinError 5]`` (ACCESS_DENIED, *not* 32
    SHARING_VIOLATION) although nothing in this process holds the target.
    A ``CreateFileW`` probe could open that same target for delete while
    the rename was still being refused, so the block is not a user-mode
    handle at all -- it is kernel-level, a filter driver. Plain stdlib
    ``open``/``write``/``fsync``/``os.replace`` in a loop reproduces it with
    none of this project's code: 45 failures in 10 000 iterations.

    The distinction that makes a retry safe here is that a **transient**
    block clears in 1-11 ms and a **permanent** one never does. A control
    experiment pinned that down: holding the target open ourselves produced
    the same error, and it survived 6 716 attempts over 30 s. So a bounded
    budget separates the two by itself -- there is nothing to detect and no
    heuristic to get wrong.

    Only :class:`PermissionError` is retried. Every other :class:`OSError`
    is a different phenomenon -- a missing temporary file, a cross-device
    move, a full disk -- and fails at once, unretried.

    When the budget runs out the **first** ``PermissionError`` is re-raised
    as it stands: same type, same ``winerror``, same message, not wrapped,
    not logged and continued. A retry that hid a permanent failure would be
    worse than the failure, and the three stage tests that put a directory
    in the target's place are the guard -- they must still see exactly the
    error they saw before, only later.

    Args:
        tmp: The temporary file that holds the finished content.
        target: The final path it is renamed onto.

    Raises:
        PermissionError: If every attempt inside the budget was refused.
        OSError: Any other failure, immediately and unretried.
    """
    attempts = len(_REPLACE_RETRY_DELAYS) + 1
    first: PermissionError | None = None
    for attempt in range(attempts):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:
            if first is None:
                first = exc
            if attempt == attempts - 1:
                # ``from None``: the report is the original failure, not a
                # chain of seven identical ones.
                raise first from None
            time.sleep(_REPLACE_RETRY_DELAYS[attempt])


@contextmanager
def atomic_path(target: Path | str) -> Iterator[Path]:
    """Yield a temporary path that is moved to the target only on success.

    Usage::

        with atomic_path(path) as tmp:
            df.write_parquet(tmp)

    The target directory is created if needed. On an exception the temporary
    file is removed and the target is left untouched -- the old version stays
    in place, intact.

    This is AD-7's single choke point, so the rename is the one place that
    needs the Windows retry (:func:`_replace_with_retry`) -- manifests,
    parquet tables, indexes and reports are all covered by it at once. The
    retry changes no promise this function makes: it only waits, at most
    93 ms, for a rename that a filter driver is refusing to let start, and
    a rename that never starts still raises.

    Args:
        target: The final file path.

    Yields:
        Path of the temporary file the content is written to.

    Raises:
        FileNotFoundError: If the block wrote nothing to the temporary path.
        PermissionError: If the rename was refused for the whole budget.
        OSError: Any other failure of the rename, unretried.
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
        _replace_with_retry(tmp, target)
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
