"""``fetch`` -- the pipeline's third stage at the head: selected demos into the archive.

``select`` can say which MapDemos belong to the sample. This stage fetches
them::

    index/selections/<team_key>.json  ->  <demos_dir>/<map_demo_id>.dem.zst
                                          <demos_dir>/<map_demo_id>.meta.json

``<demos_dir>`` is ``PAPPASCOUT_DEMOS_ROOT`` when that variable is set,
otherwise the archive's own ``demos/``. **Decided 2026-09-05:** a demo is
large (142-223 MB) and can be fetched again, its parsed result is small
(about 1 MB) and irreplaceable -- so the large one stays on the local disk
and the small one stays in the synchronised folder. The metadata file always
goes **next to the demo**: it is a claim about that exact file, and in
separate directories the two would drift apart. Idempotence looks at both
locations, so setting the variable does not download anything again.

The stage is **per unit**: one call, one MapDemo, one
:class:`~pappascout.stages.StageResult`. The series is run by :func:`run_many`,
and that is exactly why one demo's failure cannot bring down the others -- a
failure is a return value and not an exception (AD-9).

Six rules this module keeps
---------------------------

**The signed link is not here at all.** The port
(:class:`~pappascout.adapters.protocols.DemoSource`) takes a ``map_demo_id``
and returns the bytes; the address is resolved by the adapter and does not
cross the port. This stage therefore cannot write the link into the metadata
file, into a log or into an error message -- not because it remembers not to,
but because it does not have the link.

**The digest is computed while the bytes are written.** The same chunk goes at
the same time into the ``hashlib.sha256`` object and into the file. Reading
200 MB again for hashing would be a second pass over the disk for every demo
-- and in an archive inside a synchronised folder also a second fetch out of
the cloud. ``parse`` reads the digest from the ``.meta.json`` rather than
computing it again.

**The write order is the demo first, the metadata file only after it.** That
way the metadata file never describes a file that does not exist. An
interrupted run leaves a demo without its metadata, and that is repaired by
one re-download; the other way round would leave in the archive a metadata
file claiming a digest for a file that is not there -- and in ``parse``'s
input list an id that matches nothing.

**An unfinished download is not moved into place.** The write goes into
:func:`~pappascout.archive.atomic_write.atomic_path`'s temporary file, and
``os.replace`` happens only once the stream has been read to the end and the
length checked. A broken connection raises **before** the move, so the
temporary file is cleaned up and nothing appears in ``demos/``.

**Disk space is checked before the download, not after it.** Measured
2026-09-05: drive C had 9.9 GB free out of 236 GB (96% in use) and one
compressed demo is 142-223 MB. A check made afterwards would report that the
disk filled up -- that is not a check but a report of the damage.

**A demo already in the archive is skipped.** The stage is safe to run at any
time: it is the precondition for the whole collect command (Story 3.5).

What this stage does **not** do
-------------------------------
It does not call ``parse`` or any other stage -- the user decides the order one
command at a time. It does not change the files under ``index/``: it is their
reader.
It does not decompress ``.dem.zst`` into ``.dem`` (two copies of the same demo
would double the disk usage; ``parse`` decompresses itself when it needs to)
and it does not delete demos after parsing -- that is its own story
(``prune``).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pappascout.adapters.decompress import ZSTD_MAGIC
from pappascout.adapters.protocols import DemoSource
from pappascout.archive.atomic_write import (
    atomic_path,
    atomic_write_json,
    temp_suffix,
)
from pappascout.archive.paths import ArchivePaths, safe_component
from pappascout.domain.models import LeagueSettings, Settings
from pappascout.domain.selection import map_demo_id
from pappascout.errors import (
    ApiError,
    DemoUnavailable,
    DownloadsAccessDenied,
    PappascoutError,
    SettingsError,
)
from pappascout.stages import StageResult
from pappascout.stages.select import read_selection

__all__ = [
    "STAGE",
    "DEMO_SOURCE",
    "DEMO_SIZE_ESTIMATE_BYTES",
    "MIN_PLAUSIBLE_DEMO_BYTES",
    "DISK_RESERVE_BYTES",
    "FetchPlan",
    "plan",
    "CollectPlan",
    "NoVetoMatch",
    "plan_division",
    "resolve_team_key",
    "in_archive",
    "free_space",
    "run",
    "run_many",
    "default_source",
    "size_fi",
]

STAGE = "fetch"

#: The value of the ``source`` field in the ``.meta.json`` for this stage.
#:
#: The other possible value is ``import`` (Story 3.6). The field exists so that
#: the difference between a hand-imported and a downloaded demo **can be seen
#: afterwards** -- but no stage of the pipeline may behave differently
#: according to it: an imported demo behaves exactly like a downloaded one.
DEMO_SOURCE = "downloads_api"

#: The estimated size of one compressed demo in bytes, for the disk check.
#:
#: Measured 2026-09-05 over the archive's demos: the largest compressed file is
#: 234,163,493 bytes, that is 223.3 MiB. **The estimate is above the measured
#: upper bound and not an average**, because the cost of the check being wrong
#: is asymmetric: too small an estimate lets a download start onto a disk it
#: does not fit on, and the write breaks against a full disk; too large an
#: estimate refuses a download that would have fitted, and the user frees some
#: space. The latter is repairable, the former is not.
#:
#: **The estimate is only the gate's input, not a promise.** As soon as the
#: source states the ``Content-Length``, the check is made again with that
#: number before a single byte has been written (:func:`_download`).
DEMO_SIZE_ESTIMATE_BYTES = 256 * 1024 * 1024

#: The disk space reserve in bytes: what must not be spent on demos.
#:
#: **Not a courtesy but the boundary of usability.** Windows needs paging space
#: and the sync client needs a synchronisation buffer; a system disk written
#: full is not "a little tight" but a machine you cannot work on. Two gigabytes
#: is about ten demos at the measured size, that is, it survives the estimate
#: being wrong a few times in a row.
DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024

#: The smallest size a compressed CS2 demo can plausibly be.
#:
#: **A guard against rubbish, not a size limit.** An HTML error page with a 200
#: status and an empty response are both "successful" downloads that would be
#: written out as a demo -- and because idempotence looks only at whether the
#: file exists, the rubbish would then be skipped **for ever**. The smallest
#: compressed demo in the archive is over 140 MB, so a megabyte is three orders
#: of magnitude below it: it cannot reject a real demo, but it stops every
#: error page.
MIN_PLAUSIBLE_DEMO_BYTES = 1024 * 1024

#: The unit table for :func:`size_fi`.
#:
#: ``Pt`` is included because it was in ``cli._human_size``'s table: merging
#: two formatters must not lose the range either of them covered. No archive is
#: a petabyte in size, but a dropped unit would mean that the same number is
#: formatted differently from before -- and that difference is exactly what was
#: being fixed here.
_SIZE_UNITS = ("kt", "Mt", "Gt", "Tt", "Pt")


def size_fi(num_bytes: int) -> str:
    """A byte count readable in Finnish (decimal comma).

    **The project's only byte-count formatter.** ``cli`` had its own
    ``_human_size`` with its own unit table, so the same number could be
    printed two different ways depending on which command printed it. The
    guard: ``tests/test_cli.py::test_only_one_byte_formatter_exists``.

    >>> size_fi(1536)
    '1,5 kt'
    """
    if num_bytes < 1024:
        return f"{num_bytes} tavua"
    value = float(num_bytes)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        value /= 1024
        if value < 1024:
            break
    return f"{value:.1f} {unit}".replace(".", ",")


# -- The plan ----------------------------------------------------------------


@dataclass(frozen=True)
class FetchPlan:
    """What one run intends to download -- **before it downloads anything**.

    The plan is an output of its own and not a by-product of the run, because
    the user is asked for permission: a question that does not say how many
    files and how much disk space is not a question but a formality. The same
    plan is also the input to the disk check, so the number on the screen and
    the number in the check come from the same source.

    Attributes:
        team_key: The team whose selection file was read.
        pending: The MapDemos to download, in the selection file's order.
        present: MapDemos belonging to the sample that are already in the
            archive (the demo **and** its metadata file). They do not download
            again, but they belong in the count -- otherwise "2 / 12" would
            make the sample look as though it had shrunk.
        estimated_bytes: The estimated total size of what is to be downloaded.
    """

    team_key: str
    pending: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    estimated_bytes: int = 0

    @property
    def selected(self) -> int:
        """How many MapDemos belong to the sample altogether."""
        return len(self.pending) + len(self.present)


def plan(
    archive: ArchivePaths,
    team_key: str,
    *,
    size_estimate: int = DEMO_SIZE_ESTIMATE_BYTES,
) -> FetchPlan:
    """Read the selection file and decide which demos are missing from the archive.

    Rows where ``roster_ok`` is true are taken: they are the sample, and
    downloading a rejected map would spend quota and disk space on material no
    report reads.

    **The same id cannot end up on the list twice.** The same guard and the
    same reason as in :func:`plan_division`: a duplicate would mean fetching
    the same demo twice and a doubled number in both the count and the size
    estimate -- that is, a confirmation question that asks the wrong thing.
    The guard is here and not borrowed from the invariant of whoever wrote the
    selection file: the correctness of this function's result must not depend
    on another function's invariant that it does not enforce itself. The file
    in the archive can also be edited by hand. The price is one set, and the
    order stays the selection file's order.

    Args:
        archive: The archive's paths.
        team_key: The canonical team id.
        size_estimate: The estimated size of one demo in bytes.

    Returns:
        :class:`FetchPlan`.

    Raises:
        ~pappascout.errors.PappascoutError: If the selection file is missing or
            its shape is unknown. The message tells the user to run ``select``.
    """
    document = read_selection(archive, team_key)
    rows = document.get("selections")
    if not isinstance(rows, list):
        raise PappascoutError(
            f"The selection file for team {team_key} has no selections list.\n"
            f'Run again: uv run pappascout select --team "{team_key}"'
        )

    pending: list[str] = []
    present: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or not row.get("roster_ok"):
            continue
        # **The name is ``unit`` and not ``map_demo_id``.** At module level
        # there is a function of the same name
        # (:func:`~pappascout.domain.selection.map_demo_id`) that
        # :func:`plan_division` calls; a local variable shadowed it inside this
        # function, and the next line that needed the function would have
        # crashed with "str is not callable" in the middle of a run.
        unit = row.get("map_demo_id")
        if not isinstance(unit, str) or not unit:
            continue
        if unit in seen:
            continue
        seen.add(unit)
        if in_archive(archive, unit):
            present.append(unit)
        else:
            pending.append(unit)

    return FetchPlan(
        team_key=team_key,
        pending=tuple(pending),
        present=tuple(present),
        estimated_bytes=len(pending) * int(size_estimate),
    )


# -- The division's plan (Story 3.5) -----------------------------------------


@dataclass(frozen=True)
class NoVetoMatch:
    """A played match whose map list is not in the index.

    **A type of its own and not a bare id**, because this row exists only to
    state a reason. Story 3.3's review found the same gap in selection: there
    a played match without veto data vanished into a counter whose heading
    gave the reason as "not played" -- that is, the output claimed something
    about the match that was not true. A list of ids would repeat the fault,
    because the reason would have to be invented again where it is printed.

    Attributes:
        match_id: The match's id.
        reason: Why this match yields no ids at all. **Required, with no
            default.** The type exists only to state a reason, so a default
            would permit a row without one -- that is, exactly the state the
            class was written against. A rule is better than a defence against
            an empty reason where it is printed.
        finished_at: When the match ended, or ``None``. Included because a
            match that has just ended is a different case from one three weeks
            old: the first is only waiting for the next ``discover``, and the
            second means FACEIT never gave the veto at all -- and then running
            ``discover`` again produces nothing. The advice branches on this
            field (``cli._collect_no_veto``).
    """

    match_id: str
    reason: str
    finished_at: str | None = None


@dataclass(frozen=True)
class CollectPlan:
    """What ``collect`` intends to download -- **before it downloads anything**.

    :class:`FetchPlan`'s sibling and not a variant of it. Both describe the
    same set of download units, but they answer a different question and
    therefore carry different fields: ``FetchPlan`` states one team's sample
    (``team_key``, the roster threshold), ``CollectPlan`` the whole division's
    match index (the index's age, matches without veto data). A shared class
    would have to leave half its fields empty in each use, and an empty field
    is an invitation to read it wrongly.

    Three buckets and not two. ``pending`` and ``present`` are the same as in
    ``fetch``, but **a played match without a map list produces neither** --
    and if it has no place of its own, it disappears. A lost match is exactly
    the fault this command exists for.

    Attributes:
        league_ids: The ``[league].championship_ids`` whose matches qualified.
            **Read in the output**, not merely filled in: when the plan is
            empty, these ids are precisely what the user has to compare against
            the index -- an empty result usually means the settings name a
            different division from the index.
        pending: The MapDemos to download, in the match index's order.
        present: The division's MapDemos that are already on disk (the demo
            **and** its metadata file, from any of the three locations).
        no_veto: Played matches that yield no ids, with their reasons.
        matches_played: How many of the division's matches have been played.
        estimated_bytes: The estimated total size of what is to be downloaded.
        index_generated_at: The match index's ``generated_at`` as it stands.
            **A field of the plan and not a decoration of the output:** the
            index is the source of ``collect``'s whole set of units, so an
            index a week old means a week of matches this run does not see.
        best_of_unknown: Matches whose length is not known -- as ids and not as
            a flag. An observation whose justification is "an observation and
            not an assumption" has to be **traceable**: a bare true/false would
            say that some match is unknown but not which, and the user could
            not check the claim against the index. Both a missing ``best_of``
            and an invalid one (``< 1``) end up here: neither states the
            match's length, and presenting an invalid one as known would be a
            false claim. For these the maps are read from ``map_picks``.
    """

    league_ids: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    no_veto: tuple[NoVetoMatch, ...] = ()
    matches_played: int = 0
    estimated_bytes: int = 0
    index_generated_at: str | None = None
    best_of_unknown: tuple[str, ...] = ()

    @property
    def selected(self) -> int:
        """How many MapDemos are known from the division altogether."""
        return len(self.pending) + len(self.present)


def plan_division(
    archive: ArchivePaths,
    league: LeagueSettings,
    *,
    size_estimate: int = DEMO_SIZE_ESTIMATE_BYTES,
) -> CollectPlan:
    """Read the match index and decide which division demos are off disk.

    :func:`plan`'s sibling: the same download, another way of choosing the
    units. The units come from the **match index and not from the selection
    files**, because the point of collecting is to save what does not yet
    belong to anyone's sample either -- the roster threshold is ``select``'s
    business, and FACEIT deletes a demo in about 30 days regardless of whether
    the map qualified for anybody's report.

    Three rules separate this from ``select``'s similar-looking loop
    (:func:`~pappascout.stages.select._candidates`):

    **An unplayed match yields no row and no port call.** The demo does not
    exist, so asking for it would spend quota on a certain ``no_demo``.

    **A played match without a map list gets a row of its own.** That is not
    zero maps but missing information, and skipping it silently would be the
    claim "not played".

    **An uncertain map is attempted, not skipped.** ``select`` does the
    opposite, and rightly: an unplayed map reaching the sample would falsify
    the statistics. Here the asymmetry turns the other way -- a pointless
    attempt costs one call and an expected ``no_demo``, but skipping costs a
    demo that in a month's time is nowhere to be had. So ``best_of`` **does not
    limit** the list (the third map of a BO3 that ended 2-0 is attempted), and
    :func:`~pappascout.domain.selection.guaranteed_maps` is not called here at
    all.

    A missing ``best_of`` is therefore a note and not an assumption. Measured
    2026-09-06: the field is in the code (``discover._match_row``) but in the
    index written into the archive on 4 September it is absent from all 66
    rows. This function does not inherit ``select``'s silent reading in which
    an unknown length counts as "certainly played". The same applies to an
    **invalid** value: ``0`` or a negative number is not a short match but a
    broken field, and presenting it as a known length would be a false claim.
    The boundary is the same as in
    :func:`~pappascout.domain.selection.guaranteed_maps` (``< 1``).

    **The same id cannot end up on the list twice.** A duplicate would mean
    fetching the same demo twice and a doubled number in both the count and
    the size estimate -- that is, a confirmation question that asks the wrong
    thing. The guard is here and not borrowed from
    :func:`~pappascout.stages.discover.matches_from_index`'s own duplicate
    check: the correctness of this function's result must not depend on
    another function's invariant that it does not enforce itself. The price is
    one set, and the order stays the index's order.

    Args:
        archive: The archive's paths.
        league: The ``[league]`` section; ``championship_ids`` is read from it.
        size_estimate: The estimated size of one demo in bytes.

    Returns:
        :class:`CollectPlan`.

    Raises:
        ~pappascout.errors.PappascoutError: If the match index is missing, is
            not readable or has an unknown shape. The message tells the user to
            run ``discover``. **A broken match row stops the run**
            (:func:`~pappascout.stages.discover.matches_from_index`) instead of
            vanishing into a counter -- a skipped match would be exactly the
            silently lost demo this command was written against.
    """
    # The import is inside the function for the same reason as in
    # :func:`resolve_team_key`: ``discover``'s polars dependency must not load
    # when all that is needed from this module is :func:`run`.
    from pappascout.stages.discover import matches_from_index, read_matches_index

    document = read_matches_index(archive)
    matches = matches_from_index(document)
    league_ids = tuple(league.championship_ids)
    wanted = frozenset(league_ids)

    pending: list[str] = []
    present: list[str] = []
    no_veto: list[NoVetoMatch] = []
    seen: set[str] = set()
    played = 0
    best_of_unknown: list[str] = []

    for match in matches:
        # A match from the wrong competition is not this division's business.
        # The comparison is made on the id, because the index carries no name.
        if match.competition_id not in wanted:
            continue
        if not match.played:
            continue
        played += 1
        if not match.map_picks:
            no_veto.append(
                NoVetoMatch(
                    match_id=match.match_id,
                    finished_at=match.finished_at,
                    reason=(
                        "The match has been played, but the match index does "
                        "not carry its map list, so the maps' ids cannot be "
                        "built and their demos cannot be fetched. This does "
                        "not mean the match went unplayed."
                    ),
                )
            )
            continue
        if match.best_of is None or match.best_of < 1:
            best_of_unknown.append(match.match_id)
        for index in range(len(match.map_picks)):
            unit = map_demo_id(match.match_id, index)
            if unit in seen:
                continue
            seen.add(unit)
            if in_archive(archive, unit):
                present.append(unit)
            else:
                pending.append(unit)

    return CollectPlan(
        league_ids=league_ids,
        pending=tuple(pending),
        present=tuple(present),
        no_veto=tuple(no_veto),
        matches_played=played,
        estimated_bytes=len(pending) * int(size_estimate),
        index_generated_at=_optional_text(document.get("generated_at")),
        best_of_unknown=tuple(best_of_unknown),
    )


def _optional_text(value: Any) -> str | None:
    """A string to display, or ``None`` when there is nothing to display.

    The index's ``generated_at`` is **shown to the user as it stands**, so this
    is the place where the field is checked before it reaches the screen.
    Three cases produce ``None``:

    * the key is missing (``document.get`` returned ``None``),
    * the value is not a string -- a number or a list would be printed in the
      timestamp's place looking like a timestamp,
    * the value is empty or nothing but whitespace. A space is truthy, so
      without ``strip`` it would pass the check and leave a blank spot on the
      line -- which would look like an empty timestamp rather than an unknown
      one.

    Saying ``None`` out loud is the caller's business, not this function's.
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def resolve_team_key(archive: ArchivePaths, team: str) -> str:
    """Resolve ``--team`` into the canonical id using the teams index.

    The same reader as in ``discover`` and ``select``
    (:func:`~pappascout.stages.discover.resolve_team`), because an ambiguous
    name has to produce the same listing whichever command the user ran. The
    import is inside the function so that ``discover``'s polars dependency does
    not load when all that is needed from this module is :func:`run`.

    Raises:
        ~pappascout.errors.PappascoutError: If the index is missing or the name
            does not match exactly one team.
    """
    from pappascout.stages.discover import (
        read_teams_index,
        resolve_team,
        teams_from_index,
    )

    teams = teams_from_index(read_teams_index(archive))
    return resolve_team(teams, team).team_key


