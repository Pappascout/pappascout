"""``import`` -- a hand-downloaded demo into the archive (Story 3.6).

The same end result as ``fetch``, a different source::

    import/{match_id}-{map_no}-{instance}.dem.zst
        ->  <demos_dir>/<map_demo_id>.dem.zst
            <demos_dir>/<map_demo_id>.meta.json

The stage exists because **the network route can be closed**. Measured
2026-09-05: there is no Downloads API authorisation (the application is in the
queue, estimate ~25 September), and ``fetch`` is finished but cannot reach the
network. Signed in through a browser the demo can still be had, and this stage
takes it in.

Six rules this module keeps
---------------------------

**A partial demo does not reach the archive, and its source is not deleted.**
This is the module's most important rule, because breaking it costs
irreplaceable material: ``import/`` holds six of season 12's league demos that
FACEIT no longer offers. Measured 2026-09-05: a ``.dem.zst`` truncated at the
halfway point decompresses **silently** to 104,464,384 bytes (the frame states
208,561,416), and the decompressed start is a valid CS2 demo whose header even
gives the map name correctly. The shortfall therefore shows up in nothing that
looking at the file could establish. There are three guards and they answer
different questions:

``Was the source whole?``
    :func:`~pappascout.adapters.decompress.declared_size` -- the decompressed
    size stated by the zstd frame, which decompression compares against its
    result. In gzip the same job is done by the stream's end marker. An
    uncompressed ``.dem`` has neither, and then ``length_verified`` is
    **false** -- not true.
``Did the source change during the transfer?``
    The size is read before the copy and after it. Explorer writes under the
    final name while the copy is still running and the sync client uploads in
    the background, so a growing file is an everyday situation and not an
    exception.
``Does the copy match the source?``
    The number of bytes written against the source's size. Only after that may
    the source file disappear.

**An imported demo is indistinguishable from a downloaded one.** The file name,
the directory, the metadata file's fields and the write order are the same as
in ``fetch``; the only difference is the value of the ``source`` field, and
that is **traceability information and not control**. No later stage reads it.

**The file name has to describe the content.** The suffix is decided from the
**magic bytes** and not from the name given. An uncompressed ``.dem`` must not
end up in the archive under the name ``.dem.zst``: the name is what every
later reader infers from how to open the file.

**The map name is an observation, the veto data is a cross-check.** A
discrepancy is a confirmation question that ``--yes`` does not skip, and it
is the project's only place where the flag does not skip a question: a
wrongly named demo brings nothing down, it would spoil the report quietly.

**The write goes where the demo already is.** The same rule as in
``stages.fetch`` and for the same reason: if importing always wrote into
``demos_dir()``, a demo in the archive (a synchronised folder, shared) would
stay where it is and get a second copy beside it -- or worse, be deleted as
"replaced", although the difference is the directory and not the file.

**The demo first, the metadata file only after it, and the digest computed
once.**

What this stage does **not** do
-------------------------------
It downloads nothing: the only outgoing call is fetching the match's veto data
from ``MatchSource``, and that comes from the cache. It asks nothing --
questions are a field of :class:`ImportPlan` and the command line puts them.
It deletes nothing from ``import/`` except the file it has just moved. It does
not import demos without a ``match_id``: no FACEIT-shaped ``map_demo_id`` can
be derived for them, and an id convention of our own is its own decision and
not a side effect of this.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pappascout.adapters.decompress import (
    DEMO_MAGIC,
    GZIP_MAGIC,
    ZSTD_MAGIC,
    declared_size,
)
from pappascout.adapters.protocols import DemoParser, Match, MatchSource
from pappascout.archive.atomic_write import atomic_path, atomic_write_json
from pappascout.archive.paths import DEMO_SUFFIXES, ArchivePaths, safe_component
from pappascout.domain.models import Settings
from pappascout.domain.selection import map_demo_id as build_map_demo_id
from pappascout.errors import ApiError, PappascoutError, ParseError
from pappascout.stages import StageResult
from pappascout.stages.fetch import DISK_RESERVE_BYTES, size_fi

__all__ = [
    "STAGE",
    "DEMO_SOURCE",
    "MAX_MAP_NO",
    "Confirmation",
    "ImportPlan",
    "plan",
    "run",
    "candidates",
    "target_suffix",
    "same_map",
    "length_source",
    "unanswered",
    "default_source",
    "default_parser",
]

STAGE = "import"

#: The value of the ``source`` field in the ``.meta.json`` for this stage.
#:
#: The other possible value is ``downloads_api``
#: (:data:`~pappascout.stages.fetch.DEMO_SOURCE`). The field exists so that the
#: difference **can be seen afterwards** -- but no stage of the pipeline may
#: behave differently according to it.
DEMO_SOURCE = "import"

#: The largest map number ``--map`` accepts.
#:
#: **The upper bound exists because without veto data there is no other
#: bound.** When a match's ``map_picks`` is empty (a future match, no veto data
#: yet), nothing else would prevent the value ``--map 99``, and
#: ``{match_id}-98`` would appear in the archive -- an id no stage can attach
#: to anything. Nine is the same number and the same justification as in
#: ``adapters.faceit.MAX_BEST_OF``: it covers BO1, BO3, BO5 and BO7 plus one
#: unknown format. The constant is here rather than imported, because the
#: adapter is on the other side of the port and a stage must not import it.
MAX_MAP_NO = 9

#: The magic bytes and the archive suffix that matches each of them.
_SUFFIX_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (ZSTD_MAGIC, ".dem.zst"),
    (GZIP_MAGIC, ".dem.gz"),
    (DEMO_MAGIC, ".dem"),
)

#: The order in which candidates are recommended in an ambiguous situation.
#:
#: **Compressed first, and that is advice about space rather than taste.**
#: Measured 2026-09-05: the same match is in the import folder both as ``.dem``
#: (233,118,010 bytes) and as ``.dem.zst`` (169,443,262 bytes). Alphabetical
#: order would always name the uncompressed one, so the advice would steer
#: towards writing 64 MB more into the synchronised folder every time -- and
#: nobody would notice, because the advice looks neutral.
_SUFFIX_PREFERENCE: tuple[str, ...] = (".dem.zst", ".dem.gz", ".dem")

#: The transfer's chunk size.
_COPY_CHUNK = 1024 * 1024


# -- The plan ----------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """One question that has to be put before anything is moved.

    **Questions are data and not calls**, because the stage must neither print
    nor read the keyboard: the same stage will later be run behind a web shell,
    and an ``input()`` inside the stage would hang it.

    Attributes:
        question: The question, answerable yes or no.
        detail: The observation that is the reason for asking. Without it the
            question would be unanswerable -- a user cannot weigh a discrepancy
            that is not shown to them.
        forced: Whether the question has to be put **even with the ``--yes``
            flag**. True only for the map check, and that is the epic's own
            requirement.
    """

    question: str
    detail: str
    forced: bool = False


@dataclass(frozen=True)
class ImportPlan:
    """What one import intends to do -- **before it does anything**.

    Attributes:
        map_demo_id: The archive's id, ``{match_id}-{map_index}``.
        match_id: The match's id.
        map_index: The map's **0-based** ordinal.
        source_path: The file being imported from.
        target_path: The file that comes into being. The suffix has been
            decided from the magic bytes and the directory from where a demo
            with the same id already is.
        meta_path: The metadata file, always next to the demo.
        replaces: A demo with the same id **in the same directory but with a
            different suffix** that this import replaces, or ``None``. Never a
            file in another directory and never a file in ``import/`` --
            either would be a different file and not a version to be replaced.
        orphan_meta: A metadata file that would be left describing a file that
            does not exist, or ``None``.
        move: Whether it is moved (true) or copied (false).
        size_bytes: The source file's size at the moment the plan was made. The
            same number is checked again at both ends of the transfer.
        declared_bytes: The decompressed size the file states itself, or
            ``None``.
        length_verified: Whether the source's **wholeness** could be
            established. False for an uncompressed ``.dem``, which has no
            independent source of length at all.
        header_map_name: The map's name from the demo header, **as an
            observation**.
        expected_map_name: The map's name from FACEIT's veto data, or ``None``.
        map_picks: The match's whole veto data, so that the explanation of a
            discrepancy can say **which number would be right** and not only
            that this one is wrong.
        confirmations: The questions in the order they are put.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    source_path: Path
    target_path: Path
    meta_path: Path
    replaces: Path | None = None
    orphan_meta: Path | None = None
    move: bool = True
    size_bytes: int = 0
    declared_bytes: int | None = None
    length_verified: bool = False
    header_map_name: str | None = None
    expected_map_name: str | None = None
    map_picks: tuple[str, ...] = ()
    confirmations: tuple[Confirmation, ...] = ()

    @property
    def map_no(self) -> int:
        """The map's number **as the user counts**, that is, 1-based."""
        return self.map_index + 1

    @property
    def map_matches(self) -> bool:
        """Does the header's map match the veto data? Unknown is not a match."""
        if self.header_map_name is None or self.expected_map_name is None:
            return False
        return same_map(self.header_map_name, self.expected_map_name)


