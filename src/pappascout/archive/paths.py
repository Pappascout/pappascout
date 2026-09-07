"""The archive's directory layout (AD-7).

The tree is fixed in the spine's convention table::

    raw/faceit/                                  HTTP cache, may be emptied
    index/teams.json                             written only by discover
    index/matches.json                           written only by discover
    index/selections/<team_key>.json             written only by select
    index/next_opponent/<team_key>.json          written only by discover
    demos/<map_demo_id>.dem.zst  + .meta.json    written only by fetch / import
    parsed/<map_demo_id>/{ticks,events,rounds,lineups,deaths}.parquet + manifest
    classified/<team_key>/<map_demo_id>.parquet + .md + manifest
    aggregates/<team_key>/report.json
    reports/<team_key>/<YYYY-MM-DDTHHMM>-<team_slug>.md + same name .manifest.json
    import/                                      incoming folder
    logs/<host>/
    .lock

The module offers paths in two forms. Module-level functions return a
**relative** ``PurePosixPath``, which is the only form that may be stored in a
manifest or an index -- an absolute path would break the archive on the other
machine. :class:`ArchivePaths` prefixes the root and returns a ``Path``
object, which is what the actual I/O uses.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from pappascout.errors import PappascoutError

__all__ = [
    "ArchivePaths",
    "ARCHIVE_ROOT_ENV_VAR",
    "DEMOS_ROOT_ENV_VAR",
    "safe_component",
    "DEMO_SUFFIXES",
    "DEFAULT_DEMO_SUFFIX",
    "PARSED_TABLES",
    "LOCK_FILE",
    "raw_faceit_dir",
    "index_dir",
    "teams_index",
    "matches_index",
    "selection",
    "next_opponent",
    "demo",
    "demo_meta",
    "parsed_root",
    "parsed_dir",
    "parsed_table",
    "parsed_manifest",
    "classified",
    "classified_round_list",
    "classified_manifest",
    "report_json",
    "report_manifest",
    "reports_dir",
    "REPORT_TIMESTAMP_FORMAT",
    "MAX_REPORTS_PER_MINUTE",
    "report_name",
    "report_markdown",
    "render_manifest",
    "import_dir",
    "logs_dir",
]

#: FACEIT serves demos zstd-compressed (observed 2026-08-28); ``.dem.gz`` is
#: the fallback form for manually imported files.
DEFAULT_DEMO_SUFFIX = ".dem.zst"
DEMO_SUFFIXES: tuple[str, ...] = (".dem.zst", ".dem.gz", ".dem")

#: The tables the ``parse`` stage writes, one set per demo. The list is a
#: gatekeeper: a name that is not here cannot end up in an archive path.
PARSED_TABLES: tuple[str, ...] = (
    "rounds",
    "ticks",
    "events",
    "lineups",
    "deaths",
    "callouts",
    "match",
)

LOCK_FILE = PurePosixPath(".lock")

_MANIFEST_SUFFIX = ".manifest.json"

#: Environment variable that overrides the archive root per machine, without
#: having to edit the versioned settings.toml.
ARCHIVE_ROOT_ENV_VAR = "PAPPASCOUT_ARCHIVE_ROOT"

#: Environment variable that overrides the demo directory separately.
#:
#: **It exists because redirecting the archive would otherwise be half a
#: job.** ``PAPPASCOUT_ARCHIVE_ROOT`` exists precisely so that a run can be
#: pointed at a test archive without touching the production files -- but
#: ``demos_root`` is a path outside the archive, and it would not follow
#: along. A ``fetch`` run against a test archive would write and read the
#: production demo directory, silently and entirely by accident.
DEMOS_ROOT_ENV_VAR = "PAPPASCOUT_DEMOS_ROOT"

#: Characters allowed in a path component. The ids (team_key, map_demo_id,
#: host) come from FACEIT and from the user's settings, so they are not
#: interpolated into a path unchecked -- otherwise ".." would escape the
#: archive root.
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


def safe_component(value: str, kind: str) -> str:
    """Check that an id is usable as a path component as it stands.

    Args:
        value: The id to check.
        kind: Kind of id, for the error message, for example ``"team_key"``.

    Returns:
        The same value, unchanged.

    Raises:
        PappascoutError: If the value contains path separators, is empty, is
            ``.`` or ``..``, or is too long.
    """
    if not isinstance(value, str) or not _SAFE_COMPONENT.match(value):
        raise PappascoutError(
            f"The id {kind}={value!r} is not usable as an archive path "
            "component. Letters, digits and the characters _ . - are allowed "
            "(at most 120 characters)."
        )
    if value in {".", ".."}:
        raise PappascoutError(
            f"The id {kind}={value!r} is not usable as an archive path "
            "component: it would point outside the archive root."
        )
    return value


def raw_faceit_dir() -> PurePosixPath:
    """The HTTP cache. May be emptied at any time."""
    return PurePosixPath("raw/faceit")


def index_dir() -> PurePosixPath:
    """Root of the index tree. Every file in it can be derived again.

    The directory is created only when the first index is written; its
    absence is not an error but the information that ``discover`` has not
    been run yet.
    """
    return PurePosixPath("index")


def teams_index() -> PurePosixPath:
    """Team index: teams and their regular rosters. Written only by ``discover``.

    **The writer changed in Story 3.2**, and that followed from a
    measurement, not from taste. The spine gave the file to ``select``,
    because the regular roster was assumed to need a roster request of its
    own. Measured 2026-09-04: the roster is already on the match-list row
    (``roster`` and ``substitutes``, 132/132 team rows), so ``discover`` gets
    it from the same response it fetches anyway. If the file stayed with
    ``select``, the same response would be fetched twice -- or the regular
    roster would travel from stage to stage as a file neither of them owns.
    ``select`` reads this and writes its own result into :func:`selection`.
    """
    return index_dir() / "teams.json"


def matches_index() -> PurePosixPath:
    """Match index: the competition's matches as-is. Written only by ``discover``."""
    return index_dir() / "matches.json"