def in_archive(archive: ArchivePaths, map_demo_id: str) -> bool:
    """Is the demo in the archive **whole**, that is, the file and its metadata?

    Both, because either one alone is an unfinished state and not a finished
    result: a demo without metadata is an interrupted run (there is no digest),
    metadata without a demo is a file deleted by hand. In both cases the right
    continuation is to download again, and that is exactly why neither may look
    skippable.
    """
    return (
        archive.find_demo(map_demo_id) is not None
        and archive.find_demo_meta(map_demo_id) is not None
    )


def free_space(archive: ArchivePaths) -> int | None:
    """Free disk space in bytes **on the disk the demos are written to**.

    The target is :meth:`~pappascout.archive.paths.ArchivePaths.demos_dir` and
    not the archive root: the local demo directory can be on a different drive
    from an archive kept in a synchronised folder, and the wrong drive's free
    space is the wrong answer -- it would either block a download that fits or
    let through one that does not. On this machine the drives happen to be the
    same, and that must not be assumed in the code.

    The question is put to the **nearest directory that exists**: the target
    directory comes into being only with the first download, and the free space
    of a path that does not exist cannot be asked for. ``None`` means "could not
    be determined" -- and that is a different thing from zero: an unknown
    amount must not block a download, because then an unusual file system would
    stop the whole tool.
    """
    path = archive.demos_dir()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:  # pragma: no cover - depends on the file system
        return None