def plan(
    archive: ArchivePaths,
    match_id: str,
    map_no: int | str,
    *,
    source: MatchSource,
    parser: DemoParser,
    file: Path | str | None = None,
    reserve_bytes: int = DISK_RESERVE_BYTES,
    disk_free: Callable[[Path], int | None] | None = None,
) -> ImportPlan:
    """Resolve the whole import -- **without touching a single file**.

    The order of work is cheapest first: the numbers the user gave, then the
    match's veto data from the cache, then finding the source file, then disk
    space, and **only last** reading the header. The last is the only expensive
    step (a compressed demo is decompressed in full into a temporary file), and
    it should not be done for an import that falls over a wrong number -- nor
    onto a disk the decompressed demo does not fit on.

    Args:
        archive: The archive's paths.
        match_id: FACEIT's match id.
        map_no: The map's number **1-based**. A string is accepted, because a
            value coming from the command line can be anything and a wrong
            value has to produce this tool's own error rather than a library's.
        source: The :class:`~pappascout.adapters.protocols.MatchSource` port.
        parser: The :class:`~pappascout.adapters.protocols.DemoParser` port.
        file: The source file named explicitly, or ``None``.
        reserve_bytes: The disk space reserve.
        disk_free: The free-space reader, or ``None`` = :func:`_free_space`.

    Returns:
        :class:`ImportPlan`.

    Raises:
        ~pappascout.errors.PappascoutError: If the import cannot be done at
            all. Every such error carries its own advice; see :func:`_reject`.
    """
    free = disk_free if disk_free is not None else _free_space
    match_id = _match_id(match_id)
    map_index = _map_index(map_no)
    unit = _unit(match_id, map_index)

    expected, picks = _veto(source, match_id, map_index)
    source_path, move = _source_file(archive, match_id, map_index, file)

    suffix = target_suffix(source_path)
    size_bytes = _size(source_path)

    existing = _existing_demo(archive, unit)
    if existing is not None and _same_file(existing, source_path):
        raise _reject(
            f"The source and the target are the same file ({source_path}), so "
            "there is nothing to import.",
            advice=(
                f"Demo {unit} is already in place. Run this on it directly: "
                f"uv run pappascout parse {unit}"
            ),
        )

    # **The write goes where the demo already is** (the same rule as in
    # ``stages.fetch``). If the target were always chosen by ``demos_dir()``, a
    # demo in the archive would stay where it is and get a second copy beside
    # it -- or be deleted as "replaced", although the difference is the
    # directory and not the file.
    target_dir = existing.parent if existing is not None else archive.demos_dir()
    target = target_dir / f"{unit}{suffix}"
    meta = target_dir / f"{unit}.meta.json"

    declared = declared_size(source_path)
    _check_space(
        target_dir, size_bytes, declared, reserve_bytes=reserve_bytes, free=free
    )

    header = _read_map_name(parser, source_path)

    confirmations: list[Confirmation] = []
    cross_check = _cross_check(unit, map_index, header, expected, picks)
    if cross_check is not None:
        confirmations.append(cross_check)
    if existing is not None:
        confirmations.append(_overwrite_question(unit, existing))

    existing_meta = _existing_meta(archive, unit)
    return ImportPlan(
        map_demo_id=unit,
        match_id=match_id,
        map_index=map_index,
        source_path=source_path,
        target_path=target,
        meta_path=meta,
        replaces=existing if existing is not None and existing != target else None,
        orphan_meta=(
            existing_meta
            if existing_meta is not None and existing_meta != meta
            else None
        ),
        move=move,
        size_bytes=size_bytes,
        declared_bytes=declared,
        length_verified=length_source(source_path) is not None,
        header_map_name=header,
        expected_map_name=expected,
        map_picks=tuple(picks),
        confirmations=tuple(confirmations),
    )