def selection(team_key: str) -> PurePosixPath:
    """The team's match selection and roster threshold. Written only by ``select``."""
    return index_dir() / "selections" / f"{safe_component(team_key, 'team_key')}.json"


def next_opponent(team_key: str) -> PurePosixPath:
    """The next opponent. Written only by ``discover``."""
    return index_dir() / "next_opponent" / f"{safe_component(team_key, 'team_key')}.json"


def demo(map_demo_id: str, suffix: str = DEFAULT_DEMO_SUFFIX) -> PurePosixPath:
    """The compressed demo file."""
    return PurePosixPath("demos") / f"{safe_component(map_demo_id, 'map_demo_id')}{suffix}"


def demo_meta(map_demo_id: str) -> PurePosixPath:
    """The demo's metadata: ``sha256``, ``size``, ``source``, ``fetched_at``.

    Manifests read the digest from this file rather than recomputing it from
    the demo -- hashing 233 MB on every run would be too slow.
    """
    return PurePosixPath("demos") / f"{safe_component(map_demo_id, 'map_demo_id')}.meta.json"


def parsed_root() -> PurePosixPath:
    """Root of the parsed demos.

    A function of its own, because stages walk the directory (``discover``
    looks for the lineup tables there). Without it the name ``"parsed"``
    would be hard-coded in a stage, and the single source for the archive
    tree would be in two places.
    """
    return PurePosixPath("parsed")


def parsed_dir(map_demo_id: str) -> PurePosixPath:
    return parsed_root() / safe_component(map_demo_id, "map_demo_id")


def parsed_table(map_demo_id: str, table: str) -> PurePosixPath:
    """One parsed table for one demo; see :data:`PARSED_TABLES`."""
    if table not in PARSED_TABLES:
        raise ValueError(
            f"Unknown parsed table {table!r}. "
            f"Allowed: {', '.join(PARSED_TABLES)}."
        )
    return parsed_dir(map_demo_id) / f"{table}.parquet"


def parsed_manifest(map_demo_id: str) -> PurePosixPath:
    return parsed_dir(map_demo_id) / f"parse{_MANIFEST_SUFFIX}"