# -- Downloading one demo ----------------------------------------------------


def run(
    archive: ArchivePaths,
    map_demo_id: str,
    *,
    source: DemoSource,
    size_estimate: int = DEMO_SIZE_ESTIMATE_BYTES,
    reserve_bytes: int = DISK_RESERVE_BYTES,
    min_bytes: int = MIN_PLAUSIBLE_DEMO_BYTES,
    disk_free: Callable[[ArchivePaths], int | None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> StageResult:
    """Download one MapDemo to disk, or say why you did not.

    Args:
        archive: The archive's paths.
        map_demo_id: The unit's id, ``{match_id}-{map_index}``.
        source: The :class:`~pappascout.adapters.protocols.DemoSource` port. In
            production :func:`default_source`, in tests a fake.
        size_estimate: The estimated size of one demo for the disk check. Used
            only until the source states the real size.
        reserve_bytes: The reserve that is not spent on demos.
        min_bytes: The smallest size a downloaded file is allowed to be.
        disk_free: The free-space reader, or ``None`` = :func:`free_space`.
            **``None`` and not a function bound as the default**: a default
            would bind at definition time, so a ``free_space`` replaced with
            ``monkeypatch`` would not reach here at all and the test would pass
            only because the machine happens to have space.
        now: The clock for the ``fetched_at`` field.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``status`` is

        ``ok``
            The demo is on disk. ``skipped`` says whether it was downloaded
            now.
        ``no_demo``
            The demo does not exist: the match was not played, the map was not
            played, or FACEIT has already deleted the recording. ``reason``
            tells these apart.
        ``download_failed``
            The demo probably exists but did not arrive -- the network, a
            limit, a short response, rubbish as the response, disk space or a
            write error. Running again makes sense.

    Raises:
        ~pappascout.errors.PappascoutError: Only if the id is not usable as
            part of a path. Everything else is a ``status``, not an exception
            (AD-9) -- **including ``OSError``**.
    """
    started = time.perf_counter()
    safe_component(map_demo_id, "map_demo_id")
    clock = now if now is not None else (lambda: datetime.now(UTC))
    free_bytes = disk_free if disk_free is not None else free_space

    existing = archive.find_demo(map_demo_id)
    existing_meta = archive.find_demo_meta(map_demo_id)

    if existing is not None and existing_meta is not None:
        return _result(
            map_demo_id,
            status="ok",
            skipped=True,
            outputs=_archive_outputs(archive, existing, existing_meta),
            reason=(
                f"Demo {existing.name} is already in directory "
                f"{existing.parent} together with its metadata, so it was not "
                "downloaded again."
            ),
            started=started,
            stats=_location_stats(existing, existing_meta, downloaded_bytes=0),
        )

    # **The write goes where the demo already is.** If a partial demo is in the
    # archive and new downloads went to the local directory, the default target
    # would leave the archive's 190 MB in place and write a second copy beside
    # it -- that is, it would double exactly what the local directory avoids.
    demo_path = existing if existing is not None else archive.demo(map_demo_id)
    meta_path = demo_path.parent / f"{map_demo_id}.meta.json"

    # Why it downloads even though something is on disk already. The reason
    # travels all the way into the result: "downloaded again" without a
    # justification would look like wasted work.
    redo: str | None = None
    orphan: Path | None = None
    if existing is not None:
        redo = (
            f"Demo {existing.name} was in directory {existing.parent} but its "
            "metadata file was missing (the previous run broke off between "
            "the two writes), so it was downloaded again into the same place."
        )
    elif existing_meta is not None:
        # **An orphaned metadata file is removed, not merely overwritten.** It
        # can be in a different directory from the new demo (the archive versus
        # the local one), in which case writing alone would leave behind a
        # second metadata file claiming a digest for a file that is not there
        # -- and ``parse`` reads the digest from the first metadata file it
        # finds.
        orphan = existing_meta if existing_meta != meta_path else None
        redo = (
            "The metadata file was on disk but the demo was missing, so the "
            "demo was downloaded."
        )
        if orphan is None:
            redo += " The old metadata file was overwritten."
        # **The orphan's removal is reported only once it has been attempted**,
        # not here. The text was built before the ``unlink`` and claimed the
        # removal was done, even though ``unlink`` said nothing about failing
        # -- and a file lock held by the sync client is ordinary on Windows.
        # The rest is appended in :func:`_orphan_note` after the download.

    blocked = _preflight(
        archive,
        demo_path,
        map_demo_id,
        size_estimate=size_estimate,
        reserve_bytes=reserve_bytes,
        disk_free=free_bytes,
    )
    if blocked is not None:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=str(blocked),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(blocked),
                "failure_key": _failure_key(blocked),
            },
        )

    def guard(announced: int) -> None:
        """Disk space again with the **real** size, before the write.

        The estimate is enough of a gate only until the source states the size.
        After that, using the estimate would be deliberate imprecision: the
        number is known, and the write should not start if it does not fit.
        """
        free = free_bytes(archive)
        need = announced + reserve_bytes
        if free is not None and free < need:
            raise _space_error(archive, map_demo_id, free, need, announced)

    try:
        digest, size, verified = _download(
            source, map_demo_id, demo_path, min_bytes=min_bytes, guard=guard
        )
    except DownloadsAccessDenied:
        # **The only fault that rises through this stage as an exception.**
        # Everything else is a unit's status (AD-9), because everything else
        # concerns one demo. A missing Downloads scope concerns the credential:
        # every unit would fail identically, and none could succeed. Recording
        # it as a unit's status would produce a sample-sized listing of the
        # same error and the same number of doomed calls.
        raise
    except DemoUnavailable as exc:
        # An absent demo is a fact and not a disturbance: it is not retried and
        # the series is not interrupted because of it.
        return _result(
            map_demo_id,
            status="no_demo",
            skipped=False,
            outputs=(),
            reason=str(exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(exc),
                "failure_key": _failure_key(exc),
            },
        )
    except PappascoutError as exc:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=str(exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(exc),
                "failure_key": _failure_key(exc),
            },
        )
    except OSError as exc:
        # **The disk is as much a property of the unit as the network is.** A
        # full disk, a file lock held by the sync client and a dropped network
        # drive all raise ``OSError``; without this branch they would escape
        # past the stage and bring the whole run down as a "programming error"
        # -- that is, break the constraint "one demo's failure does not
        # interrupt the run".
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=_os_error_message(demo_path, map_demo_id, exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": _DISK_NEXT_STEP,
                "failure_key": _failure_key(exc),
            },
        )

    if orphan is not None:
        redo = " ".join(filter(None, (redo, _orphan_note(orphan))))

    # **Only here.** The demo is in place and has been read to the end; the
    # metadata file may come into being only now, because it is a claim about
    # this exact file.
    try:
        atomic_write_json(
            meta_path,
            {
                "map_demo_id": map_demo_id,
                "sha256": digest,
                "size": size,
                "source": DEMO_SOURCE,
                "fetched_at": clock().isoformat(),
                # **Says whether the length was checked against the source's
                # own number.** ``false`` does not mean a broken file but that
                # wholeness could not be established during the download. The
                # field is always present, so that nothing has to be inferred
                # from its absence.
                "length_verified": verified,
            },
        )
    except OSError as exc:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=_os_error_message(meta_path, map_demo_id, exc),
            started=started,
            stats={
                "downloaded_bytes": size,
                "next_step": _DISK_NEXT_STEP,
                "failure_key": _failure_key(exc),
            },
        )

    note = redo
    if not verified:
        # Not an error, but not something to keep quiet about either: this is
        # the case where a broken download is indistinguishable from a whole
        # one.
        note = " ".join(filter(None, (note, _unverified_note(map_demo_id))))

    return _result(
        map_demo_id,
        status="ok",
        skipped=False,
        outputs=_archive_outputs(archive, demo_path, meta_path),
        reason=note,
        started=started,
        stats=_location_stats(demo_path, meta_path, downloaded_bytes=size)
        | {"sha256": digest, "length_verified": verified},
    )


