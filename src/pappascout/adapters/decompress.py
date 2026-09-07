"""Decompressing and identifying a demo file.

Deliberately kept apart from parsing: FACEIT serves demos as ``.dem.zst``, and
Epic 3's demo download needs the same decompression as it stands. With
decompression in a module of its own, the download can call it without
dragging demoparser2 along with it.

Decompression **streams**: a 233 MB demo does not sensibly fit into an 8 GB
machine's memory at the same time as parsing, so the compressed file is
decompressed one chunk at a time into a temporary file. The temporary file
lives in the machine's own temp directory, **not in the archive**: the archive
sits in a synchronised folder, and synchronising a hundreds-of-megabytes
intermediate would be both slow and pointless.

Decompression writes to ``<name>.tmp`` first and renames it only at the end,
so that an interrupted decompression does not leave behind a half file that
would look like a finished demo.
"""

from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pappascout.errors import ParseError

__all__ = [
    "DEMO_MAGIC",
    "ZSTD_MAGIC",
    "GZIP_MAGIC",
    "COMPRESSED_SUFFIXES",
    "is_compressed",
    "declared_size",
    "check_demo_magic",
    "decompress_to",
    "readable_demo",
    "decompressed_name",
]

#: The CS2 demo's file signature. CS:GO's old format began with ``HL2DEMO``.
DEMO_MAGIC = b"PBDEMS2"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"

#: Compression suffixes that are stripped from the decompressed file's name.
COMPRESSED_SUFFIXES: tuple[str, ...] = (".zst", ".zstd", ".gz")

#: The decompression chunk size. A large chunk is faster, but memory use has
#: to stay moderate, because parsing reserves its own share on the same
#: machine.
_CHUNK = 1024 * 1024


def _head(path: Path, size: int = 8) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError as exc:
        raise ParseError(
            f"The file {path} could not be opened: {exc}\n"
            "Check the path, and that the file is not a cloud placeholder "
            "left by the sync client (open the file once in File Explorer)."
        ) from exc


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise ParseError(
            f"The size of the file {path} could not be read: {exc}\n"
            "Check that the file is not a cloud placeholder left by the sync "
            "client, and that it is not still being transferred."
        ) from exc


def decompressed_name(path: Path) -> str:
    """The decompressed file's name: compression suffix off, the rest as is.

    The name is **not** cut at the first dot. FACEIT's file names carry
    several dots (``...-1-1.dem.zst``), and cutting there would easily give
    two different demos the same name -- two simultaneous decompressions
    could then write into the same file.
    """
    name = path.name
    for suffix in COMPRESSED_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if name.lower().endswith(".dem") else f"{name}.dem"


def is_compressed(path: Path) -> bool:
    """Is the file compressed (zstd or gzip)?

    The detection is made from the file's leading bytes and not from its
    suffix: a hand-copied demo may be named wrongly, and a wrong guess would
    show up to the user as an incomprehensible parse error.
    """
    head = _head(path, 4)
    return head.startswith(ZSTD_MAGIC) or head.startswith(GZIP_MAGIC)


def declared_size(path: Path) -> int | None:
    """The decompressed size **the file itself declares**, or ``None``.

    A zstd frame's header carries an optional ``Frame_Content_Size``.
    Measured 2026-09-05: **every** one of the archive's five ``.dem.zst``
    files declares it (208-316 MB), and every one carries an XXH64 checksum
    as well. The field is therefore available in this data set and not
    theoretical.

    **This is the only independent length source an import has.** ``fetch``
    gets a ``Content-Length`` from the source; a hand-copied file has nobody
    to tell it how long it ought to be -- except the file itself. An
    uncompressed ``.dem`` does not have even that, and then the right answer
    is ``None`` and not a guess.

    Returns:
        The decompressed size in bytes, or ``None`` if the file is not zstd
        or if the frame does not declare a size.
    """
    head = _head(path, 64)
    if not head.startswith(ZSTD_MAGIC):
        return None
    try:
        zstandard = _zstd_module()
        size = zstandard.get_frame_parameters(head).content_size
    except Exception:  # noqa: BLE001 - the library's own error type varies
        return None
    # The library marks "not declared" with a very large sentinel value.
    if not isinstance(size, int) or size <= 0 or size >= 2**64 - 1:
        return None
    return size


def check_demo_magic(path: Path) -> None:
    """Make sure the decompressed file starts with ``PBDEMS2``.

    Raises:
        ParseError: If the file is not a CS2 demo. This check comes before
            the demoparser2 call, so that a text file with a ``.dem`` suffix
            gives a clear error of our own instead of the library's own
            message.
    """
    head = _head(path, len(DEMO_MAGIC))
    if head != DEMO_MAGIC:
        raise ParseError(
            f"The file {path.name} is not a CS2 demo: its header is "
            f"{head!r}, it should be {DEMO_MAGIC!r}.\n"
            "CS2 demos begin with the string PBDEMS2. Check that the "
            "download succeeded and that this is a .dem file and not, for "
            "example, an error page or a CS:GO-era demo."
        )