def classified(team_key: str, map_demo_id: str) -> PurePosixPath:
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}.parquet"
    )


def classified_round_list(team_key: str, map_demo_id: str) -> PurePosixPath:
    """The round list as Markdown, the ``classify`` stage's second result.

    The same list ``--show`` prints, but as a file: Veeti reads it alongside
    the demo and checks the reasoning and the inputs behind every decision.
    The file lives in the ``classify`` stage's own directory, because
    ``reports/`` is the ``render`` stage's territory and a stage does not
    write into another stage's result area.
    """
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}.md"
    )


def classified_manifest(team_key: str, map_demo_id: str) -> PurePosixPath:
    return (
        PurePosixPath("classified")
        / safe_component(team_key, "team_key")
        / f"{safe_component(map_demo_id, 'map_demo_id')}{_MANIFEST_SUFFIX}"
    )


def report_json(team_key: str) -> PurePosixPath:
    """The ``aggregate`` stage's result: the ``Report`` model as JSON."""
    return PurePosixPath("aggregates") / safe_component(team_key, "team_key") / "report.json"


def report_manifest(team_key: str) -> PurePosixPath:
    return (
        PurePosixPath("aggregates")
        / safe_component(team_key, "team_key")
        / f"report{_MANIFEST_SUFFIX}"
    )


def reports_dir(team_key: str) -> PurePosixPath:
    """Directory for the Markdown reports."""
    return PurePosixPath("reports") / safe_component(team_key, "team_key")


#: The timestamp in a report's file name, in **local time**, to the minute.
#: Local, because the file name is the only part of this the user sees, and
#: they remember when they ran the command -- not what the clock said in UTC
#: at that moment. The ``generated_at`` inside the report is UTC, because
#: that is a timestamp on the data and not user interface.
REPORT_TIMESTAMP_FORMAT = "%Y-%m-%dT%H%M"


#: The largest ordinal for reports written within the same minute. Two
#: digits, because the ordinal in the name is zero-padded (see
#: :func:`report_name`): a three-digit limit would break the padding and put
#: the sort order back into disarray.
MAX_REPORTS_PER_MINUTE = 99


def report_name(timestamp: str, team_slug: str, ordinal: int = 1) -> str:
    """A report's file name: ``<timestamp>-<slug>.md``.

    Args:
        timestamp: Timestamp in the :data:`REPORT_TIMESTAMP_FORMAT` form.
        team_slug: The team in a form that is valid inside a file name.
        ordinal: Which report this is within the same minute. The first one
            gets no **suffix**; the ones after it get a **zero-padded** one,
            ``-02``, ``-03``. The padding is there for sorting: without it a
            directory listing would order the names ``-10, -100, -11, -2``,
            so the newest report would not be last in the list. **No earlier
            report is ever overwritten**, because a new run never lands on an
            old name.

    Raises:
        ValueError: If ``ordinal`` is outside the bounds ``1``..
            :data:`MAX_REPORTS_PER_MINUTE`.
    """
    if not 1 <= ordinal <= MAX_REPORTS_PER_MINUTE:
        raise ValueError(
            f"The report ordinal has to be 1..{MAX_REPORTS_PER_MINUTE}, "
            f"was {ordinal}."
        )
    suffix = "" if ordinal == 1 else f"-{ordinal:02d}"
    return f"{timestamp}-{team_slug}{suffix}.md"


def report_markdown(team_key: str, filename: str) -> PurePosixPath:
    """One Markdown report. The ``render`` stage's result."""
    return reports_dir(team_key) / safe_component(filename, "report_filename")


def render_manifest(team_key: str, report_filename: str) -> PurePosixPath:
    """One report's manifest: ``<report name without .md>.manifest.json``.

    The manifest is **for traceability, not for skipping**: the report is
    written under a new name on every run, so the stage is never skipped.

    **The manifest is per report, not per team.** A shared
    ``render.manifest.json`` would stand up badly to exactly the concurrency
    the file name is reserved atomically for: two simultaneous runs would
    each get their own report, but the manifest written last would be the one
    left standing, and it would describe a different file from the one the
    user just received. The report name is already unique, so a manifest name
    derived from it is unique too.
    """
    stem = report_filename.removesuffix(".md")
    return (
        reports_dir(team_key)
        / f"{safe_component(stem, 'report_filename')}{_MANIFEST_SUFFIX}"
    )