def run_many(
    archive: ArchivePaths,
    map_demo_ids: Iterable[str],
    *,
    source: DemoSource,
    **kwargs: Any,
) -> tuple[StageResult, ...]:
    """Run :func:`run` for every id. **No fault interrupts it.**

    The loop is not a convenience but a rule: if one demo's 404 ended the run,
    one deleted recording would leave eleven others unfetched -- and they are
    exactly why the command is run.

    There are two types to catch, not one.
    :class:`~pappascout.errors.PappascoutError` is the tool's own error,
    ``OSError`` the disk's -- and the disk is as much a property of the unit as
    the network is: a full disk, a file lock held by the sync client and a
    dropped network drive are all situations in which **the next demo may very
    well succeed**. Without ``OSError`` the constraint "one demo's failure does
    not interrupt the run" would hold only halfway, and the user would see
    "programming error" on the screen for a full disk.

    A programming error (``TypeError`` and the rest) still rises through: it is
    not a property of the unit but a fault in the code, and it must not be
    hidden among eleven successful downloads.
    """
    results: list[StageResult] = []
    units = list(map_demo_ids)
    for index, map_demo_id in enumerate(units):
        started = time.perf_counter()
        try:
            results.append(run(archive, map_demo_id, source=source, **kwargs))
        except DownloadsAccessDenied as exc:
            # **Stop at the first authorisation failure.** See the class's own
            # documentation: this is not an exception to the rule "one demo's
            # failure does not interrupt the run" but a different thing -- it
            # is not one demo failing but no demo being able to succeed.
            raise DownloadsAccessDenied(
                f"{exc}\n\n{_progress_note(results, units, index)}"
            ) from None
        except PappascoutError as exc:
            results.append(
                _result(
                    map_demo_id,
                    status="download_failed",
                    skipped=False,
                    outputs=(),
                    reason=str(exc),
                    started=started,
                    stats={
                        "downloaded_bytes": 0,
                        "next_step": next_step(exc),
                        "failure_key": _failure_key(exc),
                    },
                )
            )
        except OSError as exc:
            results.append(
                _result(
                    map_demo_id,
                    status="download_failed",
                    skipped=False,
                    outputs=(),
                    reason=_os_error_message(
                        archive.demos_dir(), map_demo_id, exc
                    ),
                    started=started,
                    stats={
                        "downloaded_bytes": 0,
                        "next_step": _DISK_NEXT_STEP,
                        "failure_key": _failure_key(exc),
                    },
                )
            )
        repeated = _repeated_failure(results)
        if repeated is not None and index + 1 < len(units):
            results.extend(_not_attempted(units[index + 1 :], repeated))
            break
    return tuple(results)