def _zstd_module():
    """Import ``zstandard``, or say how it is installed."""
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - the dependency is in pyproject.toml
        raise ParseError(
            "A compressed demo cannot be decompressed: the package zstandard "
            "is missing.\n"
            "Run: uv sync"
        ) from exc
    return zstandard


def decompress_to(source: Path, target: Path) -> Path:
    """Decompress ``source`` into ``target`` one chunk at a time.

    Supports zstd and gzip compression. An uncompressed file is copied as it
    stands. The write goes to ``<target>.tmp`` first and is renamed only on
    success, so an interruption does not leave a half ``target`` behind.

    Returns:
        ``target``.

    Raises:
        ParseError: If the decompression fails or the target directory
            cannot be created.
    """
    head = _head(source, 4)
    source_size = _size(source)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ParseError(
            f"The decompression directory {target.parent} could not be "
            f"created: {exc}\n"
            "Check the disk space and the write permissions."
        ) from exc

    tmp = target.with_name(target.name + ".tmp")
    try:
        if head.startswith(ZSTD_MAGIC):
            zstandard = _zstd_module()
            decompressor = zstandard.ZstdDecompressor()
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                decompressor.copy_stream(src, dst, read_size=_CHUNK, write_size=_CHUNK)
        elif head.startswith(GZIP_MAGIC):
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK)
        else:
            shutil.copyfile(source, tmp)

        # **zstd does not raise on a truncated frame; it stops silently.**
        # Measured 2026-09-05 with a real demo: a half-truncated
        # ``ANCIENT_vs_RCAVE_VETERANS.dem.zst`` decompressed to 104 464 384
        # bytes without an error, even though the frame declares 208 561 416.
        # The decompressed beginning is a valid CS2 demo -- ``PBDEMS2`` is
        # there, and the map name in the header reads correctly out of it.
        # The shortfall only comes out in parsing.
        #
        # Two checks, and they answer different questions.
        actual = _size(tmp)
        expected = declared_size(source)
        if expected is not None and actual != expected:
            # **The frame itself tells how long it ought to be.** This is the
            # only independent length source a hand-copied file has, and it
            # has been measured to exist in every one of the archive's
            # ``.dem.zst`` files.
            raise ParseError(
                f"Decompressing the demo {source.name} came up short: the "
                f"file declares {expected} bytes decompressed, but it "
                f"decompressed to {actual} bytes.\n"
                "The compressed file is truncated -- either the download "
                "stopped halfway or the copy is still running.\n"
                "The decompressed beginning is a valid demo, so the "
                "shortfall cannot be seen from the file itself.",
                advice=(
                    "Wait until the copy, or the sync client, has finished "
                    "and run the command again. If the file no longer grows, "
                    "download it again -- it is damaged."
                ),
            )
        if source_size > 0 and actual == 0:
            # A frame without a declared size: an empty result is then the
            # only sign that is left. A narrower guard than the one above,
            # and that is why it is a second one.
            raise ParseError(
                f"Decompressing the demo {source.name} failed: the result "
                "was an empty file.\n"
                "The compressed file was truncated mid-download.",
                advice="Download the demo again.",
            )
        os.replace(tmp, target)
    except ParseError:
        _remove(tmp)
        raise
    except Exception as exc:  # noqa: BLE001 - the libraries' errors vary
        _remove(tmp)
        raise ParseError(
            f"Decompressing the demo {source.name} failed: {exc}\n"
            "The file is probably incomplete or corrupt. Download the demo "
            "again."
        ) from exc
    return target


def _remove(path: Path) -> None:
    """Clean up the temporary file; a missing file is not an error."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - a locked file on Windows
        pass


@contextmanager
def readable_demo(path: Path) -> Iterator[Path]:
    """Give out a path to a decompressed demo and clean up after yourself.

    An uncompressed demo is given as it stands -- it is not copied for
    nothing. A compressed demo is decompressed into the machine's temp
    directory and removed at the end, an exception included.

    The header is always checked against the **decompressed** content, so the
    path that comes out of the block is always certainly a CS2 demo. That is
    essential: FACEIT can return an error page from behind a download link,
    and it compresses into a flawless zstd file that is not a demo.

    Raises:
        ParseError: If the file does not exist, the decompression fails or
            the result is not a CS2 demo.
    """
    path = Path(path)
    if not path.is_file():
        raise ParseError(
            f"No demo file was found at the path {path}.\n"
            "Check the path, or copy the demo into the archive's import "
            "directory."
        )

    if not is_compressed(path):
        check_demo_magic(path)
        yield path
        return

    try:
        workdir = Path(tempfile.mkdtemp(prefix="pappascout-decompress-"))
    except OSError as exc:
        raise ParseError(
            f"A temporary directory could not be created for the "
            f"decompression: {exc}\n"
            "Check the disk space and the permissions on the TEMP directory."
        ) from exc

    try:
        target = decompress_to(path, workdir / decompressed_name(path))
        check_demo_magic(target)
        yield target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