def import_dir() -> PurePosixPath:
    """The incoming folder, read only by ``pappascout import``."""
    return PurePosixPath("import")


def logs_dir(host: str) -> PurePosixPath:
    """Logs per machine, so that two machines do not write the same file."""
    return PurePosixPath("logs") / safe_component(host, "host")


#: An unexpanded environment variable in a path: ``%NAME%`` or ``${NAME}``.
#:
#: A bare ``$NAME`` is included only off Windows. In Windows paths the dollar
#: is a legal character in a directory name (``$Recycle.Bin``,
#: administrative shares), so forbidding it would reject valid paths. Two
#: unambiguous forms are enough: ``%NAME%`` is precisely the one
#: ``settings.toml`` contains.
#:
#: **A Windows limitation, recorded rather than worked around.** The
#: versioned ``archive_root`` is ``%PAPPASCOUT_ARCHIVE_ROOT%``, and that form
#: expands only on Windows -- so the tests that name the failure are Windows
#: tests. The repository is Windows-only anyway (PowerShell examples, the
#: ``%USERPROFILE%`` key file, drive letters), so the limitation is recorded
#: here rather than circumvented.
_UNEXPANDED_VAR = (
    re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
    if os.name == "nt"
    else re.compile(
        r"%([A-Za-z_][A-Za-z0-9_]*)%|\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?"
    )
)


def _check_absolute(root: Path, raw: str, source: str) -> None:
    """Fail if the archive root is a relative path.

    ``os.path.expandvars`` is silent about relative values and so is
    :func:`_check_expanded` -- there is no ``%NAME%`` left to complain about.
    A relative root is resolved against the current working directory, so the
    archive would be created wherever the command was run from, and every
    stage would happily fill it: the pipeline creates what is missing, so the
    run would report success while writing a second, empty archive. Inside the
    repository that tree would not even be ignored, because ``.gitignore``
    anchors only ``/archive/``.

    The trap is specific and cheap to fall into. The versioned line reads
    ``archive_root = '%PAPPASCOUT_ARCHIVE_ROOT%'``, which invites reading the
    variable as a *folder name* rather than a whole path; setting it to
    ``pappascout-archive`` then produces exactly this failure.

    Args:
        root: Expanded archive root.
        raw: Original value, for the message.
        source: Where the value came from, so the reader knows what to fix.

    Raises:
        PappascoutError: The message names the source and demands an
            absolute path.
    """
    if root.is_absolute():
        return
    raise PappascoutError(
        f"The archive root path {str(root)!r} is relative, and the archive "
        "root has to be an absolute path.\n"
        f"The value comes from {source} and is {raw!r}.\n"
        f"Give {ARCHIVE_ROOT_ENV_VAR} the archive's full path starting from "
        "the drive root -- not a folder name. A relative path is resolved "
        "against the working directory, so the archive would be created "
        "wherever the command happened to be run from, and the run would say "
        "nothing about it."
    )


def _check_expanded(
    expanded: str,
    raw: str,
    source: str,
    subject: str = "The archive root path",
) -> None:
    """Fail if an environment variable was left unexpanded.

    Args:
        expanded: The result of ``os.path.expandvars``.
        raw: The original value, for the error message.
        source: Where the value came from, so the user knows what to fix.
        subject: Which path this is about. The same guard also covers
            ``demos_root``, which is outside the archive, and a wrong name in
            the message would send the reader to fix the wrong line.

    Raises:
        PappascoutError: The message names the missing variable.
    """
    match = _UNEXPANDED_VAR.search(expanded)
    if match is None:
        return
    name = match.group(1) or match.group(2)
    raise PappascoutError(
        f"{subject} contains the environment variable {name}, which is "
        "not set on this machine.\n"
        f"The value comes from {source} and is {raw!r}.\n"
        f"Set {name}, or write the path out in full. Without this check the "
        "archive would be written into a directory whose name is literally "
        f"{match.group(0)!r}."
    )