def default_source(settings: Settings, archive: ArchivePaths) -> DemoSource:
    """The production FACEIT implementation of the demo port.

    The import is inside the function for the same reason as in
    ``stages.discover.default_source``: the stage itself knows only the port,
    and importing this module must not load ``requests``.

    **The Downloads token is read here**, before the first download. A missing
    token stops the run with instructions -- and not in the middle of the
    series, when half the demos have already been fetched.

    Raises:
        ~pappascout.errors.SettingsError: If the key or the token is missing.
    """
    from pappascout.adapters.faceit import FaceitClient, FaceitDemoSource

    client = FaceitClient.from_settings(settings, archive.raw_faceit())
    return FaceitDemoSource.from_settings(settings, client)


# -- Internals ---------------------------------------------------------------


def _download(
    source: DemoSource,
    map_demo_id: str,
    demo_path: Path,
    *,
    min_bytes: int,
    guard: Callable[[int], None],
) -> tuple[str, int, bool]:
    """Write the stream into a temporary file and compute the digest at the same time.

    Three nested ``with`` statements, and their **order is part of the rule**:

    1. The stream is opened first. If the demo does not exist, the exception
       rises before a single file has been created -- a 404 leaves no rubbish
       on disk.
    2. The atomic path second. Its ``finally`` cleans up the temporary file,
       and ``os.replace`` happens only if the block ends without an exception.
    3. The file only third, and it is closed (``fsync``) before the checks are
       made.

    **Every check is inside the ``atomic_path`` block.** Outside it they would
    notice the fault only once the file had already been moved into place, and
    the repair would be a deletion -- that is, exactly the state the atomic
    write exists to prevent. It would be especially bad here, because
    idempotence looks only at whether the file exists: rubbish moved into place
    would be skipped **on every run, for ever**.

    There are three checks, and they answer three different questions:

    ``Is the content zstd?``
        The first four bytes. An HTML error page with a 200 status is a
        successful HTTP response and a perfectly valid file -- nothing but its
        content tells it apart.
    ``Is the size plausible?``
        ``min_bytes``. A zero-byte response would pass the length check
        (``written == expected == 0``), and an empty file would then be skipped
        for ever.
    ``Does the length match the promise?``
        ``Content-Length``. The third return value says **whether** this check
        was made -- the source does not always state the length, and it must
        not be presented as checked.

    Returns:
        ``(sha256 as hex, bytes, length_was_checked)``.

    Raises:
        ~pappascout.errors.DemoUnavailable: The demo does not exist.
        ~pappascout.errors.ApiError: The download did not succeed, came up
            short, or produced something other than a demo.
        OSError: The disk is full or the file cannot be written.
    """
    digest = hashlib.sha256()
    written = 0
    head = b""

    with source.get_demo(map_demo_id) as stream:
        expected = stream.content_length
        if expected is not None:
            # The real size is known now: check the space again before a single
            # byte has been written.
            guard(expected)

        with atomic_path(demo_path) as tmp:
            with open(tmp, "wb") as handle:
                for chunk in stream.chunks:
                    if len(head) < len(ZSTD_MAGIC):
                        head += bytes(chunk[: len(ZSTD_MAGIC) - len(head)])
                    # **The same chunk, the same time.** Computing the digest
                    # afterwards would mean reading 200 MB again.
                    digest.update(chunk)
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            if expected is not None and written != expected:
                raise ApiError(
                    f"The download of demo {map_demo_id} came up short: the "
                    f"source promised {expected} bytes ({size_fi(expected)}) "
                    f"but {written} bytes ({size_fi(written)}) arrived.\n"
                    "The unfinished file was not left on disk.",
                    advice=_RETRY_NEXT_STEP,
                )
            if written < min_bytes:
                raise ApiError(
                    f"The download of demo {map_demo_id} produced only "
                    f"{written} bytes ({size_fi(written)}), which is far too "
                    f"little for a CS2 demo (at least {size_fi(min_bytes)}).\n"
                    "The source most likely answered with an error page or an "
                    "empty body. The file was not left on disk.",
                    advice=_RETRY_NEXT_STEP,
                )
            if head[: len(ZSTD_MAGIC)] != ZSTD_MAGIC:
                raise ApiError(
                    f"The download of demo {map_demo_id} is not a "
                    f"zstd-compressed file: its first bytes are "
                    f"{head[: len(ZSTD_MAGIC)]!r}, not {ZSTD_MAGIC!r}.\n"
                    "The source most likely answered with an error page or a "
                    "sign-in page even though the status code was 200. The "
                    "file was not left on disk.",
                    advice=_RETRY_NEXT_STEP,
                )

    return digest.hexdigest(), written, expected is not None