# -- The transfer ------------------------------------------------------------


def run(
    archive: ArchivePaths,
    todo: ImportPlan,
    *,
    now: Callable[[], datetime] | None = None,
) -> StageResult:
    """Move the plan's demo into place and write its metadata file.

    **No questions are put here and no answers to them are checked.** Asking
    for permission belongs to the command line, the only layer that has a user.

    Args:
        archive: The archive's paths.
        todo: The plan produced by :func:`plan`.
        now: The clock for the ``fetched_at`` field.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``status`` is ``ok``.
        There is no other status: there is one unit, and its failure is the
        run's failure.

    Raises:
        ~pappascout.errors.PappascoutError: If the transfer fails. **Disk
            errors too**: a full disk and a file lock held by the sync client
            are the user's situations and not programming errors, and without
            translating them they would appear on the screen as "Unexpected
            error: [Errno 28]" with the advice "This is a programming error"
            -- that is, the wrong diagnosis and the wrong action.
    """
    started = time.perf_counter()
    clock = now if now is not None else (lambda: datetime.now(UTC))

    try:
        return _run(archive, todo, clock, started)
    except OSError as exc:
        raise _reject(
            f"Importing demo {todo.map_demo_id} failed with a disk error "
            f"({type(exc).__name__}: {exc}).\n"
            f"The target was {todo.target_path}.\n"
            "The most common causes: the disk filled up during the write, the "
            "sync client held the file locked, or the network drive dropped.\n"
            f"The source file {todo.source_path} was not touched.",
            advice=(
                "Free some disk space or wait until the sync client releases "
                "the file lock, then run the command again."
            ),
        ) from exc


def _run(
    archive: ArchivePaths,
    todo: ImportPlan,
    clock: Callable[[], datetime],
    started: float,
) -> StageResult:
    """See :func:`run`. Separate only so that the ``OSError`` wrapper is one block."""
    digest, size = _transfer(todo.source_path, todo.target_path, todo.size_bytes)

    # **Only here.** The demo is in place and has been read to the end; the
    # metadata file is a claim about this exact file, and it may come into
    # being only once the file has.
    atomic_write_json(
        todo.meta_path,
        {
            "map_demo_id": todo.map_demo_id,
            "sha256": digest,
            "size": size,
            "source": DEMO_SOURCE,
            # **``fetched_at`` and not ``imported_at``.** A name of its own
            # would make an imported demo recognisable from the metadata file's
            # shape, and from recognisability a branch follows before long. The
            # difference is in the ``source`` field and only there.
            "fetched_at": clock().isoformat(),
            # **The same field as in ``fetch`` and with the same meaning:**
            # whether the source's wholeness could be established against an
            # independent number. An uncompressed ``.dem`` has no such number,
            # and then this is false -- unspoken uncertainty would look like
            # certainty.
            "length_verified": todo.length_verified,
        },
    )

    notes: list[str] = []
    if todo.replaces is not None:
        notes.append(_replaced_note(todo))
    if todo.orphan_meta is not None:
        notes.append(_orphan_note(todo))
    notes.append(_source_note(todo))
    if not todo.length_verified:
        notes.append(_unverified_note(todo))
    if not todo.map_matches:
        notes.append(_mismatch_note(todo))

    return StageResult(
        stage=STAGE,
        unit=todo.map_demo_id,
        status="ok",
        skipped=False,
        outputs=_archive_outputs(archive, todo.target_path, todo.meta_path),
        # **No manifest**, for the same reason as in ``fetch``.
        manifest_path=None,
        reason=" ".join(notes) if notes else None,
        duration_s=time.perf_counter() - started,
        stats={
            "map_demo_id": todo.map_demo_id,
            "sha256": digest,
            "size": size,
            "imported_bytes": size,
            # **The name is ``demo_source`` and not ``source``, and that is a
            # condition of the guard.** ``tests/test_stage_import.py`` forbids
            # **reading** the metadata file's ``source`` field anywhere in the
            # source tree -- and a prohibition with an exception list is not a
            # prohibition.
            "demo_source": DEMO_SOURCE,
            "moved": todo.move,
            "length_verified": todo.length_verified,
            "declared_bytes": todo.declared_bytes,
            "demo_path": str(todo.target_path),
            "meta_path": str(todo.meta_path),
            "demos_dir": str(todo.target_path.parent),
            "source_path": str(todo.source_path),
            "header_map_name": todo.header_map_name,
            "expected_map_name": todo.expected_map_name,
            "map_matches": todo.map_matches,
            "notes": tuple(notes),
        },
    )


# -- The ports ---------------------------------------------------------------


def default_source(settings: Settings, archive: ArchivePaths) -> MatchSource:
    """The production FACEIT implementation of the match port.

    The same client and the same cache as in ``discover``: the veto data has
    already been fetched, and importing must not make a second call for it nor
    a second cache key. The import is inside the function so that importing
    this module does not load ``requests``.

    **No Downloads token is needed.** This command does not download demos, so
    a missing token must not block an import -- that is exactly the situation
    the whole stage exists for. The match's veto data, on the other hand, is
    fetched from the API if it is not in the response cache; the Data API key
    is enough for that.
    """
    from pappascout.adapters.faceit import FaceitClient

    return FaceitClient.from_settings(settings, archive.raw_faceit())