@dataclass(frozen=True)
class ArchivePaths:
    """The archive root and the absolute paths bound to it.

    Every module-level path function is available as a method that returns a
    ``Path`` object. The relative form is always available from the functions
    directly.

    Attributes:
        root: The archive root (a synchronised folder).
        demos_root: Local directory for downloaded demos, **outside** the
            archive, or ``None``. See
            :attr:`~pappascout.domain.models.ProjectSettings.demos_root`.
            This is the only path in this class that is not inside the
            archive, which is why it is a field of its own and not derived
            from the root.
    """

    root: Path
    demos_root: Path | None = None

    @classmethod
    def from_settings(
        cls, archive_root: Path | str, demos_root: Path | str | None = None
    ) -> ArchivePaths:
        """Build the archive paths from the settings value.

        The path is expanded twice, so that the same versioned
        ``settings.toml`` works on both machines: ``%USERPROFILE%``-style
        environment variables and ``~`` are replaced with the machine's own
        values. The same expansion is applied to ``demos_root`` -- otherwise
        the local directory would be the only path that cannot be written
        machine-independently.

        **The environment variable is the only source of the real path.** The
        repository is public, so ``settings.toml`` does not carry the archive
        path: ``PAPPASCOUT_ARCHIVE_ROOT`` does, per machine. The versioned
        value is a placeholder -- literally ``%PAPPASCOUT_ARCHIVE_ROOT%`` --
        which ``os.path.expandvars`` leaves untouched when the variable is
        unset, so a fresh clone stops with the error described below instead of
        creating an empty archive somewhere else. The variable therefore does
        not point at *another* archive; it points at *the* archive.

        **The value must be absolute.** A relative value would be resolved
        against the current working directory, so the archive would land
        wherever the command happened to be run from -- inside the repository,
        most likely, where ``.gitignore`` anchors only ``/archive/`` and would
        not catch a tree under any other name. Nothing downstream would
        complain: the stages create what is missing, so the run would look
        like a success while filling a second, empty archive.

        **Redirecting the archive takes the demos with it.** When
        ``PAPPASCOUT_ARCHIVE_ROOT`` is set, ``demos_root`` is ignored and the
        demos go into the redirected archive's own ``demos/``. Otherwise a
        ``fetch`` run against a test archive would write and read the
        **production** demo directory -- and because idempotence only looks
        at whether a file exists, the test run would appear to succeed
        without downloading anything, and could overwrite real files.
        Isolation is not isolation if it covers only some of the paths.

        ``PAPPASCOUT_DEMOS_ROOT`` overrides both: it is the way to say
        explicitly "demos here" even when the archive is redirected.

        Raises:
            PappascoutError: If after expansion the path still holds an
                unexpanded environment variable. ``os.path.expandvars``
                **leaves ``%NAME%`` as it is** when the variable does not
                exist -- it raises no error and does not return an empty
                string. Without this check the run would create a directory
                named literally ``%USERPROFILE%``, write the whole archive
                there, and look like it had succeeded. An archive shared by
                two machines would break silently.
            PappascoutError: If the expanded archive root is relative. The
                message names the source, so the reader knows whether to fix
                the variable or the file.
        """
        override = os.environ.get(ARCHIVE_ROOT_ENV_VAR)
        raw = override if override else str(archive_root)
        source = (
            f"the environment variable {ARCHIVE_ROOT_ENV_VAR}"
            if override
            else "the setting [project].archive_root in settings.toml"
        )
        expanded = os.path.expandvars(str(raw))
        _check_expanded(expanded, raw, source)
        root = Path(expanded).expanduser()
        _check_absolute(root, raw, source)

        demos_override = os.environ.get(DEMOS_ROOT_ENV_VAR)
        if demos_override:
            raw_demos: str | None = demos_override
            demos_source = f"the environment variable {DEMOS_ROOT_ENV_VAR}"
        elif override:
            # A redirected archive takes the demos with it; see the docstring.
            raw_demos = None
            demos_source = ""
        else:
            raw_demos = None if demos_root is None else str(demos_root)
            demos_source = "the setting [project].demos_root in settings.toml"

        local: Path | None = None
        if raw_demos is not None and raw_demos.strip():
            expanded_demos = os.path.expandvars(raw_demos)
            _check_expanded(
                expanded_demos,
                raw_demos,
                demos_source,
                subject="The demo directory path",
            )
            local = Path(expanded_demos).expanduser()
        return cls(root=root, demos_root=local)

    def resolve(self, relative: PurePosixPath | str) -> Path:
        """Join a relative archive path to the root.

        Raises:
            PappascoutError: If the path is absolute. Manifests and indexes
                may contain relative paths only, so an absolute path is
                always a sign of a bug and is not accepted silently.
        """
        candidate = Path(str(relative))
        # On Windows "/etc/passwd" is not is_absolute() (no drive letter),
        # but its root is "\\" -- it is still an escape from the archive root.
        if candidate.is_absolute() or candidate.drive or candidate.root:
            raise PappascoutError(
                f"The archive path {relative!r} has to be relative to the "
                "archive root. An absolute path would break the archive on "
                "the other machine."
            )
        if ".." in candidate.parts:
            raise PappascoutError(
                f"The archive path {relative!r} contains '..' and therefore "
                "does not stay inside the archive root."
            )
        return self.root / candidate

    def relative(self, path: Path | str) -> PurePosixPath:
        """Convert an absolute path into the archive-internal relative form.

        Raises:
            ValueError: If the path is not inside the archive.
        """
        rel = Path(path).resolve().relative_to(self.root.resolve())
        return PurePosixPath(rel.as_posix())

    # -- Convenience shortcuts --------------------------------------------
    def raw_faceit(self) -> Path:
        return self.resolve(raw_faceit_dir())

    def teams_index(self) -> Path:
        return self.resolve(teams_index())

    def matches_index(self) -> Path:
        return self.resolve(matches_index())

    def selection(self, team_key: str) -> Path:
        return self.resolve(selection(team_key))

    def next_opponent(self, team_key: str) -> Path:
        return self.resolve(next_opponent(team_key))

    def demos_dir(self) -> Path:
        """The directory **demos are written to**.

        The local :attr:`demos_root` if it is set, otherwise the archive's
        own ``demos/``. One method rather than a condition at every call
        site: the disk-space check, the write and the line printed to the
        console all need the same answer, and three separate conditions would
        diverge.
        """
        if self.demos_root is not None:
            return self.demos_root
        return self.resolve(PurePosixPath("demos"))

    def archive_demos_dir(self) -> Path:
        """The archive's own ``demos/`` -- **even when demos go elsewhere**.

        Needed separately, because adopting a local directory must not lose
        the demos that were already downloaded into the archive.
        """
        return self.resolve(PurePosixPath("demos"))

    def demo(self, map_demo_id: str, suffix: str = DEFAULT_DEMO_SUFFIX) -> Path:
        """The demo's **write path**: :meth:`demos_dir` + the id.

        Note: this is no longer always ``resolve(demo(...))``. The
        module-level :func:`demo` gives the archive-internal relative path,
        and that is still the right answer to "where in the archive would the
        demo be" -- but the question "where is this demo written" can point
        outside the archive.
        """
        name = f"{safe_component(map_demo_id, 'map_demo_id')}{suffix}"
        return self.demos_dir() / name

    def find_demo(self, map_demo_id: str) -> Path | None:
        """Find a demo in **all locations**. ``None`` if there is no demo.

        The order is the local directory first, the archive second. It is the
        same order :func:`~pappascout.stages.parse.resolve_demo` uses (which
        carries on into ``import/``), and here the shared order is the whole
        condition for idempotence: **a demo already downloaded into the
        archive must not be downloaded again into the local directory.** If
        the search looked only where writes go, adopting the setting would
        download the entire sample a second time and waste the Downloads
        quota for nothing.
        """
        for directory in self.demo_dirs():
            for suffix in DEMO_SUFFIXES:
                candidate = (
                    directory
                    / f"{safe_component(map_demo_id, 'map_demo_id')}{suffix}"
                )
                if candidate.is_file():
                    return candidate
        return None

    def demo_meta(self, map_demo_id: str) -> Path:
        """The meta file's **write path** -- always beside the demo.

        The same directory as the demo and never a different one: the meta
        file is a claim about that exact file, and in different directories
        the two would diverge as soon as one is copied or deleted.
        """
        name = f"{safe_component(map_demo_id, 'map_demo_id')}.meta.json"
        return self.demos_dir() / name

    def find_demo_meta(self, map_demo_id: str) -> Path | None:
        """Find the meta file in the same order as :meth:`find_demo`."""
        name = f"{safe_component(map_demo_id, 'map_demo_id')}.meta.json"
        for directory in self.demo_dirs():
            candidate = directory / name
            if candidate.is_file():
                return candidate
        return None

    def demo_dirs(self) -> tuple[Path, ...]:
        """The directories **in search order**.

        1. the local ``demos_root``, if set -- that is where new downloads
           go,
        2. the archive's ``demos/`` -- where they went before the setting,
        3. the archive's ``import/`` -- manually imported demos.

        **One order, used by both the search and idempotence.** If ``fetch``
        looked in different places from ``parse``, one would download what
        the other already finds.

        **``import/`` finds only a demo stored under the canonical name**,
        and that is how it is right now, not a wish. FACEIT's own file name
        is ``{match_id}-{round}-{instance}`` (``...-1-1.dem``), while the
        archive's id is ``{match_id}-{map_index}`` (``...-0``) -- a file
        downloaded through the browser therefore does not match this search
        at all, and it has no ``.meta.json``, which idempotence requires.
        Renaming such a demo and writing its meta file is
        **``pappascout import``'s job, and it is done** (Story 3.6): the
        command reads the file from ``import/`` under FACEIT's own name and
        writes it into :meth:`demos_dir` under the canonical one, with the
        meta file beside it. So this search finds a browser-downloaded demo
        after that command has run over it -- from ``demos/``, not from
        ``import/`` -- and not before. That is why the search keeps one
        naming scheme instead of reaching into ``import/`` with a second one:
        the rename has an owner, and it is not this function.
        """
        dirs = [] if self.demos_root is None else [self.demos_root]
        dirs.append(self.archive_demos_dir())
        dirs.append(self.import_dir())
        return tuple(dirs)

    def parsed_root(self) -> Path:
        return self.resolve(parsed_root())

    def parsed_table(self, map_demo_id: str, table: str) -> Path:
        return self.resolve(parsed_table(map_demo_id, table))

    def parsed_manifest(self, map_demo_id: str) -> Path:
        return self.resolve(parsed_manifest(map_demo_id))

    def classified(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified(team_key, map_demo_id))

    def classified_round_list(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified_round_list(team_key, map_demo_id))

    def classified_manifest(self, team_key: str, map_demo_id: str) -> Path:
        return self.resolve(classified_manifest(team_key, map_demo_id))

    def report_json(self, team_key: str) -> Path:
        return self.resolve(report_json(team_key))

    def report_manifest(self, team_key: str) -> Path:
        return self.resolve(report_manifest(team_key))

    def reports_dir(self, team_key: str) -> Path:
        return self.resolve(reports_dir(team_key))

    def report_markdown(self, team_key: str, filename: str) -> Path:
        return self.resolve(report_markdown(team_key, filename))

    def render_manifest(self, team_key: str, report_filename: str) -> Path:
        return self.resolve(render_manifest(team_key, report_filename))

    def import_dir(self) -> Path:
        return self.resolve(import_dir())

    def logs_dir(self, host: str) -> Path:
        return self.resolve(logs_dir(host))

    def lock_file(self) -> Path:
        return self.resolve(LOCK_FILE)

    # -- Status information ------------------------------------------------
    def exists(self) -> bool:
        return self.root.is_dir()

    def total_size_bytes(self) -> int:
        """The archive's total size in bytes. ``0`` if it does not exist yet."""
        if not self.root.is_dir():
            return 0
        total = 0
        for path in self.root.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                # A cloud placeholder from the sync client, or a file
                # still being transferred.
                continue
        return total