def _preflight(
    archive: ArchivePaths,
    demo_path: Path,
    map_demo_id: str,
    *,
    size_estimate: int,
    reserve_bytes: int,
    disk_free: Callable[[ArchivePaths], int | None],
) -> PappascoutError | None:
    """Everything about the target directory, before the first call.

    Two checks in one place, because they share a timing requirement: both have
    to happen **before** a download is started. A connection opened towards a
    directory that cannot be written spends Downloads quota and fails only at
    the first byte.

    Returns:
        An error carrying both the explanation and the advice, or ``None`` when
        all is well. **An error and not a string**: bare text would force the
        caller to invent the advice itself, and that inventing is exactly what
        ``PappascoutError.advice`` exists to prevent.
    """
    unwritable = _writable_problem(demo_path.parent)
    if unwritable is not None:
        return unwritable
    free = disk_free(archive)
    need = int(size_estimate) + int(reserve_bytes)
    if free is None or free >= need:
        return None
    return _space_error(archive, map_demo_id, free, need, size_estimate)


def _writable_problem(directory: Path) -> PappascoutError | None:
    """Can the directory be created and written to? ``None`` = yes.

    **It tries rather than reasons.** Permissions, a network drive that is not
    connected, write protection and a file where the directory should be are
    four different causes, none of which shows up in ``disk_usage`` -- and each
    of them would bring the run down only in the middle of a write, by which
    point Downloads quota has already been spent. ``os.access`` will not do on
    Windows, because it does not see ACLs or a write-protected drive; the only
    reliable way is to write.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SettingsError(
            f"The demo directory {directory} could not be created: {exc}\n"
            "The download was not started.",
            advice=(
                "Check the path: does it point at a drive that exists, and "
                "is there a file of the same name on the path? It comes from "
                "the PAPPASCOUT_DEMOS_ROOT environment variable when that is "
                "set, otherwise from the archive root."
            ),
        )
    probe = directory / f".pappascout-write-probe{temp_suffix()}"
    try:
        probe.write_bytes(b"1")
    except OSError as exc:
        return SettingsError(
            f"The demo directory {directory} cannot be written to: {exc}\n"
            "The download was not started, so that no Downloads quota would "
            "be spent for nothing.",
            advice=(
                "Check the directory's write permissions -- and the "
                "PAPPASCOUT_DEMOS_ROOT environment variable if it is set -- "
                "then run the command again."
            ),
        )
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - depends on the disk
            pass
    return None


def _space_error(
    archive: ArchivePaths,
    map_demo_id: str,
    free: int,
    need: int,
    demo_bytes: int,
) -> ApiError:
    """Running out of disk space, explained. Same text before and during.

    ``ApiError`` and not a type of its own, because the stage's decision is the
    same as for a network fault: ``download_failed``, that is, a situation that
    may be put right by another run -- here once the user has freed some space.
    """
    return ApiError(
        f"Not enough disk space to download demo {map_demo_id}, so the "
        "download was not started.\n"
        f"Free space is {size_fi(free)} and {size_fi(need)} is needed "
        f"(the demo {size_fi(demo_bytes)} + the reserve "
        f"{size_fi(need - demo_bytes)}).\n"
        "Do one of these and run the command again:\n"
        f"  1. Delete already parsed demos from the directory "
        f"{archive.demos_dir()} -- the parsed tables stay, and the report does "
        "not need the demo file again.\n"
        "  2. Free disk space elsewhere on the machine.\n"
        "  3. Point the demos at another disk with the "
        "PAPPASCOUT_DEMOS_ROOT environment variable.",
        advice=(
            f"Free at least {size_fi(need - free)} of disk space and run the "
            "command again."
        ),
    )


#: The advice for a fault that **really** is put right by another attempt.
#:
#: This is the sentence that used to come from the heading for every failure.
#: Now it is given explicitly, and only when it is true.
_RETRY_NEXT_STEP = "Run the command again."

#: The advice for disk and file-system faults.
_DISK_NEXT_STEP = (
    "Free some disk space or wait until the sync client releases the file "
    "lock, then run the command again."
)


#: How many consecutive units failing the same way end the series.
#:
#: **An observation of repetition, not an assumption about the cause.** Story
#: 3.4's live run on 2026-09-05 produced two identical 400s; with twelve demos
#: that would have been twelve pointless calls. The temptation would be to stop
#: the run at the first 400, but **that cannot be justified**: a 400 means
#: either a malformed id (all of them fail) or a malformed ``resource_url``
#: (only this one fails), and the response does not say which. C2's
#: justification ("none of them can succeed") therefore does not apply here.
#:
#: Repetition is a different thing from a cause, and it is **measurable**: when
#: three consecutive units fall over the same failure key, the fault is a
#: common one whatever it is. Three and not two, because two identical codes in
#: a row is entirely plausible coincidence (two deleted demos from the same
#: match), and the price of three calls is small next to ending the series on a
#: wrong justification.
IDENTICAL_FAILURE_LIMIT = 3


def _repeated_failure(results: Sequence[StageResult]) -> str | None:
    """Did the last :data:`IDENTICAL_FAILURE_LIMIT` fall over the same fault?

    Returns the fault's key or ``None``. It looks only at ``download_failed``
    states: ``no_demo`` is the expected outcome for an old match and not a sign
    that something is broken -- three deleted demos in a row is a normal
    observation, not a reason to stop.
    """
    if len(results) < IDENTICAL_FAILURE_LIMIT:
        return None
    last = results[-IDENTICAL_FAILURE_LIMIT:]
    if any(r.status != "download_failed" for r in last):
        return None
    keys = {str(r.stats.get("failure_key", "")) for r in last}
    if len(keys) != 1:
        return None
    key = keys.pop()
    return key or None


def _not_attempted(
    units: Sequence[str], failure_key: str
) -> list[StageResult]:
    """The result for units that were no longer attempted.

    **A row for each, no silent shortening.** The plan promised a certain
    number of units, and a listing shorter than the plan would leave the user
    guessing where the rest went. Every row also states its own next step, as
    every other failure does.
    """
    reason = (
        f"Not attempted: the previous {IDENTICAL_FAILURE_LIMIT} demos failed "
        f"over the same fault ({failure_key}), so the fault is a common one "
        "and not specific to a demo. The call was not made, so that Downloads "
        "quota would not certainly be spent for nothing."
    )
    step = (
        "Fix the fault listed above and run the command again -- this demo is "
        "still unfetched."
    )
    return [
        _result(
            unit,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=reason,
            started=time.perf_counter(),
            stats={
                "downloaded_bytes": 0,
                "next_step": step,
                "failure_key": failure_key,
                "not_attempted": True,
            },
        )
        for unit in units
    ]


def _failure_key(exc: BaseException) -> str:
    """The fault's **key for noticing repetition**, not its cause.

    Two consecutive units with the same key failed in the same way. That is an
    observation and not a conclusion about *why* -- and that is exactly why it
    is a good enough justification for ending the series: repetition is
    measurable, the cause is not always (see :data:`IDENTICAL_FAILURE_LIMIT`).

    The status code when there is one, otherwise the exception's type. For
    ``OSError`` also the ``errno``, because a full disk (28) and a permission
    (13) are a different fault and a different fix.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"http-{status}"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"errno-{exc.errno}"
    return f"type-{type(exc).__name__}"