def default_parser() -> DemoParser:
    """The production demoparser2 implementation for reading the header.

    **No settings, and that is a contract rather than laziness.** The values in
    the ``[parse]`` section govern first contact, sample points and the point
    cloud -- that is, the work this command does not do. Passing the section in
    here would tie importing to settings that cannot affect its result (AD-3).
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    return Demoparser2Adapter()


# -- Finding the source file -------------------------------------------------


def candidates(
    archive: ArchivePaths, match_id: str, map_index: int
) -> tuple[Path, ...]:
    """The files in the import folder that could be this map.

    The search uses **FACEIT's own naming pattern** ``{match_id}-{round}-*``,
    where ``round`` is the 1-based map number -- that is, ``map_index + 1``.

    **There is an assumption to record here (Story 3.6, review point B1).** The
    pattern assumes that FACEIT's ``instances[].round`` is the same number as
    the position in the ``voting.map.pick`` list + 1. Chapter 9 of
    ``mittaus-faceit-aineisto.md`` says that ``round`` is **a value that is
    read and not a list position**, and that the contents of the pick list for
    a BO3 that ended 2-0 are **still unmeasured** -- if an unplayed map stays
    in the pick list, the position and ``round`` differ. The right fix would be
    to carry ``instances`` through
    :class:`~pappascout.adapters.protocols.Match`, which the port does not
    currently do; that is a change to the port and not this story's business.
    Until then the assumption is **visible** here, and the map check is what
    falls over in the safe direction: a wrong position produces a discrepancy,
    and a discrepancy is a question ``--yes`` does not skip -- a question
    that also says under which number the header's map is found in the veto
    (:func:`_other_pick`).

    Returns **all** the hits and does not choose between them. Measured
    2026-09-05: one match is in the import folder both as ``.dem`` and as
    ``.dem.zst``.
    """
    directory = archive.import_dir()
    if not directory.is_dir():
        return ()
    pattern = f"{safe_component(match_id, 'match_id')}-{map_index + 1}-*"
    return tuple(
        sorted(
            path
            for path in directory.glob(pattern)
            if path.is_file() and _is_demo_name(path.name)
        )
    )


def target_suffix(path: Path) -> str:
    """The archive suffix for a file **based on its content**.

    The magic bytes and not the name given: a hand-downloaded file can be named
    anything at all, and the name is what every later reader infers from how to
    open the file.

    Returns:
        ``".dem.zst"``, ``".dem.gz"`` or ``".dem"``.

    Raises:
        ~pappascout.errors.PappascoutError: If the first bytes are of no known
            format.
    """
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(DEMO_MAGIC))
    except OSError as exc:
        raise _reject(
            f"The file {path} could not be opened: {exc}",
            advice=(
                "Check the path, and check that the file is not a cloud "
                "placeholder -- open it once in File Explorer."
            ),
        ) from exc

    for magic, suffix in _SUFFIX_BY_MAGIC:
        if head.startswith(magic):
            return suffix

    raise _reject(
        f"The file {path.name} is neither a demo nor a compressed demo: its "
        f"first bytes are {head!r}.\n"
        f"Either zstd ({ZSTD_MAGIC!r}), gzip ({GZIP_MAGIC!r}) or a CS2 demo "
        f"header ({DEMO_MAGIC!r}) was expected.\n"
        "Nothing was moved.",
        advice=(
            "Check that the download succeeded and that the file is a FACEIT "
            "demo recording and not, say, a sign-in page or an empty file."
        ),
    )


def length_source(path: Path) -> str | None:
    """What says whether the source was **whole**? ``None`` = nothing does.

    Three formats, two answers:

    ``.dem.zst``
        The frame's ``Frame_Content_Size``, if it is stated. Decompression
        compares its result against it
        (:func:`~pappascout.adapters.decompress.decompress_to`), so a truncated
        file falls over before it is moved.
    ``.dem.gz``
        The gzip stream's end marker. Measured 2026-09-05: a truncated gzip
        raises ``EOFError`` during decompression, so the same guard works by a
        different mechanism.
    ``.dem``
        **Nothing.** An uncompressed demo has no length, no checksum and no end
        marker, so half a file is indistinguishable from a whole one. The right
        answer is ``None`` and ``length_verified: false`` -- not true. In
        ``fetch`` the same role belongs to ``Content-Length``, and it writes
        the same field false when there was no header.
    """
    path = Path(path)
    if declared_size(path) is not None:
        return "the decompressed size stated by the zstd frame"
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(GZIP_MAGIC))
    except OSError:  # pragma: no cover - depends on the file system
        return None
    if head.startswith(GZIP_MAGIC):
        return "the gzip stream's end marker"
    return None


def same_map(observed: str, expected: str) -> bool:
    """Do the header's and the veto's map names mean the same map?

    The comparison ignores case and surrounding whitespace, **but nothing
    else**. It does not strip the ``de_`` prefix and does not look for
    synonyms: measured 2026-09-05, ``voting.map.pick`` gives the names in the
    same form as the demo header (``de_ancient``, ``de_nuke``), so any other
    "tidying" would be a guess -- and a guess is exactly what this check exists
    to prevent.
    """
    return observed.strip().casefold() == expected.strip().casefold()


def unanswered(
    confirmations: Sequence[Confirmation], *, yes: bool
) -> tuple[Confirmation, ...]:
    """The questions that **have to be put** with the given flag.

    One place rather than a condition at the call site, because the rule is the
    epic's own requirement and therefore exactly the one that has to be
    testable on its own: ``--yes`` skips the questions **except** those where
    ``forced`` is true.
    """
    if not yes:
        return tuple(confirmations)
    return tuple(item for item in confirmations if item.forced)


# -- Internals: rejections ---------------------------------------------------


def _reject(message: str, *, advice: str) -> PappascoutError:
    """A rejection that **cannot come into being without advice**.

    The same guard as in ``stages.fetch._result`` and from the same root cause
    (Story 3.4): when the advice comes from a heading or from a default, every
    new class of fault inherits it silently -- and inherited advice is advice
    nobody has considered for this particular fault.

    **Every error rising out of this module has to go through here.** Story
    3.6's review found a path that did not: the id was built with
    ``safe_component`` directly, and ``archive.paths`` raises its own error
    without advice. See :func:`_unit`.
    """
    if not advice.strip():
        raise AssertionError(
            f"A rejection without advice: {message!r}. A failure without a "
            "next step would leave the user guessing."
        )
    return PappascoutError(message, advice=advice)


def _match_id(value: str) -> str:
    """Check that the match id is usable as part of a path."""
    text = str(value).strip()
    if not text:
        raise _reject(
            "The match id is missing.",
            advice=(
                "Give the match's id: --match 1-<uuid>. The ids are in the "
                "index/matches.json file."
            ),
        )
    try:
        return safe_component(text, "match_id")
    except PappascoutError as exc:
        raise _reject(
            str(exc),
            advice=(
                "Copy the id as it stands from the index/matches.json file or "
                "from the address of FACEIT's match page."
            ),
        ) from exc


def _map_index(map_no: int | str) -> int:
    """``--map`` from 1-based to 0-based, or this tool's own rejection.

    **The value is taken as a string, and that is deliberate.** If the command
    line declared it an integer, ``--map abc`` would fall over in ``typer``'s
    own message before this function saw anything -- and this function's own
    check would be dead code, unreachable from the command line. Story 3.6's
    review found exactly that state.

    The numbering starts at one **for the user** and at zero **in the
    recordings**. Zero is therefore the most typical mistake, and it has to get
    a sentence of its own. The upper bound is :data:`MAX_MAP_NO`; see its
    justification.
    """
    try:
        number = int(str(map_no).strip())
    except (TypeError, ValueError):
        raise _reject(
            f"The map number {map_no!r} is not a whole number.",
            advice="Give the map number as a number, for example --map 1.",
        ) from None
    if number < 1:
        raise _reject(
            f"The map number is {number}, but the numbering starts at one: "
            "the match's first map is --map 1.",
            advice="Run the command again with --map 1 or higher.",
        )
    if number > MAX_MAP_NO:
        raise _reject(
            f"The map number is {number}, but at most {MAX_MAP_NO} maps are "
            "played in a match.\n"
            "Without the match's veto data the number cannot be checked "
            "against it, so this is the only bound there is.",
            advice=(
                f"Give a --map between 1 and {MAX_MAP_NO}. If the number was "
                "right, the match id is wrong."
            ),
        )
    return number - 1


def _unit(match_id: str, map_index: int) -> str:
    """Build the ``map_demo_id`` with **the domain's canonical builder**.

    :func:`pappascout.domain.selection.map_demo_id` is the place where the id
    comes into being, and its own documentation says so. In Story 3.6's review
    importing was the project's only place that built the id around it -- and
    going around it cost two things: the domain's own ``index < 0`` check, and
    the fact that the path check did not go through :func:`_reject`. The latter
    produced a **genuinely reachable** rejection without advice: a 119-character
    ``--match`` passes :func:`_match_id` but exceeds the length limit for a
    path component while the id is being built, and the message named the id
    ``map_demo_id``, which the user never gave.
    """
    try:
        unit = build_map_demo_id(match_id, map_index)
    except ValueError as exc:  # pragma: no cover - _map_index prevents this
        raise _reject(
            f"The id could not be built: {exc}",
            advice="Check --match and --map.",
        ) from exc
    try:
        return safe_component(unit, "map_demo_id")
    except PappascoutError as exc:
        raise _reject(
            f"The archive id formed from match id {match_id!r} and map "
            f"{map_index + 1} is not usable as a file name: {exc}",
            advice=(
                "Check --match: FACEIT's match id has the shape 1-<uuid>, that "
                "is 38 characters, and it has no spaces and no path "
                "separators."
            ),
        ) from exc


# -- Internals: the match and its veto data ----------------------------------


def _veto(
    source: MatchSource, match_id: str, map_index: int
) -> tuple[str | None, tuple[str, ...]]:
    """The map's name from the veto data **and the whole veto data**.

    Both are returned, because the explanation of a discrepancy needs the list:
    if the header's map is in the veto under some other number, that number is
    exactly what the user needs (:func:`_other_pick`).

    The call goes through the cache and is not a demo download.

    **The assumption about positions is the same as in :func:`candidates` and
    it is recorded there.** Here ``picks[map_index]`` is read, that is, the
    veto's list position is assumed to match the map number.

    Returns:
        ``(the map's name or None, the whole pick list)``. An empty list means
        "no veto data", not "no maps".
    """
    match = _match(source, match_id)
    picks = tuple(match.map_picks)
    if not picks:
        return None, ()
    if map_index >= len(picks):
        listing = ", ".join(f"{i + 1}. {name}" for i, name in enumerate(picks))
        raise _reject(
            f"Match {match_id} has {len(picks)} maps ({listing}), so map "
            f"{map_index + 1} does not exist.\n"
            "Nothing was moved.",
            advice=(
                f"Give a --map between 1 and {len(picks)}. If the map is not "
                "on the list, the match's veto data is different from what you "
                "thought -- check the match id."
            ),
        )
    return picks[map_index], picks


def _match(source: MatchSource, match_id: str) -> Match:
    """Fetch the match from the port and turn its absence into a user error."""
    try:
        return source.get_match(match_id)
    except ApiError as exc:
        raise _reject(
            f"Match {match_id} could not be fetched: {exc}\n"
            "Nothing was moved.",
            advice=(
                "Check the match id. The known matches are in the archive's "
                "index/matches.json file; if there is no such file, run "
                "uv run pappascout discover first."
            ),
        ) from exc


# -- Internals: the source file ----------------------------------------------


def _source_file(
    archive: ArchivePaths,
    match_id: str,
    map_index: int,
    file: Path | str | None,
) -> tuple[Path, bool]:
    """Where the import comes from, and whether it moves or copies.

    Returns:
        ``(path, whether_it_moves)``. A file in the ``import/`` folder is moved
        -- it is an inbox and not a store. A file given from anywhere else is
        **copied**: it is the user's own file in the user's own place, and
        deleting it is their decision and not a side effect of importing.
    """
    if file is not None:
        path = Path(file).expanduser()
        if not path.is_file():
            raise _reject(
                f"There is no file {path}.",
                advice=(
                    "Check the path, or leave --file out and the file will be "
                    "looked for in the archive's import folder."
                ),
            )
        return path, _inside(path, archive.import_dir())

    found = candidates(archive, match_id, map_index)
    if not found:
        raise _reject(
            f"In the import folder {archive.import_dir()} there is no file "
            f"named {match_id}-{map_index + 1}-*.dem[.zst|.gz].\n"
            f"{_import_listing(archive)}",
            advice=(
                "Copy the browser-downloaded demo into the import folder under "
                "its original name, or give its path directly: --file <path>."
            ),
        )
    if len(found) > 1:
        listing = "\n".join(f"    {p.name} ({size_fi(_size(p))})" for p in found)
        raise _reject(
            f"The import folder holds {len(found)} files for map "
            f"{map_index + 1} and no choice between them can be made for "
            f"you:\n{listing}\n"
            "Nothing was moved.",
            advice=_pick_advice(found),
        )
    return found[0], True


def _pick_advice(found: Sequence[Path]) -> str:
    """Advice that **can be copied as it stands and steers towards the compressed one**.

    Two faults in one line, and both are hit on the first real run:

    **The quotation marks.** The archive's path contains spaces (in the
    synchronised folder's name and in the project folder), so an unquoted path
    breaks up into several arguments in the shell and produces a ``typer``
    error. Advice that cannot be copied is not advice.

    **The order.** Alphabetical order would always name ``.dem`` before
    ``.dem.zst``, so the advice would steer towards importing the uncompressed
    one -- measured 2026-09-05: the same match is 233 MB uncompressed and
    169 MB compressed, and the difference stays in the synchronised folder for
    good. See :data:`_SUFFIX_PREFERENCE`.
    """
    ordered = sorted(found, key=_suffix_rank)
    return f'Say which one to import: --file "{ordered[0]}"'


def _suffix_rank(path: Path) -> int:
    """The place in :data:`_SUFFIX_PREFERENCE`; unknown goes last."""
    lowered = path.name.lower()
    for index, suffix in enumerate(_SUFFIX_PREFERENCE):
        if lowered.endswith(suffix):
            return index
    return len(_SUFFIX_PREFERENCE)


def _import_listing(archive: ArchivePaths) -> str:
    """What is in the import folder -- **by name, not as a count**."""
    directory = archive.import_dir()
    if not directory.is_dir():
        return f"The import folder {directory} does not exist yet."
    names = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and _is_demo_name(path.name)
    )
    if not names:
        return "There is not a single demo file in the import folder."
    listing = "\n".join(f"    {name}" for name in names)
    return f"The import folder holds these demos:\n{listing}"


def _read_map_name(parser: DemoParser, path: Path) -> str | None:
    """The map's name from the header, and a read error **with advice**.

    This is also the place where a truncated ``.dem.zst`` falls over:
    decompression compares its result against the size the frame states, and
    the difference rises as a ``ParseError`` **before anything has been
    moved**. A partial demo's own advice comes from decompression and differs
    from this function's general advice ("wait until the copy is finished"
    versus "check that the file is a demo"), so it is kept as it stands.
    """
    try:
        return parser.read_map_name(path)
    except ParseError as exc:
        advice = getattr(exc, "advice", None)
        raise ParseError(
            f"The demo header could not be read from the file {path.name}:\n"
            f"{exc}\n"
            "Nothing was moved, and the source file was not touched.",
            advice=advice
            or (
                "Check that the download succeeded in full and that the file "
                "is a CS2 demo. If need be, wait until the sync client has "
                "finished."
            ),
        ) from exc


# -- Internals: the archive's current state ----------------------------------


def _writable_demo_dirs(archive: ArchivePaths) -> tuple[Path, ...]:
    """The directories a demo **managed by the archive** can be in.

    The same order as in
    :meth:`~pappascout.archive.paths.ArchivePaths.demo_dirs` but **without
    ``import/``**, and that difference is the whole reason for the function.
    ``demo_dirs`` is the search order for ``parse``: to it, a hand-carried demo
    in the import folder is as good as one in the archive. For importing it is
    a different matter. ``import/`` is an inbox, not a target:

    * nothing is written there, so it cannot be the ``target_dir``,
    * a file of the same name there is not a "version to be replaced" but
      another file that importing does not own -- and when importing from
      elsewhere with ``--file`` the old code would have **deleted it**, which
      breaks the spec's Never rule "nothing is written anywhere but demos/".
    """
    return tuple(
        directory
        for directory in archive.demo_dirs()
        if directory != archive.import_dir()
    )


def _existing_demo(archive: ArchivePaths, unit: str) -> Path | None:
    """A demo with the same id in the directories the archive manages."""
    for directory in _writable_demo_dirs(archive):
        for suffix in DEMO_SUFFIXES:
            candidate = directory / f"{unit}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _existing_meta(archive: ArchivePaths, unit: str) -> Path | None:
    """A metadata file with the same id in the directories the archive manages."""
    for directory in _writable_demo_dirs(archive):
        candidate = directory / f"{unit}.meta.json"
        if candidate.is_file():
            return candidate
    return None


# -- Internals: disk space ---------------------------------------------------


def _free_space(directory: Path) -> int | None:
    """Free space **on the nearest directory that exists**.

    The same shape as in ``stages.fetch.free_space``: the target directory may
    come into being only with the write, and the free space of a path that does
    not exist cannot be asked for. ``None`` means "could not be determined" and
    not zero -- an unknown amount must not block an import.
    """
    path = Path(directory)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:  # pragma: no cover - depends on the file system
        return None


def _check_space(
    target_dir: Path,
    size_bytes: int,
    declared: int | None,
    *,
    reserve_bytes: int,
    free: Callable[[Path], int | None],
) -> None:
    """Space for **two** different writes, and they can be on different disks.

    Importing writes hundreds of megabytes twice, and only one of them is
    obvious:

    1. **TEMP**, when the header is read: the compressed demo is decompressed
       in full into the machine's own temp directory. The decompressed size is
       208-316 MB, and it is known in advance
       (:func:`~pappascout.adapters.decompress.declared_size`).
    2. **The target directory**, when the file is copied into place.

    Without the check a full disk produces an ``OSError`` in the middle of a
    write and the text "Unexpected error: [Errno 28]" on the screen -- and if
    TEMP fills up while the header is being read, decompression's own advice is
    "download the demo again", which sends the user off to fetch a 230 MB file
    that is perfectly fine. Both are the wrong diagnosis and the wrong action.
    """
    if declared is not None:
        temp_dir = Path(tempfile.gettempdir())
        available = free(temp_dir)
        if available is not None and available < declared + reserve_bytes:
            raise _reject(
                "Not enough disk space to read the demo's header, so the "
                "import was not started.\n"
                f"The compressed demo is decompressed temporarily into the "
                f"directory {temp_dir}, and decompressed it is "
                f"{size_fi(declared)}.\n"
                f"Free space is {size_fi(available)} and "
                f"{size_fi(declared + reserve_bytes)} is needed with the "
                "reserve.\n"
                "The source file was not touched.",
                advice=(
                    f"Free some disk space on the drive of {temp_dir} or point "
                    "TEMP at another disk, then run the command again."
                ),
            )

    available = free(target_dir)
    if available is not None and available < size_bytes + reserve_bytes:
        raise _reject(
            f"Not enough disk space to import the demo into the directory "
            f"{target_dir}, so the import was not started.\n"
            f"Free space is {size_fi(available)} and "
            f"{size_fi(size_bytes + reserve_bytes)} is needed (the demo "
            f"{size_fi(size_bytes)} + the reserve {size_fi(reserve_bytes)}).\n"
            "The source file was not touched.",
            advice=(
                "Free at least "
                f"{size_fi(size_bytes + reserve_bytes - available)} of disk "
                "space and run the command again."
            ),
        )


# -- Internals: the questions ------------------------------------------------


def _cross_check(
    unit: str,
    map_index: int,
    header: str | None,
    expected: str | None,
    picks: Sequence[str],
) -> Confirmation | None:
    """The question about the map matching, or ``None`` when everything matches.

    Three different situations, **one shared consequence**: the question is
    put, and ``--yes`` does not skip it. They are still kept apart in the
    text, because the user's next step differs.

    The only case where nothing is asked is the one where **both observations
    exist and agree**.
    """
    map_no = map_index + 1
    if expected is None:
        return Confirmation(
            question=f"Import {unit} anyway?",
            detail=(
                f"The match has no veto data, so the name of map {map_no} "
                "could not be cross-checked. The demo header says "
                f"{_map_text(header)}, and there is nothing to compare it "
                "with.\n"
                "If the number is wrong, the demo is stored under the wrong "
                "map's name and the mistake does not show until the report's "
                "map distribution is wrong."
            ),
            forced=True,
        )
    if header is None:
        return Confirmation(
            question=f"Import {unit} anyway?",
            detail=(
                "The demo header carries no map name, so it could not be "
                f"compared against the veto data (map {map_no} is "
                f"{expected}).\n"
                "The file may still be the right one, but that cannot be "
                "established from here."
            ),
            forced=True,
        )
    if same_map(header, expected):
        return None

    other = _other_pick(header, picks)
    hint = (
        f"\nIn the match's veto data {header} is map {other} -- if that is the "
        f"one you are importing, the right value is --map {other}."
        if other is not None
        else "\nThe header's map is not in the match's veto data at all, so "
        "the file is most likely from a different match."
    )
    return Confirmation(
        question=f"Import {unit} anyway?",
        detail=(
            f"The map does not match: the demo header says {header}, but "
            f"according to the match's veto data map {map_no} is "
            f"{expected}.{hint}\n"
            "A demo stored under the wrong name brings nothing down -- it "
            "spoils the report quietly."
        ),
        forced=True,
    )


def _other_pick(header: str | None, picks: Sequence[str]) -> int | None:
    """Where in the veto data is the header's map found, if it is found elsewhere?

    **A question with no option to answer is a poor question.** When the map
    does not match, the most likely cause is a wrong ``--map`` -- and if the
    header's map is in the veto under some other number, that number is exactly
    what the user needs. Without it they have no correct value at all, and the
    temptation to answer "k" is strong.

    Returns:
        A 1-based map number, or ``None``.
    """
    if header is None:
        return None
    for index, name in enumerate(picks):
        if same_map(header, name):
            return index + 1
    return None


def _overwrite_question(unit: str, existing: Path) -> Confirmation:
    """The question about replacing a demo that is already in the archive.

    ``forced`` is **false**, and that is a row of the frozen I/O matrix ("The
    target is already in the archive ... ``--yes`` skips this"). The review
    took issue with the justification, and rightly: the earlier text said the
    file being replaced could be "fetched again", and that is not the case --
    there is no Downloads authorisation, and that is exactly why this command
    exists.

    The right justification is a different one and it is structural: the file
    being replaced is **the same unit's** demo in the same directory, the
    replacing content has been checked whole before the transfer, and the
    transfer is atomic -- so the question is "shall this version be replaced by
    this version", not "shall something else be destroyed". The map check is a
    different matter: there a wrong answer stores the demo under **the wrong
    map's name**, and nothing shows it.
    """
    return Confirmation(
        question=f"Replace {unit}?",
        detail=(
            f"Demo {unit} is already on disk: {existing}\n"
            f"Size {size_fi(_size(existing))}."
        ),
        forced=False,
    )


# -- Internals: the notes ----------------------------------------------------


def _replaced_note(todo: ImportPlan) -> str:
    """Removing the replaced file -- **and its real reason**.

    Two review findings in one line. First, the return value of the removal is
    read: :func:`_remove`'s own documentation says failing is ordinary on
    Windows, and if the note reported a removal that did not happen, the
    archive would be left with two files for the same id -- ``find_demo`` would
    return **the old one** of them in ``DEMO_SUFFIXES`` order, and ``parse``
    would read the digest from the metadata without checking it against that
    file.

    Second, the reason is now true. The earlier text said "because the new file
    has a different suffix" even when the suffixes were identical and the
    difference was the directory. Now the target is chosen by where the demo
    already is, so the difference **is** the suffix -- and only that.
    """
    assert todo.replaces is not None
    if _remove(todo.replaces):
        return (
            f"The replaced demo {todo.replaces.name} was removed, because the "
            f"new file is in the same directory with a different suffix "
            f"({todo.target_path.name}) and the two must not be left side by "
            "side for the same id."
        )
    return (
        f"WARNING: the old demo {todo.replaces} could not be removed (a file "
        "lock held by the sync client, for instance), so there are now two "
        f"files for the same id: {todo.replaces.name} and "
        f"{todo.target_path.name}. The metadata file describes the new one. "
        "Delete the old one by hand before you run the parse command."
    )


def _orphan_note(todo: ImportPlan) -> str:
    """Removing an orphaned metadata file.

    The same rule as in ``stages.fetch``: a metadata file left describing a
    file that does not exist is a claim about a digest -- and ``parse`` reads
    the digest from the **first metadata file it finds**. The wrong metadata in
    the wrong directory would make a fresh demo look up to date against an old
    demo's result.
    """
    assert todo.orphan_meta is not None
    if _remove(todo.orphan_meta):
        return (
            f"The old metadata file in directory {todo.orphan_meta.parent} was "
            "removed, because it described a file that is not there."
        )
    return (
        f"WARNING: the old metadata file {todo.orphan_meta} could not be "
        "removed, and it describes a file that does not exist. Delete it by "
        "hand -- otherwise parse may read the digest from the wrong file."
    )


def _source_note(todo: ImportPlan) -> str:
    """What was done to the source file."""
    if not todo.move:
        return (
            f"The source file {todo.source_path} was left where it is, because "
            "it is not in the archive's import folder -- it was copied, not "
            "moved."
        )
    if _remove(todo.source_path):
        return (
            f"The source file {todo.source_path.name} was removed from the "
            "import folder."
        )
    return (
        f"The source file {todo.source_path} could not be removed (a file lock "
        "held by the sync client, for instance). The demo is in the archive "
        "all the same; delete the source by hand if you want to free the "
        "space."
    )


def _unverified_note(todo: ImportPlan) -> str:
    """What **could not** be established about the source -- said out loud.

    The same rule and the same justification as in
    ``stages.fetch._unverified_note``: unspoken uncertainty would look like
    certainty. An uncompressed ``.dem`` has no length, no checksum and no end
    marker, so half a file is indistinguishable from a whole one -- and that
    would only show up in parsing.
    """
    return (
        f"The file {todo.source_path.name} carries no length information (an "
        "uncompressed demo has no frame size, no checksum and no end marker), "
        "so its wholeness could not be established. The copy matches the "
        "source byte for byte, but if the source was partial, that will only "
        "show up in parsing. In the metadata file length_verified is false."
    )


def _mismatch_note(todo: ImportPlan) -> str:
    """A confirmed map discrepancy, into the result's ``reason``.

    The question was answered yes, but the observation does not disappear with
    the answer: if the report's map distribution looks odd later, this line is
    what says why.
    """
    return (
        "The map was left unchecked or differed: the header says "
        f"{_map_text(todo.header_map_name)}, the veto data "
        f"{_map_text(todo.expected_map_name)}. The import was made on the "
        "user's confirmation."
    )


def _map_text(value: str | None) -> str:
    """A map name for the output; a missing one must be said, not left blank."""
    return value if value else "(no name)"


# -- Internals: the transfer -------------------------------------------------


def _transfer(source: Path, target: Path, expected_size: int) -> tuple[str, int]:
    """Copy the source to the target and compute the digest **from the same chunk**.

    Four rules in one block:

    1. **The digest once.** The same chunk goes both into the
       ``hashlib.sha256`` object and into the file. Computing it afterwards
       would be a second 230 MB pass over the disk.
    2. **The write is atomic.** The temporary file is moved only once the
       stream has been read to the end.
    3. **``os.rename`` will not do**, even if the source and the target are on
       the same disk: it would be faster, but the digest would be left
       uncomputed and would need a read pass of its own -- that is, exactly the
       work rule 1 forbids.
    4. **The source's size is checked at both ends.** Explorer writes under the
       final name while the copy is still running and the sync client uploads
       in the background, so a growing file is an everyday situation. Without
       this a half file would be copied and **a whole source deleted** -- and
       ``import/`` holds six demos that FACEIT no longer offers.

    Args:
        source: The source file.
        target: The target.
        expected_size: The source's size at the moment the plan was made.

    Returns:
        ``(sha256 as hex, bytes)``.

    Raises:
        ~pappascout.errors.PappascoutError: If the source changed during the
            transfer. Nothing is left at the target and the source is not
            touched.
    """
    digest = hashlib.sha256()
    written = 0
    with atomic_path(target) as tmp:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            while chunk := src.read(_COPY_CHUNK):
                digest.update(chunk)
                dst.write(chunk)
                written += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = _size(source)
        if written != expected_size or after != expected_size:
            raise _reject(
                f"The source file {source.name} changed during the transfer: "
                f"the size was {expected_size} bytes before the copy, {after} "
                f"bytes after it, and {written} bytes were copied.\n"
                "The file is most likely being written right now (a copy in "
                "File Explorer, or the sync client still uploading).\n"
                "Nothing was moved into the archive and the source file was "
                "not touched.",
                advice=(
                    "Wait until the file's size stops changing and run the "
                    "command again."
                ),
            )
    return digest.hexdigest(), written


def _size(path: Path) -> int:
    """The file's size in bytes."""
    return Path(path).stat().st_size


def _remove(path: Path) -> bool:
    """Delete a file; failing is **information** and not an exception.

    A file lock held by the sync client and an open handle from the antivirus
    are both ordinary on Windows, and neither means the import failed: the demo
    is in the archive and its metadata file is beside it.

    **The return value must be read.** The review found a call site that threw
    it away and wrote the note "was removed" before the attempt; see
    :func:`_replaced_note`.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:  # pragma: no cover - depends on the file system
        return False


def _inside(path: Path, directory: Path) -> bool:
    """Is ``path`` inside the directory ``directory``?"""
    try:
        resolved = path.resolve()
        base = directory.resolve()
    except OSError:  # pragma: no cover - depends on the file system
        return False
    return base == resolved.parent or base in resolved.parents


def _same_file(left: Path, right: Path) -> bool:
    """Do two paths point at the same file?"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - depends on the file system
        return False


def _is_demo_name(name: str) -> bool:
    """Does the name carry a demo suffix? Case decides nothing.

    This is a check on the name and not on the content: it narrows the search
    and the listing, and the content is decided by :func:`target_suffix` from
    the magic bytes.
    """
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in DEMO_SUFFIXES)


def _archive_outputs(
    archive: ArchivePaths, *paths: Path
) -> tuple[PurePosixPath, ...]:
    """The files written, in ``StageResult.outputs``'s form.

    The same rule and the same justification as in
    ``stages.fetch._archive_outputs``: by contract ``outputs`` is **a path
    relative to the inside of the archive**, and ``[project].demos_root`` is
    outside the archive.
    """
    inside: list[PurePosixPath] = []
    for path in paths:
        try:
            inside.append(archive.relative(path))
        except ValueError:
            continue
    return tuple(inside)