def _progress_note(
    results: Sequence[StageResult], units: Sequence[str], index: int
) -> str:
    """What was managed before the run stopped.

    Without this line the user does not know whether anything reached the
    archive -- and an interrupted series would look the same as a series that
    never started.
    """
    done = sum(1 for r in results if r.status == "ok")
    remaining = len(units) - index
    return (
        f"The run stopped at the first authorisation failure: demos fetched: "
        f"{done}, demos left unfetched: {remaining}. The same fault would "
        "repeat on every one of them, so no attempt was made."
    )


def _os_error_message(path: Path, map_demo_id: str, exc: OSError) -> str:
    """A disk error **in the user's terms**, not as an errno number.

    The three most common causes are named, because ``[Errno 28] No space
    left`` does not say what to do -- and because this very error used to be
    shown as a "programming error", which is the wrong diagnosis.
    """
    return (
        f"Writing demo {map_demo_id} failed with a disk error "
        f"({type(exc).__name__}: {exc}).\n"
        f"The target was {path}.\n"
        "The most common causes: the disk filled up during the write, the sync "
        "client held the file locked, or the network drive dropped.\n"
        "The other demos were fetched all the same. Free some space or wait a "
        "moment and run the command again."
    )


def _unverified_note(map_demo_id: str) -> str:
    """What **could not** be established about the download -- said out loud.

    Without a ``Content-Length`` a broken stream looks exactly like a whole
    one: the file is in place, the start is in zstd form and the size is
    plausible. The stage cannot tell them apart, and **it has to say so** --
    otherwise unspoken uncertainty would look like certainty, and a truncated
    demo would be skipped for ever on the strength of idempotence.

    Why wholeness is not checked all the way: that would mean decompressing the
    whole stream with zstd, that is, about a gigabyte of work per demo.
    ``parse`` does it anyway, and a truncated file falls over there -- this note
    is what tells the user what to do then.
    """
    return (
        f"The source did not state the size of demo {map_demo_id} (no "
        "Content-Length header), so the download's wholeness could not be "
        "established from its length. The start is in zstd form and the size "
        "is plausible, but a truncated end would only show up in parsing. If "
        "parse fails on this demo, delete it and run fetch again."
    )


def _remove(path: Path) -> bool:
    """Delete a file; failing is **information** and not an exception.

    The same function and the same justification as
    ``stages.import_demo._remove``: a file lock held by the sync client and an
    open handle from the antivirus are both ordinary on Windows, and neither
    means the download failed -- the demo is on disk and its metadata file is
    beside it.

    **The return value must be read.** The deletion used to be
    ``except OSError: pass``, and the note reported a removal before it had
    even been attempted.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        # **No ``pragma: no cover``.** The branch is tested:
        # ``test_an_orphan_meta_that_cannot_be_removed_is_never_claimed_removed``
        # runs it with a monkeypatched ``Path.unlink``. A pragma on a covered
        # line would hide from coverage the very place for which the function
        # returns a boolean instead of staying silent.
        return False


def _orphan_note(orphan: Path) -> str:
    """Removing an orphaned metadata file -- **and failing to remove it**.

    The same rule and the same wording as
    ``stages.import_demo._orphan_note``: a metadata file left describing a file
    that does not exist is a claim about a digest -- and ``parse`` reads the
    digest from the **first metadata file it finds**. If the removal fails, the
    note is a ``WARNING`` rather than silence, because a false claim is more
    dangerous than missing information: the reader trusts it.
    """
    if _remove(orphan):
        return (
            f"The old metadata file in directory {orphan.parent} was removed, "
            "because it described a file that does not exist."
        )
    return (
        f"WARNING: the old metadata file {orphan} could not be removed (a file "
        "lock held by the sync client, for instance), and it describes a file "
        "that does not exist. Delete it by hand -- otherwise parse may read "
        "the digest from the wrong file."
    )


def _archive_outputs(
    archive: ArchivePaths, *paths: Path
) -> tuple[PurePosixPath, ...]:
    """The files written, in ``StageResult.outputs``'s form.

    **By contract ``outputs`` is a path relative to the inside of the
    archive**, and that is a deliberate constraint: an absolute path would
    break the archive on another machine. When the demos go into a local
    demo directory (``PAPPASCOUT_DEMOS_ROOT``), they are outside the archive
    and no such path exists.

    The answer is to **leave them out rather than lie**: a file outside must
    not appear in a list that promises to hold archive paths. The absolute
    paths travel instead in ``stats["demo_path"]`` and ``stats["meta_path"]``
    (see :func:`_location_stats`) -- they are **this run's information** and
    not state stored into the archive, and the command prints them for the
    user. The alternatives that were rejected: an absolute path in ``outputs``
    (it would break the contract that manifests and indexes read) and a ``..``
    path relative to the archive root (which
    :meth:`~pappascout.archive.paths.ArchivePaths.resolve` rejects -- and
    rightly).
    """
    inside: list[PurePosixPath] = []
    for path in paths:
        try:
            inside.append(archive.relative(path))
        except ValueError:
            # Outside the archive. See the docstring: no guess, no row.
            continue
    return tuple(inside)


def _location_stats(
    demo_path: Path, meta_path: Path, *, downloaded_bytes: int
) -> dict[str, Any]:
    """Where the demo and the metadata file are -- **absolute, in both modes**.

    Always present and not only in local mode: if the field appeared only when
    the demo is outside the archive, the output would have to infer the
    location from whether the field exists -- and that inference never has to
    be made when the value is always there.
    """
    return {
        "downloaded_bytes": downloaded_bytes,
        "demo_path": str(demo_path),
        "meta_path": str(meta_path),
        "demos_dir": str(demo_path.parent),
    }


#: The advice when nothing else knows better.
#:
#: **Not "run the command again".** That very default was the root cause of
#: both faults found in the live run: advice that arrives as a default is
#: advice nobody has considered for this fault. The right advice for an unknown
#: fault is to say that it is not known.
DEFAULT_NEXT_STEP = (
    "The cause is not one the tool knows. Read the message above and work out "
    "from it whether this is the network, the settings or the disk."
)


def next_step(exc: BaseException) -> str:
    """What the user has to do next because of this fault.

    **The advice comes from the error, not from the heading.** See
    :class:`~pappascout.errors.PappascoutError`. This function does not infer
    the advice from a status code or from the words of a message -- it reads it
    from the field that whoever raised the error filled in. Inferring would
    make the same guess this fix removes, and it would be far from the place
    where the cause is known.
    """
    advice = getattr(exc, "advice", None)
    if isinstance(advice, str) and advice.strip():
        return advice.strip()
    return DEFAULT_NEXT_STEP


def _result(
    map_demo_id: str,
    *,
    status: str,
    skipped: bool,
    outputs: Sequence[PurePosixPath],
    reason: str | None,
    started: float,
    stats: dict[str, Any],
) -> StageResult:
    """The stage's result, and **for a failure also the next step**.

    A guard and not formatting: without it a new failure path could produce a
    failure with no advice, and the output would have to invent one -- that is,
    it would return to exactly the default this structure removes.
    """
    if status != "ok" and not str(stats.get("next_step", "")).strip():
        raise AssertionError(
            f"The status of unit {map_demo_id} is {status!r} but there is no "
            "next step. A failure without advice would leave the user "
            "guessing -- add a next_step."
        )
    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status=status,  # type: ignore[arg-type]
        skipped=skipped,
        outputs=tuple(outputs),
        # **No manifest.** A manifest answers the question "is the result up to
        # date with its inputs", but this stage's input is FACEIT's recording,
        # which cannot be compared with anything without downloading it -- that
        # is, by doing the very work that skipping would save. Idempotence is
        # settled by whether the file exists, and that is cheaper and more
        # honest.
        manifest_path=None,
        reason=reason,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )
